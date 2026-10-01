#!/usr/bin/env python3
"""
Telegram Session Migrator Bot
Отправляешь старый .session → бот сам получает код → создаёт новую сессию → убивает старую
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Set

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
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.tl.types import User

# ====================== КОНФИГ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str

    admin_ids: Set[int] = Field(default_factory=set)
    session_timeout_minutes: int = 10

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


settings = Settings()

# ====================== ЛОГИРОВАНИЕ ======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SessionMigrator")

# ====================== FSM ======================

class MigrateStates(StatesGroup):
    waiting_session = State()
    waiting_password = State()


# ====================== МОДЕЛИ ======================

@dataclass
class ActiveMigration:
    old_client: Optional[TelegramClient] = None
    new_client: Optional[TelegramClient] = None
    phone: str = ""
    phone_code_hash: str = ""
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    code_event: Optional[asyncio.Event] = None
    received_code: Optional[str] = None


active: Dict[int, ActiveMigration] = {}

stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0}


# ====================== УТИЛИТЫ ======================

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


async def cleanup(chat_id: int, state: Optional[FSMContext] = None):
    mig = active.pop(chat_id, None)
    if mig:
        for client in (mig.old_client, mig.new_client):
            if client:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass
        if mig.temp_dir and mig.temp_dir.exists():
            shutil.rmtree(mig.temp_dir, ignore_errors=True)
    if state:
        await state.clear()


async def periodic_cleanup():
    while True:
        await asyncio.sleep(45)
        now = datetime.now()
        timeout = timedelta(minutes=settings.session_timeout_minutes)
        for cid in list(active.keys()):
            if now - active[cid].created_at > timeout:
                logger.info(f"Timeout cleanup {cid}")
                await cleanup(cid)
                stats["cancelled"] += 1


# ====================== TELETHON ======================

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


async def finalize(message: Message, state: FSMContext, mig: ActiveMigration):
    chat_id = message.chat.id
    client = mig.new_client
    phone = mig.phone
    temp_dir = mig.temp_dir

    try:
        if not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Новая сессия не авторизована")

        # Убиваем все старые сессии
        try:
            await client(ResetAuthorizationsRequest())
            logger.info(f"Старые сессии убиты для {phone}")
        except Exception as e:
            logger.warning(f"Не удалось убить старые сессии: {e}")

        metadata = await create_metadata(client, phone)

        await client.disconnect()
        mig.new_client = None

        session_file = temp_dir / f"{phone}.session"
        original = Path(str(temp_dir / "new_session") + ".session")
        if original.exists():
            original.rename(session_file)

        if not session_file.exists():
            raise FileNotFoundError("Файл новой сессии не создан")

        meta_file = temp_dir / f"{phone}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = temp_dir / f"{phone}_new_session.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_file, arcname=f"{phone}.session")
            zf.write(meta_file, arcname=f"{phone}.json")

        data = zip_path.read_bytes()
        await message.answer_document(
            BufferedInputFile(data, filename=f"{phone}_new_session.zip"),
            caption=(
                f"✅ <b>Новая сессия создана!</b>\n\n"
                f"📞 <code>{phone}</code>\n"
                f"🆔 <code>{metadata['id']}</code>\n"
                f"👤 {metadata['first_name']} {metadata['last_name']}\n"
                f"{'⭐ Premium' if metadata['is_premium'] else ''}\n\n"
                f"🔒 Старая сессия завершена."
            ),
        )

        stats["success"] += 1
        logger.info(f"Успех миграции {phone} (user={chat_id})")

        await message.answer("Готово. Можно отправить следующий .session: /start")

    except Exception as e:
        logger.exception(f"Ошибка финализации {chat_id}")
        stats["failed"] += 1
        await message.answer(f"❌ Ошибка при создании файлов: <code>{e}</code>\nПопробуйте /start")
    finally:
        await cleanup(chat_id, state)


# ====================== ХЕНДЛЕРЫ ======================

router = Router()


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await cleanup(chat_id, state)

    if len(active) > 30:
        await message.answer("⏳ Сервер перегружен, попробуйте позже.")
        return

    stats["started"] += 1
    active[chat_id] = ActiveMigration()
    await state.set_state(MigrateStates.waiting_session)

    await message.answer(
        "🔄 <b>Мигратор сессий</b>\n\n"
        "Отправь мне <b>старый .session</b> файл.\n\n"
        "Я:\n"
        "1. Загружу его\n"
        "2. Сам запрошу код\n"
        "3. Автоматически получу и введу код\n"
        "4. Создам новую сессию\n"
        "5. Завершу старую\n"
        "6. Пришлю тебе новую .session",
        reply_markup=cancel_kb(),
    )


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await cleanup(chat_id, state)
    stats["cancelled"] += 1

    text = "✅ Отменено."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text)
        await event.answer()
    else:
        await event.answer(text)


@router.message(MigrateStates.waiting_session, F.document)
async def process_session_file(message: Message, state: FSMContext):
    chat_id = message.chat.id
    doc: Document = message.document

    if not doc.file_name or not doc.file_name.lower().endswith(".session"):
        await message.answer("❌ Нужен именно файл с расширением <code>.session</code>", reply_markup=cancel_kb())
        return

    mig = active.get(chat_id)
    if not mig:
        await message.answer("Сессия потеряна. /start")
        return

    status = await message.answer("⏳ Скачиваю и загружаю старую сессию...")

    try:
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"mig_{chat_id}_"))
        old_session_path = mig.temp_dir / "old.session"
        new_session_path = str(mig.temp_dir / "new_session")

        # Скачиваем файл
        await message.bot.download(doc, destination=old_session_path)

        # === Старый клиент ===
        old_client = TelegramClient(
            str(old_session_path.with_suffix("")),
            settings.api_id,
            settings.api_hash,
            device_model=settings.device_model,
            system_version=settings.system_version,
            app_version=settings.app_version,
            lang_code=settings.lang_code,
            system_lang_code=settings.system_lang_code,
        )
        await old_client.connect()

        if not await old_client.is_user_authorized():
            await status.edit_text("❌ Этот .session не авторизован / протух.")
            await cleanup(chat_id, state)
            return

        me = await old_client.get_me()
        phone = me.phone
        if not phone:
            await status.edit_text("❌ Не удалось получить номер телефона из сессии.")
            await cleanup(chat_id, state)
            return

        phone = f"+{phone}" if not str(phone).startswith("+") else str(phone)
        mig.phone = phone
        mig.old_client = old_client

        await status.edit_text(
            f"✅ Старая сессия загружена\n📞 <code>{phone}</code>\n\n"
            f"⏳ Создаю новую сессию и запрашиваю код..."
        )

        # === Новый клиент ===
        new_client = TelegramClient(
            new_session_path,
            settings.api_id,
            settings.api_hash,
            device_model=settings.device_model,
            system_version=settings.system_version,
            app_version=settings.app_version,
            lang_code=settings.lang_code,
            system_lang_code=settings.system_lang_code,
        )
        await new_client.connect()
        mig.new_client = new_client

        # Запрашиваем код
        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash

        # Готовим ожидание кода
        mig.code_event = asyncio.Event()
        mig.received_code = None

        @old_client.on(events.NewMessage(from_users=777000))
        async def code_handler(event):
            if mig.code_event and not mig.code_event.is_set():
                match = re.search(r"\b(\d{5,6})\b", event.raw_text)
                if match:
                    mig.received_code = match.group(1)
                    logger.info(f"Код получен: {mig.received_code}")
                    mig.code_event.set()

        await status.edit_text("📩 Код запрошен. Жду сообщение от Telegram (777000)...")

        # Ждём код максимум 90 секунд
        try:
            await asyncio.wait_for(mig.code_event.wait(), timeout=90)
        except asyncio.TimeoutError:
            await status.edit_text("❌ Код не пришёл за 90 секунд. Попробуйте ещё раз.")
            await cleanup(chat_id, state)
            return

        code = mig.received_code
        if not code:
            await status.edit_text("❌ Не удалось извлечь код.")
            await cleanup(chat_id, state)
            return

        await status.edit_text(f"✅ Код получен: <code>{code}</code>\n⏳ Вхожу в новую сессию...")

        # Входим
        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=mig.phone_code_hash)
            await status.edit_text("✅ Вход выполнен! Формирую файлы...")
            await finalize(message, state, mig)

        except SessionPasswordNeededError:
            await state.set_state(MigrateStates.waiting_password)
            await status.edit_text(
                "🔒 На аккаунте стоит облачный пароль (2FA).\n\n"
                "Отправь пароль:",
                reply_markup=cancel_kb(),
            )

        except Exception as e:
            logger.exception("Ошибка sign_in")
            await status.edit_text(f"❌ Ошибка входа: <code>{e}</code>")
            await cleanup(chat_id, state)

    except Exception as e:
        logger.exception(f"Ошибка обработки сессии {chat_id}")
        await status.edit_text(f"❌ Ошибка: <code>{e}</code>")
        await cleanup(chat_id, state)


@router.message(MigrateStates.waiting_password)
async def process_2fa(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = (message.text or "").strip()

    mig = active.get(chat_id)
    if not mig or not mig.new_client:
        await message.answer("Сессия потеряна. /start")
        return

    if not password:
        await message.answer("❌ Пароль пустой. Попробуй ещё раз:", reply_markup=cancel_kb())
        return

    status = await message.answer("⏳ Проверяю пароль...")

    try:
        await mig.new_client.sign_in(password=password)
        await status.edit_text("✅ 2FA пройден! Формирую файлы...")
        await finalize(message, state, mig)
    except PasswordHashInvalidError:
        await status.edit_text("❌ Неверный пароль. Попробуй ещё раз:", reply_markup=cancel_kb())
    except Exception as e:
        logger.exception("Ошибка 2FA")
        await status.edit_text(f"❌ Ошибка: <code>{e}</code>")
        await cleanup(chat_id, state)


@router.message(MigrateStates.waiting_session)
async def wrong_content(message: Message):
    await message.answer("📎 Отправь именно файл <code>.session</code>", reply_markup=cancel_kb())


# ====================== АДМИН ======================

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Запусков: {stats['started']}\n"
        f"Успешных: {stats['success']}\n"
        f"Ошибок: {stats['failed']}\n"
        f"Отменено: {stats['cancelled']}\n"
        f"Активных: {len(active)}"
    )


# ====================== ЗАПУСК ======================

async def main():
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(periodic_cleanup())

    logger.info("Session Migrator Bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
