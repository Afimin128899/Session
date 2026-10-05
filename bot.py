#!/usr/bin/env python3
"""
Telegram Session Migrator Bot v3.0 Ultimate
+ Полная очистка: профиль, аватарки, @username, контакты, боты, ЛС, папки, каналы, Избранное
+ Удаление собственных каналов создателя (DeleteChannelRequest)
+ Сброс веб-сессий и авторизаций WebApp
+ Надёжный авто-перехват кода из 777000 без потерь
+ Фоновый таймаут-вотчдог неактивных сессий
+ Защита от Telegram 429 и детальный отчет
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
from datetime import datetime
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
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    SessionPasswordNeededError,
    UserDeactivatedError,
)
from telethon.tl.functions.account import (
    ResetWebAuthorizationsRequest,
    UpdateProfileRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.tl.functions.channels import DeleteChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import BlockRequest, DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.messages import (
    DeleteChatUserRequest,
    DeleteHistoryRequest,
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.photos import DeletePhotosRequest, GetUserPhotosRequest
from telethon.tl.types import Channel, Chat, InputPhoto, User


# ====================== КОНФИГУРАЦИЯ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str

    admin_ids: Set[int] = Field(default_factory=set)
    session_timeout_minutes: int = 15
    max_queue_per_user: int = 15
    default_auto_code: bool = True

    # Прокси (необязательно: socks5, socks4 или http)
    proxy_protocol: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    device_model: str = "Desktop-PC"
    system_version: str = "Windows 11"
    app_version: str = "5.3.0 x64"
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

    @property
    def telethon_proxy(self) -> Optional[dict[str, Any]]:
        if self.proxy_host and self.proxy_port and self.proxy_protocol:
            return {
                "proxy_type": self.proxy_protocol.lower(),
                "addr": self.proxy_host,
                "port": self.proxy_port,
                "username": self.proxy_username,
                "password": self.proxy_password,
            }
        return None


settings = Settings()

# ====================== ЛОГИРОВАНИЕ ======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("MigratorCore")

# ====================== FSM & СТРУКТУРЫ ======================

class MigrateStates(StatesGroup):
    waiting_phone = State()
    waiting_password = State()
    waiting_manual_code = State()


@dataclass
class QueueItem:
    id: str
    chat_id: int
    file_path: Path
    original_name: str
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class CleanReport:
    profile_cleared: bool = False
    username_cleared: bool = False
    photos_deleted: int = 0
    contacts_deleted: int = 0
    folders_deleted: int = 0
    channels_deleted: int = 0
    dialogs_purged: int = 0
    saved_messages_cleared: bool = False
    web_auths_reset: bool = False
    sessions_reset: bool = False
    sessions_reset_warning: Optional[str] = None


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
    last_activity: float = field(default_factory=time.time)
    code_event: Optional[asyncio.Event] = None
    received_code: Optional[str] = None
    status_message: Optional[Message] = None
    waiting_2fa: bool = False
    waiting_manual_code: bool = False
    queue_item: Optional[QueueItem] = None
    is_new_session: bool = False


user_auto_mode: Dict[int, bool] = {}
user_queues: Dict[int, List[QueueItem]] = {}
current_migration: Dict[int, ActiveMigration] = {}
queue_workers: Dict[int, bool] = {}

stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0}
# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================

class StatusReporter:
    """Безопасное обновление статуса без превышения лимитов Telegram Bot API."""
    def __init__(self, message: Message, interval: float = 2.0):
        self.message = message
        self.interval = interval
        self.last_update = 0.0
        self.last_text = ""

    async def update(self, text: str, force: bool = False):
        now = time.time()
        if not force and (now - self.last_update < self.interval or text == self.last_text):
            return
        try:
            await self.message.edit_text(text)
            self.last_update = now
            self.last_text = text
        except Exception:
            pass


def extract_telegram_code(text: str) -> Optional[str]:
    """Надёжное извлечение 5-6 значного кода Telegram из текста уведомления."""
    if not text:
        return None

    # Поиск по ключевым фразам Telegram: "код: 12345", "Login code: 12345"
    match = re.search(r"(?:code|код|login code)[:\s]+(\d{5,6})", text, re.IGNORECASE)
    if match:
        return match.group(1)

    # Поиск изолированных чисел (исключая служебный ID отправителя 777000)
    numbers = re.findall(r"\b\d{5,6}\b", text)
    for num in numbers:
        if num != "777000":
            return num

    return None


def get_telethon_client(session_target: Any) -> TelegramClient:
    return TelegramClient(
        session_target,
        settings.api_id,
        settings.api_hash,
        proxy=settings.telethon_proxy,
        device_model=settings.device_model,
        system_version=settings.system_version,
        app_version=settings.app_version,
        lang_code=settings.lang_code,
        system_lang_code=settings.system_lang_code,
    )


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def get_auto_mode(chat_id: int) -> bool:
    return user_auto_mode.get(chat_id, settings.default_auto_code)


def main_menu_kb(chat_id: int) -> InlineKeyboardMarkup:
    mode_text = "🟢 Авто" if get_auto_mode(chat_id) else "🔵 Ручной"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"Перехват кода: {mode_text}", callback_data="toggle_mode"),
            InlineKeyboardButton(text="📱 Сессия с нуля", callback_data="btn_new_session"),
        ],
        [
            InlineKeyboardButton(text="❌ Сброс очереди / отмена", callback_data="cancel_all"),
        ]
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Прервать процесс", callback_data="cancel_all")]
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

    if state and (mig.waiting_2fa or mig.waiting_manual_code):
        await state.clear()


async def clear_user_queue(chat_id: int, state: Optional[FSMContext] = None):
    if chat_id in current_migration:
        await cleanup_migration(current_migration[chat_id], state)

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


# ====================== ГЛУБОКАЯ ОЧИСТКА АККАУНТА ======================

async def deep_clean_account(client: TelegramClient, reporter: StatusReporter) -> CleanReport:
    report = CleanReport()

    # 1. Профиль и @username
    await reporter.update("🧹 [1/6] Сброс профиля и освобождение @username...", force=True)
    try:
        await client(UpdateProfileRequest(first_name=".", last_name="", about=""))
        report.profile_cleared = True
    except Exception as e:
        logger.warning(f"Сброс профиля: {e}")

    try:
        me = await client.get_me()
        if getattr(me, "username", None):
            await client(UpdateUsernameRequest(username=""))
            report.username_cleared = True
    except Exception as e:
        logger.warning(f"Удаление username: {e}")

    # 2. Аватарки
    await reporter.update("🧹 [2/6] Удаление истории аватарок...")
    try:
        photos = await client(GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=100))
        if photos.photos:
            input_photos = [
                InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
                for p in photos.photos
            ]
            await client(DeletePhotosRequest(id=input_photos))
            report.photos_deleted = len(input_photos)
    except Exception as e:
        logger.warning(f"Удаление аватарок: {e}")

    # 3. Контакты
    await reporter.update("🧹 [3/6] Удаление контактов из облака...")
    try:
        contacts_res = await client(GetContactsRequest(hash=0))
        users = getattr(contacts_res, "users", [])
        if users:
            await client(DeleteContactsRequest(id=users))
            report.contacts_deleted = len(users)
    except Exception as e:
        logger.warning(f"Удаление контактов: {e}")

    # 4. Папки с чатами (Filters)
    await reporter.update("🧹 [4/6] Удаление папок диалогов...")
    try:
        filters = await client(GetDialogFiltersRequest())
        for f in getattr(filters, "filters", []):
            fid = getattr(f, "id", None)
            if fid is not None and fid != 0:
                await client(UpdateDialogFilterRequest(id=fid, filter=None))
                report.folders_deleted += 1
    except Exception as e:
        logger.warning(f"Удаление папок: {e}")

    # 5. Очистка «Избранного» (Saved Messages)
    try:
        await client(DeleteHistoryRequest(peer="me", max_id=0, just_clear=False, revoke=True))
        report.saved_messages_cleared = True
    except Exception as e:
        logger.warning(f"Очистка Избранного: {e}")

    # 6. Все диалоги, группы, боты и каналы
    await reporter.update("🧹 [6/6] Полная зачистка всех диалогов...", force=True)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        try:
            if isinstance(entity, Channel):
                # Если создатель канала — удаляем полностью, иначе просто покидаем
                if getattr(entity, "creator", False):
                    try:
                        await client(DeleteChannelRequest(channel=entity))
                        report.channels_deleted += 1
                    except Exception:
                        await client(LeaveChannelRequest(entity))
                else:
                    await client(LeaveChannelRequest(entity))
                report.dialogs_purged += 1

            elif isinstance(entity, Chat):
                await client(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
                report.dialogs_purged += 1

            elif isinstance(entity, User):
                if getattr(entity, "is_self", False):
                    continue
                if getattr(entity, "bot", False):
                    try:
                        await client(BlockRequest(id=entity))
                    except Exception:
                        pass
                try:
                    await client(DeleteHistoryRequest(peer=entity, max_id=0, just_clear=False, revoke=True))
                except Exception:
                    await client.delete_dialog(entity)
                report.dialogs_purged += 1

            if report.dialogs_purged % 10 == 0:
                await reporter.update(f"🧹 [6/6] Стерто диалогов: <b>{report.dialogs_purged}</b>...")
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(0.25)

        except FloodWaitError as e:
            await reporter.update(f"⏳ FloodWait: ожидание {e.seconds} с...", force=True)
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            continue

    # Сброс авторизованных сайтов и Mini Apps
    try:
        await client(ResetWebAuthorizationsRequest())
        report.web_auths_reset = True
    except Exception:
        pass

    return report
# ====================== ФИНАЛИЗАЦИЯ И МЕТАДАННЫЕ ======================

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
        "username": me.username or "",
        "is_premium": bool(getattr(me, "premium", False)),
        "has_profile_pic": False,
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
    reporter = StatusReporter(mig.status_message or message)

    try:
        if not client or not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Клиент не авторизован")

        clean_info = await deep_clean_account(client, reporter)

        if mig.old_client:
            try:
                if mig.old_client.is_connected():
                    await mig.old_client.log_out()
            except Exception as e:
                logger.warning(f"log_out старого клиента: {e}")

        # Сброс остальных авторизованных сессий
        try:
            await client(ResetAuthorizationsRequest())
            clean_info.sessions_reset = True
        except FreshResetAuthorisationForbiddenError:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = "Сессия младше 24ч (ограничение Telegram)"
        except Exception as e:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = str(e)

        metadata = await create_metadata(client, phone)
        await client.disconnect()
        mig.new_client = None

        session_file = temp_dir / f"{phone}.session"
        original = Path(str(temp_dir / "new_session") + ".session")
        if original.exists():
            original.rename(session_file)

        if not session_file.exists():
            raise FileNotFoundError("Файл сессии не найден")

        meta_file = temp_dir / f"{phone}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = temp_dir / f"{phone}_clean.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_file, arcname=f"{phone}.session")
            zf.write(meta_file, arcname=f"{phone}.json")

        data = zip_path.read_bytes()
        reset_status = "✅ Сброшены" if clean_info.sessions_reset else f"⚠️ {clean_info.sessions_reset_warning}"

        caption = (
            f"📦 <b>Сессия очищена и упакована</b>\n\n"
            f"📞 Телефон: <code>{phone}</code>\n"
            f"🆔 Telegram ID: <code>{metadata['id']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 Профиль / @username: <b>{'Очищены' if clean_info.profile_cleared else 'Без изменений'}</b>\n"
            f"🖼 Удалено аватарок: <b>{clean_info.photos_deleted}</b>\n"
            f"📇 Удалено контактов: <b>{clean_info.contacts_deleted}</b>\n"
            f"📁 Удалено папок: <b>{clean_info.folders_deleted}</b>\n"
            f"👑 Удалено каналов создателя: <b>{clean_info.channels_deleted}</b>\n"
            f"🗑 Очищено диалогов/ботов/групп: <b>{clean_info.dialogs_purged}</b>\n"
            f"📝 Избранное очищено: <b>{'Да' if clean_info.saved_messages_cleared else 'Нет'}</b>\n"
            f"🌐 Web/MiniApp сессии: <b>{'Сброшены' if clean_info.web_auths_reset else 'Нет'}</b>\n"
            f"🔒 Другие устройства: {reset_status}"
        )

        await message.answer_document(
            BufferedInputFile(data, filename=f"{phone}_clean.zip"),
            caption=caption,
        )
        stats["success"] += 1

    except Exception as e:
        logger.exception("Ошибка финализации")
        stats["failed"] += 1
        await message.answer(f"❌ Ошибка миграции: <code>{e}</code>")
    finally:
        await cleanup_migration(mig, state)


# ====================== ОБРАБОТЧИК СЕССИИ ======================

async def process_one_session(item: QueueItem, bot: Bot, state: FSMContext):
    chat_id = item.chat_id
    mig_id = str(uuid.uuid4())[:8]
    auto_mode = get_auto_mode(chat_id)

    status = await bot.send_message(
        chat_id,
        f"⏳ Начинаю: <b>{item.original_name}</b>\nРежим перехвата: <b>{'Авто' if auto_mode else 'Вручную'}</b>"
    )

    mig = ActiveMigration(id=mig_id, chat_id=chat_id, status_message=status, queue_item=item)
    current_migration[chat_id] = mig
    reporter = StatusReporter(status)

    try:
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"mig_{chat_id}_{mig_id}_"))
        old_session_path = mig.temp_dir / "old.session"
        new_session_path = str(mig.temp_dir / "new_session")
        shutil.copy(item.file_path, old_session_path)

        old_client = get_telethon_client(str(old_session_path.with_suffix("")))
        await old_client.connect()

        if not await old_client.is_user_authorized():
            await reporter.update(f"❌ <b>{item.original_name}</b>: сессия не авторизована.", force=True)
            await cleanup_migration(mig)
            return

        me = await old_client.get_me()
        phone = me.phone
        if not phone:
            await reporter.update(f"❌ <b>{item.original_name}</b>: отсутствует привязанный номер.", force=True)
            await cleanup_migration(mig)
            return

        phone = f"+{phone}" if not str(phone).startswith("+") else str(phone)
        mig.phone = phone
        mig.old_client = old_client

        await reporter.update(f"📱 Сессия: <code>{phone}</code>\n⏳ Запрашиваю авторизационный код...", force=True)

        new_client = get_telethon_client(new_session_path)
        await new_client.connect()
        mig.new_client = new_client

        # Фиксируем время запроса (с запасом 5 сек), чтобы не взять старый код
        request_ts = int(time.time()) - 5

        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash
        mig.last_activity = time.time()

        if auto_mode:
            await reporter.update("📩 Запрос отправлен. Опрашиваю служебный чат 777000...", force=True)
            code = None
            start_wait = time.time()

            # Опрос входящих сообщений от 777000 каждые 1.5 секунды
            while time.time() - start_wait < 90:
                try:
                    async for msg in old_client.iter_messages(777000, limit=3):
                        msg_time = msg.date.timestamp() if msg.date else 0
                        if msg_time >= request_ts:
                            parsed = extract_telegram_code(msg.raw_text or "")
                            if parsed:
                                code = parsed
                                break
                    if code:
                        break
                except Exception as e:
                    logger.debug(f"Ошибка чтения 777000: {e}")

                await asyncio.sleep(1.5)

            if not code:
                await reporter.update(
                    "❌ Код от 777000 не получен за 90 сек.\n"
                    "Попробуйте ручной режим: /manual",
                    force=True
                )
                await cleanup_migration(mig)
                return

        else:
            mig.waiting_manual_code = True
            await state.set_state(MigrateStates.waiting_manual_code)
            await status.edit_text(
                f"📩 Код отправлен на <code>{phone}</code>\n\nОтправьте код подтверждения в чат:",
                reply_markup=cancel_kb(),
            )
            return

        # Код успешно получен — выводим в статус и авторизуемся
        await reporter.update(f"🔑 Код получен: <code>{code}</code>\n⏳ Выполняю вход...", force=True)

        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=mig.phone_code_hash)
            await finalize(mig, status, state)
        except SessionPasswordNeededError:
            mig.waiting_2fa = True
            mig.last_activity = time.time()
            await state.set_state(MigrateStates.waiting_password)
            await status.edit_text(
                f"🔑 Код <code>{code}</code> принят!\n"
                "🔒 Обнаружен пароль двухфакторной аутентификации (2FA). Отправьте его:",
                reply_markup=cancel_kb()
            )
            return
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            await reporter.update(f"❌ Неверный или истекший код (<code>{code}</code>): {e}", force=True)
            await cleanup_migration(mig)
            return
        except Exception as e:
            await reporter.update(f"❌ Ошибка авторизации: {e}", force=True)
            await cleanup_migration(mig)
            return

    except PhoneNumberBannedError:
        await reporter.update(f"❌ Номер <code>{mig.phone}</code> заблокирован Telegram.", force=True)
        await cleanup_migration(mig)
    except UserDeactivatedError:
        await reporter.update(f"❌ Аккаунт <code>{mig.phone}</code> деактивирован.", force=True)
        await cleanup_migration(mig)
    except Exception as e:
        logger.exception("Ошибка process_one_session")
        await reporter.update(f"❌ Системный сбой: {e}", force=True)
        await cleanup_migration(mig)


async def queue_worker(chat_id: int, bot: Bot, state: FSMContext):
    queue_workers[chat_id] = True
    while True:
        queue = user_queues.get(chat_id, [])
        if not queue:
            break

        item = queue.pop(0)
        if not queue:
            user_queues.pop(chat_id, None)

        remaining = len(user_queues.get(chat_id, []))
        if remaining > 0:
            await bot.send_message(chat_id, f"📋 В очереди файлов: <b>{remaining}</b>")

        await process_one_session(item, bot, state)

        mig = current_migration.get(chat_id)
        if mig and (mig.waiting_2fa or mig.waiting_manual_code):
            queue_workers[chat_id] = False
            return

        await asyncio.sleep(1.5)

    queue_workers[chat_id] = False
    await bot.send_message(chat_id, "✅ Обработка очереди завершена!")
# ====================== ХЕНДЛЕРЫ ДИАЛОГОВ ======================

router = Router()


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_user_queue(chat_id, state)

    await message.answer(
        "⚡ <b>Session Cleaner & Migrator v3.0 Ultimate</b>\n\n"
        "Отправляйте файлы <code>.session</code> в диалог (поддерживается очередь) "
        "или зарегистрируйте чистую сессию с нуля по номеру телефона.\n\n"
        "<b>Комплекс очистки:</b>\n"
        "• Удаление имени, био и @username\n"
        "• Очистка истории аватарок\n"
        "• Удаление облачных контактов\n"
        "• Удаление папок диалогов (Filters)\n"
        "• Безвозвратное удаление каналов, где аккаунт владелец\n"
        "• Очистка личных переписок и блокировка ботов\n"
        "• Полная очистка «Избранного»\n"
        "• Завершение сайтов, Mini Apps и сброс сессий",
        reply_markup=main_menu_kb(chat_id),
    )


@router.callback_query(F.data == "toggle_mode")
async def cb_toggle_mode(call: CallbackQuery):
    chat_id = call.from_user.id
    current = get_auto_mode(chat_id)
    user_auto_mode[chat_id] = not current
    await call.message.edit_reply_markup(reply_markup=main_menu_kb(chat_id))
    await call.answer(f"Режим переключен на: {'Авто' if not current else 'Ручной'}")


@router.message(Command("auto"))
async def cmd_auto(message: Message):
    user_auto_mode[message.chat.id] = True
    await message.answer("✅ Включен авто-перехват кода.", reply_markup=main_menu_kb(message.chat.id))


@router.message(Command("manual"))
async def cmd_manual(message: Message):
    user_auto_mode[message.chat.id] = False
    await message.answer("✅ Включен ручной ввод кода.", reply_markup=main_menu_kb(message.chat.id))


@router.message(Command("new", "create"))
@router.callback_query(F.data == "btn_new_session")
async def cmd_new(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id

    if chat_id in current_migration or user_queues.get(chat_id):
        text = "❌ Идет обработка. Сначала дождитесь окончания или выполните /cancel"
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    await state.set_state(MigrateStates.waiting_phone)
    prompt = "📱 Отправьте номер телефона в международном формате (например, <code>+79001234567</code>):"
    if isinstance(event, CallbackQuery):
        await event.message.answer(prompt, reply_markup=cancel_kb())
        await event.answer()
    else:
        await event.answer(prompt, reply_markup=cancel_kb())


@router.message(MigrateStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    chat_id = message.chat.id
    raw = (message.text or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if raw.startswith("8") and len(raw) == 11:
        raw = "+7" + raw[1:]
    elif not raw.startswith("+"):
        raw = "+" + raw

    if not re.fullmatch(r"\+\d{10,15}", raw):
        await message.answer("❌ Неверный формат. Пример: <code>+79001234567</code>")
        return

    phone = raw
    mig_id = str(uuid.uuid4())[:8]
    status = await message.answer(f"⏳ Создание клиента для <code>{phone}</code>...")

    mig = ActiveMigration(id=mig_id, chat_id=chat_id, phone=phone, status_message=status, is_new_session=True, waiting_manual_code=True)
    current_migration[chat_id] = mig

    try:
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"new_{chat_id}_{mig_id}_"))
        new_client = get_telethon_client(str(mig.temp_dir / "new_session"))
        await new_client.connect()
        mig.new_client = new_client

        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash
        mig.last_activity = time.time()
        stats["started"] += 1

        await state.set_state(MigrateStates.waiting_manual_code)
        await status.edit_text(f"📩 Код отправлен на <code>{phone}</code>. Отправьте код:", reply_markup=cancel_kb())

    except FloodWaitError as e:
        await status.edit_text(f"❌ Telegram FloodWait: подождите {e.seconds} с.")
        await cleanup_migration(mig, state)
    except Exception as e:
        logger.exception("Ошибка запроса кода")
        await status.edit_text(f"❌ Ошибка запроса: {e}")
        await cleanup_migration(mig, state)


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_all")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await clear_user_queue(chat_id, state)
    stats["cancelled"] += 1

    text = "🛑 Все текущие процессы и сессии отменены."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=main_menu_kb(chat_id))
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu_kb(chat_id))


@router.message(F.document)
async def process_session_file(message: Message, state: FSMContext):
    chat_id = message.chat.id
    if await state.get_state() is not None:
        await message.answer("❌ Завершите предыдущее действие или нажмите /cancel")
        return

    doc: Document = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".session"):
        await message.answer("❌ Принимаются исключительно файлы с расширением <code>.session</code>")
        return

    if len(user_queues.get(chat_id, [])) >= settings.max_queue_per_user:
        await message.answer(f"❌ Очередь заполнена (лимит: {settings.max_queue_per_user})")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix=f"queue_{chat_id}_"))
    file_path = temp_dir / doc.file_name
    await message.bot.download(doc, destination=file_path)

    item = QueueItem(id=str(uuid.uuid4())[:8], chat_id=chat_id, file_path=file_path, original_name=doc.file_name)
    user_queues.setdefault(chat_id, []).append(item)

    pos = len(user_queues[chat_id])
    mode = "Авто" if get_auto_mode(chat_id) else "Ручной"
    await message.answer(f"📥 <b>{doc.file_name}</b> добавлен в очередь (#{pos})\nПерехват кода: <b>{mode}</b>", reply_markup=cancel_kb())
    stats["started"] += 1

    if not queue_workers.get(chat_id, False):
        asyncio.create_task(queue_worker(chat_id, message.bot, state))


@router.message(MigrateStates.waiting_manual_code)
async def process_manual_code(message: Message, state: FSMContext):
    chat_id = message.chat.id
    code = (message.text or "").strip().replace(" ", "").replace("-", "")

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_manual_code:
        await message.answer("Нет ожидающих ввода кода процессов.")
        return

    if not code.isdigit() or not (4 <= len(code) <= 8):
        await message.answer("❌ Код должен содержать от 4 до 8 цифр.")
        return

    status = await message.answer("⏳ Авторизация...")
    try:
        await mig.new_client.sign_in(phone=mig.phone, code=code, phone_code_hash=mig.phone_code_hash)
        mig.waiting_manual_code = False
        await state.clear()
        await finalize(mig, status, state)

        if user_queues.get(chat_id):
            asyncio.create_task(queue_worker(chat_id, message.bot, state))

    except SessionPasswordNeededError:
        mig.waiting_manual_code = False
        mig.waiting_2fa = True
        mig.last_activity = time.time()
        await state.set_state(MigrateStates.waiting_password)
        await status.edit_text("🔒 Аккаунт защищен 2FA-паролем. Отправьте его:")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await status.edit_text(f"❌ Неверный или истекший код: {e}")
    except Exception as e:
        await status.edit_text(f"❌ Сбой авторизации: {e}")


@router.message(MigrateStates.waiting_password)
async def process_2fa(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = (message.text or "").strip()

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_2fa:
        await message.answer("Нет ожидающих пароль процессов.")
        return

    status = await message.answer("⏳ Проверка 2FA...")
    try:
        await mig.new_client.sign_in(password=password)
        mig.waiting_2fa = False
        await state.clear()
        await finalize(mig, status, state)

        if user_queues.get(chat_id):
            asyncio.create_task(queue_worker(chat_id, message.bot, state))

    except PasswordHashInvalidError:
        await status.edit_text("❌ Пароль 2FA не подходит. Попробуйте еще раз:")
    except Exception as e:
        await status.edit_text(f"❌ Ошибка 2FA: {e}")
        await cleanup_migration(mig, state)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    total_queue = sum(len(q) for q in user_queues.values())
    await message.answer(
        f"📊 <b>Статистика сервиса</b>\n\n"
        f"• Всего запусков: {stats['started']}\n"
        f"• Успешно очищено: {stats['success']}\n"
        f"• Ошибок обработки: {stats['failed']}\n"
        f"• Отменено пользователем: {stats['cancelled']}\n"
        f"• В очереди прямо сейчас: {total_queue}"
    )


# ====================== ФОНОВЫЙ ТАЙМАУТ-ВОТЧДОГ ======================

async def watchdog_task(bot: Bot):
    """Очищает зависшие сессии, ожидающие ввода кода/2FA дольше лимита."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        timeout_sec = settings.session_timeout_minutes * 60
        stale_chats = []

        for chat_id, mig in list(current_migration.items()):
            if (mig.waiting_2fa or mig.waiting_manual_code) and (now - mig.last_activity > timeout_sec):
                stale_chats.append(chat_id)

        for chat_id in stale_chats:
            mig = current_migration.get(chat_id)
            if mig:
                logger.info(f"Таймаут сессии для chat_id {chat_id}")
                try:
                    await bot.send_message(chat_id, "⌛ Время ожидания кода/2FA истекло. Сессия сброшена.")
                except Exception:
                    pass
                await cleanup_migration(mig)


# ====================== ТОЧКА ВХОДА ======================

async def main():
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(watchdog_task(bot))

    logger.info("Bot v3.0 Ultimate запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
