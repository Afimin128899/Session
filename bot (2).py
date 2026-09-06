#!/usr/bin/env python3
"""
Telegram Session Generator Bot — высоконадёжная async-версия
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

# ====================== КОНФИГ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str

    admin_ids: Set[int] = Field(default_factory=set)
    session_timeout_minutes: int = 12
    max_sessions_per_user: int = 3

    # Можно менять под свои нужды
    device_model: str = "SLANFB5Z-EXTREME"
    system_version: str = "Windows 10"
    app_version: str = "7.0.9 x64"
    lang_code: str = "en"
    system_lang_code: str = "en"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            return {int(x.strip()) for x in v.split(",") if x.strip()}
        if isinstance(v, set):
            return v
        return set()


settings = Settings()

# ====================== ЛОГИРОВАНИЕ ======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SessionBot")

# ====================== FSM ======================

class SessionStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


# ====================== МОДЕЛИ ======================

@dataclass
class ActiveSession:
    client: Optional[TelegramClient] = None
    phone: str = ""
    phone_code_hash: str = ""
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    attempts: int = 0


# Глобальное хранилище активных сессий (chat_id → ActiveSession)
active_sessions: Dict[int, ActiveSession] = {}

# Статистика
stats = {
    "started": 0,
    "success": 0,
    "failed": 0,
    "cancelled": 0,
}


# ====================== УТИЛИТЫ ======================

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def phone_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


def code_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Запросить код снова", callback_data="resend_code"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"),
        ]
    ])


def password_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


def normalize_phone(text: str) -> Optional[str]:
    text = text.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not text.startswith("+"):
        if text.startswith("8") and len(text) == 11:  # российский формат
            text = "+7" + text[1:]
        else:
            return None
    if not text[1:].isdigit() or not (10 <= len(text) <= 16):
        return None
    return text


async def cleanup_user(chat_id: int, state: Optional[FSMContext] = None) -> None:
    """Полная очистка пользователя."""
    sess = active_sessions.pop(chat_id, None)
    if sess:
        if sess.client:
            try:
                if sess.client.is_connected():
                    await sess.client.disconnect()
            except Exception:
                pass
        if sess.temp_dir and sess.temp_dir.exists():
            try:
                shutil.rmtree(sess.temp_dir, ignore_errors=True)
            except Exception:
                pass
    if state:
        await state.clear()


async def periodic_cleanup() -> None:
    """Фоновая очистка просроченных сессий."""
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        timeout = timedelta(minutes=settings.session_timeout_minutes)
        to_remove = [
            cid for cid, s in active_sessions.items()
            if now - s.created_at > timeout
        ]
        for cid in to_remove:
            logger.info(f"Очистка просроченной сессии {cid}")
            await cleanup_user(cid)
            stats["cancelled"] += 1


# ====================== TELETHON HELPERS ======================

@asynccontextmanager
async def managed_client(session_path: str, api_id: int, api_hash: str):
    client = TelegramClient(
        session_path,
        api_id,
        api_hash,
        device_model=settings.device_model,
        system_version=settings.system_version,
        app_version=settings.app_version,
        lang_code=settings.lang_code,
        system_lang_code=settings.system_lang_code,
    )
    try:
        await client.connect()
        yield client
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass


async def create_metadata(client: TelegramClient, phone: str) -> dict[str, Any]:
    """Создаём метаданные, пока клиент ещё подключён."""
    me: Optional[User] = await client.get_me()
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
        "role": "",
        "id": me.id if me else None,
        "phone": phone,
        "username": me.username if me else None,
        "date_of_birth": None,
        "date_of_birth_integrity": None,
        "is_premium": bool(getattr(me, "premium", False)),
        "has_profile_pic": bool(getattr(me, "photo", None)),
        "spamblock": None,
        "register_time": ts,
        "last_check_time": ts,
        "avatar": None,
        "first_name": (me.first_name or "") if me else "",
        "last_name": (me.last_name or "") if me else "",
        "sex": None,
        "proxy": None,
        "ipv6": False,
        "session_file": f"{phone}.session",
    }


async def finalize_and_send(
    message: Message,
    state: FSMContext,
    sess: ActiveSession,
) -> None:
    """Финальный этап: метаданные → zip → отправка → очистка."""
    chat_id = message.chat.id
    client = sess.client
    phone = sess.phone
    temp_dir = sess.temp_dir

    if not client or not temp_dir:
        await message.answer("❌ Внутренняя ошибка сессии.")
        await cleanup_user(chat_id, state)
        return

    try:
        # Проверяем, что действительно авторизованы
        if not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Сессия не авторизована")

        metadata = await create_metadata(client, phone)

        # Отключаем клиент (файл .session корректно запишется)
        await client.disconnect()
        sess.client = None

        session_file = temp_dir / f"{phone}.session"
        # Telethon создаёт файл с именем session_path + ".session"
        original = Path(str(temp_dir / "session") + ".session")
        if original.exists():
            original.rename(session_file)

        meta_file = temp_dir / f"{phone}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        # Создаём удобный ZIP
        zip_path = temp_dir / f"{phone}_session.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_file, arcname=f"{phone}.session")
            zf.write(meta_file, arcname=f"{phone}.json")

        # Отправляем
        data = zip_path.read_bytes()
        await message.answer_document(
            BufferedInputFile(data, filename=f"{phone}_session.zip"),
            caption=(
                f"✅ <b>Сессия успешно создана!</b>\n\n"
                f"📞 <code>{phone}</code>\n"
                f"🆔 <code>{metadata.get('id')}</code>\n"
                f"👤 {metadata.get('first_name', '')} {metadata.get('last_name', '')}\n"
                f"{'⭐ Premium' if metadata.get('is_premium') else ''}"
            ),
        )

        stats["success"] += 1
        logger.info(f"Успех: {phone} (user={chat_id})")

        await message.answer(
            "Файлы отправлены и удалены с сервера.\n"
            "Можно создать новую сессию: /start"
        )

    except Exception as e:
        logger.exception(f"Ошибка финализации {chat_id}")
        stats["failed"] += 1
        await message.answer(f"❌ Ошибка при создании файлов: <code>{e}</code>")
    finally:
        await cleanup_user(chat_id, state)


# ====================== ХЕНДЛЕРЫ ======================

router = Router()


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await cleanup_user(chat_id, state)  # на всякий случай

    # Ограничение количества одновременных попыток
    user_active = sum(1 for s in active_sessions.values() if s.created_at > datetime.now() - timedelta(hours=1))
    # Простая защита от массового создания
    if len(active_sessions) > 50:  # глобальный лимит
        await message.answer("⏳ Сервер сейчас перегружен. Попробуйте через несколько минут.")
        return

    stats["started"] += 1
    active_sessions[chat_id] = ActiveSession()
    await state.set_state(SessionStates.waiting_phone)

    await message.answer(
        "👋 <b>Генератор Telegram-сессий</b>\n\n"
        "Я создам файл <code>.session</code> + метаданные в удобном ZIP-архиве.\n\n"
        "📱 Пришлите номер телефона в международном формате:\n"
        "<code>+79001234567</code>",
        reply_markup=phone_keyboard(),
    )


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await cleanup_user(chat_id, state)
    stats["cancelled"] += 1

    text = "✅ Процесс отменён."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text)
        await event.answer()
    else:
        await event.answer(text)


@router.message(SessionStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    chat_id = message.chat.id
    phone = normalize_phone(message.text or "")

    if not phone:
        await message.answer(
            "❌ Неверный формат номера.\nПример: <code>+79001234567</code>",
            reply_markup=phone_keyboard(),
        )
        return

    sess = active_sessions.get(chat_id)
    if not sess:
        await message.answer("Сессия потеряна. Начните заново: /start")
        return

    sess.phone = phone
    sess.temp_dir = Path(tempfile.mkdtemp(prefix=f"tg_sess_{chat_id}_"))
    session_path = str(sess.temp_dir / "session")

    status_msg = await message.answer("⏳ Подключаюсь к Telegram и отправляю код…")

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

        await state.set_state(SessionStates.waiting_code)
        await status_msg.edit_text(
            f"📩 Код отправлен на <code>{phone}</code>\n\n"
            "Введите код подтверждения:",
            reply_markup=code_keyboard(),
        )

    except PhoneNumberInvalidError:
        await status_msg.edit_text("❌ Неверный номер телефона.")
        await cleanup_user(chat_id, state)
    except PhoneNumberBannedError:
        await status_msg.edit_text("❌ Этот номер заблокирован в Telegram.")
        await cleanup_user(chat_id, state)
    except FloodWaitError as e:
        await status_msg.edit_text(f"⏳ FloodWait: подождите {e.seconds} секунд.")
        await cleanup_user(chat_id, state)
    except ApiIdInvalidError:
        await status_msg.edit_text("❌ Неверные API_ID / API_HASH. Обратитесь к администратору.")
        await cleanup_user(chat_id, state)
    except Exception as e:
        logger.exception(f"Ошибка отправки кода {chat_id}")
        await status_msg.edit_text(f"❌ Ошибка: <code>{e}</code>")
        await cleanup_user(chat_id, state)


@router.callback_query(F.data == "resend_code", StateFilter(SessionStates.waiting_code))
async def resend_code(callback: CallbackQuery, state: FSMContext):
    chat_id = callback.from_user.id
    sess = active_sessions.get(chat_id)
    if not sess or not sess.client:
        await callback.answer("Сессия потеряна", show_alert=True)
        return

    try:
        result = await sess.client.send_code_request(sess.phone)
        sess.phone_code_hash = result.phone_code_hash
        await callback.message.edit_text(
            f"📩 Новый код отправлен на <code>{sess.phone}</code>\n\nВведите код:",
            reply_markup=code_keyboard(),
        )
        await callback.answer("Код отправлен повторно")
    except FloodWaitError as e:
        await callback.answer(f"Подождите {e.seconds} сек.", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@router.message(SessionStates.waiting_code)
async def process_code(message: Message, state: FSMContext):
    chat_id = message.chat.id
    code = (message.text or "").strip().replace(" ", "")

    sess = active_sessions.get(chat_id)
    if not sess or not sess.client:
        await message.answer("Сессия потеряна. /start")
        return

    sess.attempts += 1
    if sess.attempts > 5:
        await message.answer("❌ Слишком много попыток. Начните заново: /start")
        await cleanup_user(chat_id, state)
        return

    try:
        await sess.client.sign_in(
            phone=sess.phone,
            code=code,
            phone_code_hash=sess.phone_code_hash,
        )
        # Успех без 2FA
        await finalize_and_send(message, state, sess)

    except SessionPasswordNeededError:
        await state.set_state(SessionStates.waiting_password)
        await message.answer(
            "🔒 На аккаунте включена двухфакторная аутентификация.\n\n"
            "Введите облачный пароль:",
            reply_markup=password_keyboard(),
        )

    except PhoneCodeInvalidError:
        await message.answer(
            "❌ Неверный код. Попробуйте ещё раз:",
            reply_markup=code_keyboard(),
        )
    except PhoneCodeExpiredError:
        await message.answer("❌ Код истёк. Начните заново: /start")
        await cleanup_user(chat_id, state)
    except FloodWaitError as e:
        await message.answer(f"⏳ FloodWait: {e.seconds} сек.")
        await cleanup_user(chat_id, state)
    except Exception as e:
        logger.exception(f"Ошибка кода {chat_id}")
        await message.answer(f"❌ Ошибка: <code>{e}</code>")
        await cleanup_user(chat_id, state)


@router.message(SessionStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = (message.text or "").strip()

    sess = active_sessions.get(chat_id)
    if not sess or not sess.client:
        await message.answer("Сессия потеряна. /start")
        return

    try:
        await sess.client.sign_in(password=password)
        await finalize_and_send(message, state, sess)

    except PasswordHashInvalidError:
        await message.answer(
            "❌ Неверный пароль. Попробуйте ещё раз:",
            reply_markup=password_keyboard(),
        )
    except Exception as e:
        logger.exception(f"Ошибка 2FA {chat_id}")
        await message.answer(f"❌ Ошибка: <code>{e}</code>")
        await cleanup_user(chat_id, state)


# ====================== АДМИН ======================

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"Запусков: {stats['started']}\n"
        f"Успешных: {stats['success']}\n"
        f"Ошибок: {stats['failed']}\n"
        f"Отменено: {stats['cancelled']}\n"
        f"Активных сейчас: {len(active_sessions)}"
    )
    await message.answer(text)


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message):
    if not is_admin(message.from_user.id):
        return
    count = len(active_sessions)
    for cid in list(active_sessions.keys()):
        await cleanup_user(cid)
    await message.answer(f"Очищено сессий: {count}")


# ====================== ЗАПУСК ======================

async def main():
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode="HTML")
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Фоновая очистка
    asyncio.create_task(periodic_cleanup())

    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        # Graceful shutdown
        for cid in list(active_sessions.keys()):
            await cleanup_user(cid)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
