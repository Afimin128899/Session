#!/usr/bin/env python3
"""
Telegram Session Migrator Bot
- Вход по номеру телефона
- Вход по .session
- Режимы /auto и /manual
- Доступ только по токену (админ-панель)
- Очистка профиля + выход из всех чатов
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import DeleteChatUserRequest
from telethon.tl.functions.photos import DeletePhotosRequest, GetUserPhotosRequest
from telethon.tl.types import Channel, Chat, InputPhoto, User

# ====================== КОНФИГ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str

    admin_ids: Set[int] = Field(default_factory=set)
    session_timeout_minutes: int = 15
    max_queue_per_user: int = 8
    default_auto_code: bool = True

    device_model: str = "SLANFB5Z-EXTREME"
    system_version: str = "Windows 10"
    app_version: str = "7.0.9 x64"
    lang_code: str = "en"
    system_lang_code: str = "en"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if v is None:
            return set()
        if isinstance(v, int):
            return {v}
        if isinstance(v, str):
            return {int(x.strip()) for x in v.split(",") if x.strip()}
        if isinstance(v, (list, set, tuple)):
            return {int(x) for x in v}
        return set()


print("[startup] Loading Settings...", flush=True)
try:
    settings = Settings()
except (ValueError, ValidationError) as e:
    # pydantic's ValidationError subclasses ValueError; print the exact cause
    # so missing/invalid environment variables are visible in deploy logs.
    print(f"[startup] FATAL: failed to load Settings: {e}", file=sys.stderr, flush=True)
    print(
        "[startup] Required environment variables: API_ID (int), API_HASH (str), BOT_TOKEN (str)",
        file=sys.stderr,
        flush=True,
    )
    raise
except Exception as e:
    print(f"[startup] FATAL: unexpected error while loading Settings: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    traceback.print_exc()
    raise
print("[startup] Settings loaded successfully", flush=True)

# ====================== ЛОГИРОВАНИЕ ======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SessionBot")

# ====================== FSM ======================

class States(StatesGroup):
    waiting_token = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_manual_code = State()
    waiting_session = State()


# ====================== ХРАНИЛИЩЕ ТОКЕНОВ ======================

# token -> {"uses_left": int | None, "created_by": int, "created_at": datetime, "note": str}
TOKENS: Dict[str, dict] = {}

# chat_id -> True (авторизован)
AUTHORIZED: Set[int] = set()

# Режим кода для пользователя
user_auto_mode: Dict[int, bool] = {}


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def get_auto_mode(chat_id: int) -> bool:
    return user_auto_mode.get(chat_id, settings.default_auto_code)


def generate_token(length: int = 12) -> str:
    return secrets.token_urlsafe(length)[:length].upper()


# ====================== МОДЕЛИ ======================

@dataclass
class ActiveSession:
    client: Optional[TelegramClient] = None
    old_client: Optional[TelegramClient] = None
    phone: str = ""
    phone_code_hash: str = ""
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    attempts: int = 0
    last_code_sent_at: Optional[datetime] = None
    code_event: Optional[asyncio.Event] = None
    received_code: Optional[str] = None
    is_session_mode: bool = False
    status_message: Optional[Message] = None


active_sessions: Dict[int, ActiveSession] = {}

stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0, "tokens_used": 0}


# ====================== КЛАВИАТУРЫ ======================

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Войти по номеру", callback_data="mode_phone")],
        [InlineKeyboardButton(text="📁 Войти по .session", callback_data="mode_session")],
        [
            InlineKeyboardButton(text="🔄 Авто-код", callback_data="set_auto"),
            InlineKeyboardButton(text="✍️ Ручной код", callback_data="set_manual"),
        ],
        [InlineKeyboardButton(text="❌ Выйти", callback_data="logout")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать токен (1 использование)", callback_data="token_1")],
        [InlineKeyboardButton(text="➕ Создать токен (5 использований)", callback_data="token_5")],
        [InlineKeyboardButton(text="➕ Создать токен (безлимит)", callback_data="token_inf")],
        [InlineKeyboardButton(text="📋 Список токенов", callback_data="token_list")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
    ])


# ====================== УТИЛИТЫ ======================

def normalize_phone(text: str) -> Optional[str]:
    text = text.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if text.startswith("00"):
        text = text[2:]
    if text.startswith("+"):
        digits = text[1:]
    else:
        digits = text
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if not digits.isdigit() or not (10 <= len(digits) <= 15):
        return None
    return f"+{digits}"


def normalize_code(text: str) -> Optional[str]:
    code = text.strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or not (4 <= len(code) <= 8):
        return None
    return code


async def cleanup_user(chat_id: int, state: Optional[FSMContext] = None):
    sess = active_sessions.pop(chat_id, None)
    if sess:
        for client in (sess.client, sess.old_client):
            if client:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass
        if sess.temp_dir and sess.temp_dir.exists():
            shutil.rmtree(sess.temp_dir, ignore_errors=True)
    if state:
        await state.clear()


# ====================== ОЧИСТКА АККАУНТА ======================

async def clean_account(client: TelegramClient, status: Message):
    try:
        await status.edit_text("🧹 Очищаю профиль (имя, ава, описание)...")

        try:
            await client(UpdateProfileRequest(first_name=".", last_name="", about=""))
        except Exception as e:
            logger.warning(f"Профиль: {e}")

        try:
            photos = await client(GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=100))
            if photos.photos:
                input_photos = [
                    InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
                    for p in photos.photos
                ]
                await client(DeletePhotosRequest(id=input_photos))
        except Exception as e:
            logger.warning(f"Фото: {e}")

        await status.edit_text("🧹 Выхожу из всех групп и каналов...")

        left = 0
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            try:
                if isinstance(entity, Channel):
                    await client(LeaveChannelRequest(entity))
                    left += 1
                elif isinstance(entity, Chat):
                    await client(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
                    left += 1
                if left % 12 == 0:
                    await asyncio.sleep(1.4)
                else:
                    await asyncio.sleep(0.3)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                continue

        await status.edit_text(f"✅ Очистка завершена (вышел из {left} чатов)")
    except Exception as e:
        logger.exception("Ошибка очистки")
        await status.edit_text(f"⚠️ Очистка с ошибками: {e}")


# ====================== TELETHON HELPERS ======================

async def create_metadata(client: TelegramClient, phone: str) -> dict[str, Any]:
    me: User = await client.get_me()
    ts = int(time.time())
    return {
        "app_id": settings.api_id,
        "app_hash": settings.api_hash,
        "device": settings.device_model,
        "sdk": settings.system_version,
        "app_version": settings.app_version,
        "system_lang_pack": settings.system_lang_code,
        "system_lang_code": settings.system_lang_code,
        "lang_pack": "tdesktop",
        "lang_code": settings.lang_code,
        "twoFA": None,
        "id": me.id,
        "phone": phone,
        "username": me.username,
        "is_premium": bool(getattr(me, "premium", False)),
        "has_profile_pic": bool(getattr(me, "photo", None)),
        "register_time": ts,
        "last_check_time": ts,
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "session_file": f"{phone}.session",
    }


async def finalize_and_send(message: Message, state: FSMContext, sess: ActiveSession):
    chat_id = message.chat.id
    client = sess.client
    phone = sess.phone
    temp_dir = sess.temp_dir
    status = sess.status_message or message

    try:
        if not client or not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Сессия не авторизована")

        await clean_account(client, status)

        if sess.old_client:
            try:
                if sess.old_client.is_connected():
                    await sess.old_client.log_out()
            except Exception:
                pass

        try:
            await client(ResetAuthorizationsRequest())
        except Exception:
            pass

        await asyncio.sleep(1.0)

        metadata = await create_metadata(client, phone)
        await client.disconnect()
        sess.client = None

        session_file = temp_dir / f"{phone}.session"
        original = Path(str(temp_dir / "session") + ".session")
        if original.exists():
            original.rename(session_file)
        if not session_file.exists():
            # для session-режима имя другое
            original2 = Path(str(temp_dir / "new_session") + ".session")
            if original2.exists():
                original2.rename(session_file)

        if not session_file.exists():
            raise FileNotFoundError("Файл сессии не создан")

        meta_file = temp_dir / f"{phone}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = temp_dir / f"{phone}_session.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_file, arcname=f"{phone}.session")
            zf.write(meta_file, arcname=f"{phone}.json")

        data = zip_path.read_bytes()
        await message.answer_document(
            BufferedInputFile(data, filename=f"{phone}_session.zip"),
            caption=(
                f"✅ <b>Сессия готова + очищена!</b>\n\n"
                f"📞 <code>{phone}</code>\n"
                f"🆔 <code>{metadata['id']}</code>\n"
                f"👤 Имя / ава / био удалены\n"
                f"🚪 Вышел из всех чатов\n"
                f"🔒 Старые сессии завершены"
            ),
        )
        stats["success"] += 1
        await message.answer("Можно создать ещё одну сессию:", reply_markup=main_menu_kb())

    except Exception as e:
        logger.exception("finalize error")
        stats["failed"] += 1
        await message.answer(f"❌ Ошибка: <code>{e}</code>")
    finally:
        await cleanup_user(chat_id, state)


# ====================== ХЕНДЛЕРЫ ======================

router = Router()


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await cleanup_user(chat_id, state)

    if chat_id in AUTHORIZED:
        mode = "авто" if get_auto_mode(chat_id) else "ручной"
        await message.answer(
            f"👋 С возвращением!\nРежим кода: <b>{mode}</b>\n\nВыбери способ входа:",
            reply_markup=main_menu_kb(),
        )
        return

    await state.set_state(States.waiting_token)
    await message.answer(
        "🔒 <b>Доступ только по токену</b>\n\n"
        "Отправь токен, который тебе выдал администратор:"
    )


@router.message(States.waiting_token)
async def process_token(message: Message, state: FSMContext):
    chat_id = message.chat.id
    token = (message.text or "").strip().upper()

    if token not in TOKENS:
        await message.answer("❌ Неверный или несуществующий токен. Попробуй ещё раз:")
        return

    info = TOKENS[token]
    uses = info.get("uses_left")

    if uses is not None:
        if uses <= 0:
            await message.answer("❌ Этот токен больше не действителен.")
            return
        info["uses_left"] = uses - 1
        if info["uses_left"] <= 0:
            del TOKENS[token]

    AUTHORIZED.add(chat_id)
    stats["tokens_used"] += 1
    await state.clear()

    mode = "авто" if get_auto_mode(chat_id) else "ручной"
    await message.answer(
        f"✅ Токен принят!\nРежим кода: <b>{mode}</b>\n\nВыбери способ входа:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "logout")
async def cb_logout(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.from_user.id
    AUTHORIZED.discard(chat_id)
    await cleanup_user(chat_id, state)
    await callback.message.edit_text("Вы вышли. Для входа снова нужен токен.\n/start")
    await callback.answer()


@router.callback_query(F.data == "set_auto")
async def cb_set_auto(callback: CallbackQuery):
    user_auto_mode[callback.from_user.id] = True
    await callback.answer("Режим: автоматический")
    await callback.message.edit_text(
        "Режим кода: <b>авто</b>\nВыбери способ входа:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "set_manual")
async def cb_set_manual(callback: CallbackQuery):
    user_auto_mode[callback.from_user.id] = False
    await callback.answer("Режим: ручной")
    await callback.message.edit_text(
        "Режим кода: <b>ручной</b>\nВыбери способ входа:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "cancel")
@router.message(Command("cancel"))
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await cleanup_user(chat_id, state)
    stats["cancelled"] += 1

    text = "✅ Отменено."
    if chat_id in AUTHORIZED:
        if isinstance(event, CallbackQuery):
            await event.message.edit_text(text, reply_markup=main_menu_kb())
        else:
            await event.answer(text, reply_markup=main_menu_kb())
    else:
        if isinstance(event, CallbackQuery):
            await event.message.edit_text(text + "\n/start")
        else:
            await event.answer(text + "\n/start")
    if isinstance(event, CallbackQuery):
        await event.answer()


# ---------- РЕЖИМ ПО НОМЕРУ ----------

@router.callback_query(F.data == "mode_phone")
async def cb_mode_phone(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.from_user.id
    if chat_id not in AUTHORIZED:
        await callback.answer("Нужен токен", show_alert=True)
        return

    await cleanup_user(chat_id, state)
    active_sessions[chat_id] = ActiveSession()
    await state.set_state(States.waiting_phone)

    await callback.message.edit_text(
        "📱 Пришли номер телефона в международном формате:\n"
        "<code>79001234567</code> или <code>+79001234567</code>",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(States.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    chat_id = message.chat.id
    if chat_id not in AUTHORIZED:
        return

    phone = normalize_phone(message.text or "")
    if not phone:
        await message.answer("❌ Неверный формат номера.", reply_markup=cancel_kb())
        return

    sess = active_sessions.get(chat_id)
    if not sess:
        await message.answer("Сессия потеряна. /start")
        return

    sess.phone = phone
    sess.temp_dir = Path(tempfile.mkdtemp(prefix=f"tg_{chat_id}_"))
    session_path = str(sess.temp_dir / "session")

    status = await message.answer("⏳ Подключаюсь и отправляю код…")
    sess.status_message = status

    try:
        client = TelegramClient(
            session_path,
            settings.api_id,
            settings.api_hash,
            device_model=settings.device_model,
            system_version=settings.system_version,
            app_version=settings.app_version,
            lang_code=settings.lang_code,
            system_lang_code=settings.system_lang_code,
        )
        await client.connect()
        sess.client = client

        result = await client.send_code_request(phone)
        sess.phone_code_hash = result.phone_code_hash
        sess.last_code_sent_at = datetime.now()

        auto = get_auto_mode(chat_id)
        if auto:
            # В режиме авто по номеру код всё равно нужно вводить вручную
            # (нет старой сессии). Поэтому просто просим код.
            await state.set_state(States.waiting_code)
            await status.edit_text(
                f"📩 Код отправлен на <code>{phone}</code>\n\nВведи код:",
                reply_markup=cancel_kb(),
            )
        else:
            await state.set_state(States.waiting_code)
            await status.edit_text(
                f"📩 Код отправлен на <code>{phone}</code>\n\nВведи код:",
                reply_markup=cancel_kb(),
            )

    except PhoneNumberInvalidError:
        await status.edit_text("❌ Неверный номер.")
        await cleanup_user(chat_id, state)
    except PhoneNumberBannedError:
        await status.edit_text("❌ Номер заблокирован.")
        await cleanup_user(chat_id, state)
    except FloodWaitError as e:
        await status.edit_text(f"⏳ FloodWait: {e.seconds} сек.")
        await cleanup_user(chat_id, state)
    except Exception as e:
        logger.exception("phone error")
        await status.edit_text(f"❌ Ошибка: <code>{e}</code>")
        await cleanup_user(chat_id, state)


@router.message(States.waiting_code)
async def process_code(message: Message, state: FSMContext):
    chat_id = message.chat.id
    code = normalize_code(message.text or "")
    sess = active_sessions.get(chat_id)

    if not sess or not sess.client:
        await message.answer("Сессия потеряна. /start")
        return

    if not code:
        await message.answer("❌ Код 4–8 цифр.", reply_markup=cancel_kb())
        return

    sess.attempts += 1
    if sess.attempts > 5:
        await message.answer("❌ Слишком много попыток.")
        await cleanup_user(chat_id, state)
        return


# ====================== ЗАПУСК ======================

async def main() -> None:
    print("[startup] Creating bot...", flush=True)
    try:
        bot = Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
    except Exception as e:
        print(f"[startup] FATAL: failed to create bot: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    print("[startup] Starting dispatcher polling...", flush=True)
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"[startup] FATAL: dispatcher crashed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("[startup] Bot stopped", flush=True)
    except Exception as e:
        print(f"[startup] FATAL: unhandled error in main(): {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
