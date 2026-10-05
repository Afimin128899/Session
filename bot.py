#!/usr/bin/env python3
"""
Telegram Session Migrator & Bulk Cleaner v5.0 Enterprise
+ Приём ZIP-пакетов (массовая загрузка десятков сессий одним архивом)
+ Авто-подхват паролей 2FA из прикрепленных .json файлов
+ Визуальный прогресс-бар очистки [██████░░░░]
+ Полное уничтожение следов: профиль, аватарки, @username, контакты, боты, ЛС, папки, стикеры
+ Privacy Reset: принудительное скрытие номера и онлайна
+ Защита от параллельных гонок (asyncio.Lock) и пропуск зависших сессий
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
    SetPrivacyRequest,
    UpdateProfileRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.auth import ResetAuthorizationsRequest
from telethon.tl.functions.channels import DeleteChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import BlockRequest, DeleteContactsRequest, GetContactsRequest
from telethon.tl.functions.messages import (
    ClearAllDraftsRequest,
    DeleteChatUserRequest,
    DeleteHistoryRequest,
    GetAllStickersRequest,
    GetDialogFiltersRequest,
    UninstallStickerSetRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.photos import DeletePhotosRequest, GetUserPhotosRequest
from telethon.tl.types import (
    Channel,
    Chat,
    InputPhoto,
    InputPrivacyKeyPhoneNumber,
    InputPrivacyKeyStatusTimestamp,
    InputPrivacyValueDisallowAll,
    InputStickerSetID,
    User,
)


# ====================== КОНФИГУРАЦИЯ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str

    admin_ids: Set[int] = Field(default_factory=set)
    session_timeout_minutes: int = 15
    max_queue_per_user: int = 50
    default_auto_code: bool = True

    # SOCKS5 / HTTP прокси (формат: socks5://login:pass@host:port)
    proxy_protocol: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    device_model: str = "Desktop-PC"
    system_version: str = "Windows 11"
    app_version: str = "5.3.2 x64"
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
logger = logging.getLogger("MigratorEnterprise")

# ====================== FSM & МОДЕЛИ ======================

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
    auto_2fa_password: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class CleanReport:
    profile_cleared: bool = False
    username_cleared: bool = False
    photos_deleted: int = 0
    contacts_deleted: int = 0
    folders_deleted: int = 0
    stickers_removed: int = 0
    channels_deleted: int = 0
    dialogs_purged: int = 0
    saved_messages_cleared: bool = False
    privacy_secured: bool = False
    drafts_cleared: bool = False
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
    auto_2fa_password: Optional[str] = None
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: float = field(default_factory=time.time)
    status_message: Optional[Message] = None
    waiting_2fa: bool = False
    waiting_manual_code: bool = False
    skip_requested: bool = False
    queue_item: Optional[QueueItem] = None
    is_new_session: bool = False


user_auto_mode: Dict[int, bool] = {}
user_queues: Dict[int, List[QueueItem]] = {}
user_locks: Dict[int, asyncio.Lock] = {}
current_migration: Dict[int, ActiveMigration] = {}
queue_workers: Dict[int, bool] = {}

stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0, "skipped": 0}
# ====================== ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ ======================

def get_user_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in user_locks:
        user_locks[chat_id] = asyncio.Lock()
    return user_locks[chat_id]


def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Генерирует визуальный прогресс-бар вида [████░░░░░░] 40%"""
    if total <= 0:
        return "░" * length + " 0%"
    ratio = min(1.0, max(0.0, current / total))
    filled = int(length * ratio)
    return "█" * filled + "░" * (length - filled) + f" {int(ratio * 100)}%"


class StatusReporter:
    def __init__(self, message: Message, interval: float = 2.0):
        self.message = message
        self.interval = interval
        self.last_update = 0.0
        self.last_text = ""

    async def update(self, text: str, force: bool = False, reply_markup: Optional[InlineKeyboardMarkup] = None):
        now = time.time()
        if not force and (now - self.last_update < self.interval or text == self.last_text):
            return
        try:
            await self.message.edit_text(text, reply_markup=reply_markup)
            self.last_update = now
            self.last_text = text
        except Exception:
            pass


def extract_telegram_code(text: str) -> Optional[str]:
    if not text:
        return None
    clean = text.replace("\u200b", "").replace("\u200c", "").replace("\xa0", " ")

    match = re.search(
        r"(?i)(?:code|код|c[oó]digo|passcode|kod|login code)[\D]{0,10}(\d[\d\s\-]{3,6}\d)",
        clean,
    )
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        if 4 <= len(digits) <= 8 and digits != "777000":
            return digits

    for num in re.findall(r"\b\d{5,6}\b", clean):
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
            InlineKeyboardButton(text=f"Перехват: {mode_text}", callback_data="toggle_mode"),
            InlineKeyboardButton(text="📱 Сессия с нуля", callback_data="btn_new_session"),
        ],
        [
            InlineKeyboardButton(text="❌ Сброс очереди / отмена", callback_data="cancel_all"),
        ]
    ])


def action_kb(can_skip: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if can_skip:
        buttons.append(InlineKeyboardButton(text="⏭ Пропустить эту", callback_data="skip_current"))
    buttons.append(InlineKeyboardButton(text="❌ Отменить всё", callback_data="cancel_all"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


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


# ====================== ПОЛНЫЙ МОДУЛЬ ОЧИСТКИ ======================

async def deep_clean_account(client: TelegramClient, reporter: StatusReporter) -> CleanReport:
    report = CleanReport()

    # 1. Профиль и Username
    await reporter.update(f"🧹 Очистка профиля...\n{render_progress_bar(1, 7)}", force=True)
    try:
        await client(UpdateProfileRequest(first_name=".", last_name="", about=""))
        report.profile_cleared = True
    except Exception as e:
        logger.warning(f"Ошибка сброса профиля: {e}")

    try:
        me = await client.get_me()
        if getattr(me, "username", None):
            await client(UpdateUsernameRequest(username=""))
            report.username_cleared = True
    except Exception as e:
        logger.warning(f"Ошибка удаления username: {e}")

    # 2. Аватарки
    await reporter.update(f"🧹 Удаление аватарок...\n{render_progress_bar(2, 7)}")
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
        logger.warning(f"Ошибка удаления фото: {e}")

    # 3. Приватность (номер и онлайн скрыть от всех)
    try:
        await client(SetPrivacyRequest(key=InputPrivacyKeyPhoneNumber(), rules=[InputPrivacyValueDisallowAll()]))
        await client(SetPrivacyRequest(key=InputPrivacyKeyStatusTimestamp(), rules=[InputPrivacyValueDisallowAll()]))
        report.privacy_secured = True
    except Exception as e:
        logger.warning(f"Ошибка приватности: {e}")

    # 4. Облачные контакты
    await reporter.update(f"🧹 Стирание контактов...\n{render_progress_bar(3, 7)}")
    try:
        contacts_res = await client(GetContactsRequest(hash=0))
        users = getattr(contacts_res, "users", [])
        if users:
            await client(DeleteContactsRequest(id=users))
            report.contacts_deleted = len(users)
    except Exception as e:
        logger.warning(f"Ошибка удаления контактов: {e}")

    # 5. Папки чатов и стикеры
    await reporter.update(f"🧹 Удаление папок и стикеров...\n{render_progress_bar(4, 7)}")
    try:
        filters = await client(GetDialogFiltersRequest())
        for f in getattr(filters, "filters", []):
            fid = getattr(f, "id", None)
            if fid is not None and fid != 0:
                await client(UpdateDialogFilterRequest(id=fid, filter=None))
                report.folders_deleted += 1
    except Exception as e:
        logger.warning(f"Ошибка удаления папок: {e}")

    try:
        stickers = await client(GetAllStickersRequest(hash=0))
        for sset in getattr(stickers, "sets", []):
            try:
                await client(UninstallStickerSetRequest(stickerset=InputStickerSetID(id=sset.id, access_hash=sset.access_hash)))
                report.stickers_removed += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Ошибка стикеров: {e}")

    # 6. Черновики и Избранное
    try:
        await client(ClearAllDraftsRequest())
        report.drafts_cleared = True
    except Exception:
        pass

    try:
        await client(DeleteHistoryRequest(peer="me", max_id=0, just_clear=False, revoke=True))
        report.saved_messages_cleared = True
    except Exception:
        pass

    # 7. Личные переписки, группы, боты и каналы
    await reporter.update(f"🧹 Массовая очистка чатов...\n{render_progress_bar(5, 7)}", force=True)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        try:
            if isinstance(entity, Channel):
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
                await reporter.update(
                    f"🧹 Очищено диалогов: <b>{report.dialogs_purged}</b>\n"
                    f"{render_progress_bar(6, 7)}"
                )
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(0.25)

        except FloodWaitError as e:
            await reporter.update(f"⏳ FloodWait: ожидание {e.seconds} с...", force=True)
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            continue

    try:
        await client(ResetWebAuthorizationsRequest())
        report.web_auths_reset = True
    except Exception:
        pass

    await reporter.update(f"✅ Очистка завершена!\n{render_progress_bar(7, 7)}", force=True)
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
                logger.warning(f"Ошибка logout старой сессии: {e}")

        # Завершение остальных устройств
        try:
            await client(ResetAuthorizationsRequest())
            clean_info.sessions_reset = True
        except FreshResetAuthorisationForbiddenError:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = "Сессия младше 24ч (лимит Telegram)"
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
            f"🆔 ID: <code>{metadata['id']}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 Профиль и @username: <b>{'Очищены' if clean_info.profile_cleared else 'Без изменений'}</b>\n"
            f"🖼 Удалено аватарок: <b>{clean_info.photos_deleted}</b>\n"
            f"📇 Удалено контактов: <b>{clean_info.contacts_deleted}</b>\n"
            f"📁 Папок: <b>{clean_info.folders_deleted}</b> | Стикеров: <b>{clean_info.stickers_removed}</b>\n"
            f"👑 Удалено каналов создателя: <b>{clean_info.channels_deleted}</b>\n"
            f"🗑 Очищено диалогов/ботов/групп: <b>{clean_info.dialogs_purged}</b>\n"
            f"🔒 Приватность: <b>{'Номер и статус скрыты' if clean_info.privacy_secured else 'Пропущено'}</b>\n"
            f"🌐 Web/MiniApp сессии: <b>{'Сброшены' if clean_info.web_auths_reset else 'Нет'}</b>\n"
            f"📱 Другие устройства: {reset_status}"
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
        f"⏳ Начинаю: <b>{item.original_name}</b>\n"
        f"Режим перехвата: <b>{'Авто' if auto_mode else 'Вручную'}</b>\n"
        f"2FA из JSON: <b>{'Да' if item.auto_2fa_password else 'Нет'}</b>",
        reply_markup=action_kb(can_skip=True),
    )

    mig = ActiveMigration(
        id=mig_id,
        chat_id=chat_id,
        status_message=status,
        queue_item=item,
        auto_2fa_password=item.auto_2fa_password,
    )
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
            await reporter.update(f"❌ <b>{item.original_name}</b>: нет номера.", force=True)
            await cleanup_migration(mig)
            return

        phone = f"+{phone}" if not str(phone).startswith("+") else str(phone)
        mig.phone = phone
        mig.old_client = old_client

        await reporter.update(
            f"📱 Сессия: <code>{phone}</code>\n⏳ Запрашиваю код подтверждения...",
            force=True,
            reply_markup=action_kb(can_skip=True),
        )

        new_client = get_telethon_client(new_session_path)
        await new_client.connect()
        mig.new_client = new_client

        request_ts = int(time.time()) - 5
        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash
        mig.last_activity = time.time()

        if auto_mode:
            await reporter.update(
                f"📩 Код запрошен на <code>{phone}</code>. Опрашиваю 777000...",
                force=True,
                reply_markup=action_kb(can_skip=True),
            )
            code = None
            start_wait = time.time()

            while time.time() - start_wait < 90:
                if mig.skip_requested:
                    await cleanup_migration(mig, state)
                    return

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
                    logger.debug(f"Ошибка 777000: {e}")

                await asyncio.sleep(1.5)

            if not code:
                await reporter.update(
                    f"❌ Код от 777000 для <code>{phone}</code> не получен за 90 сек.\n"
                    "Попробуйте ручной режим: /manual",
                    force=True,
                )
                await cleanup_migration(mig)
                return

        else:
            mig.waiting_manual_code = True
            await state.set_state(MigrateStates.waiting_manual_code)
            await status.edit_text(
                f"📩 Код отправлен на <code>{phone}</code>\n\nОтправьте код подтверждения в чат:",
                reply_markup=action_kb(can_skip=True),
            )
            return

        await reporter.update(
            f"🔑 Код получен: <code>{code}</code>\n⏳ Выполняю вход...",
            force=True,
            reply_markup=action_kb(can_skip=False),
        )

        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=mig.phone_code_hash)
            await finalize(mig, status, state)

        except SessionPasswordNeededError:
            # Если пароль найден в прикрепленном .json — вводим автоматически!
            if mig.auto_2fa_password:
                await reporter.update("🔒 Пароль 2FA найден в JSON! Ввожу автоматически...", force=True)
                try:
                    await new_client.sign_in(password=mig.auto_2fa_password)
                    await finalize(mig, status, state)
                    return
                except PasswordHashInvalidError:
                    await reporter.update("⚠️ Пароль 2FA из JSON оказался неверным. Введите вручную:", force=True)

            mig.waiting_2fa = True
            mig.last_activity = time.time()
            await state.set_state(MigrateStates.waiting_password)
            await status.edit_text(
                f"🔑 Код <code>{code}</code> принят!\n"
                "🔒 Обнаружен пароль двухфакторной аутентификации (2FA). Отправьте его:",
                reply_markup=action_kb(can_skip=True),
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
# ====================== ОБРАБОТЧИК ОЧЕРЕДИ ======================

async def queue_worker(chat_id: int, bot: Bot, state: FSMContext):
    lock = get_user_lock(chat_id)
    if lock.locked():
        return

    async with lock:
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
                await bot.send_message(chat_id, f"📋 В очереди осталось сессий: <b>{remaining}</b>")

            await process_one_session(item, bot, state)

            mig = current_migration.get(chat_id)
            if mig and (mig.waiting_2fa or mig.waiting_manual_code):
                queue_workers[chat_id] = False
                return

            await asyncio.sleep(1.5)

        queue_workers[chat_id] = False
        await bot.send_message(chat_id, "✅ Вся очередь успешно обработана!")


# ====================== ХЕНДЛЕРЫ ======================

router = Router()


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_user_queue(chat_id, state)

    await message.answer(
        "⚡ <b>Session Cleaner & Migrator v5.0 Enterprise</b>\n\n"
        "Отправляйте файлы <code>.session</code> или <b>целые ZIP-архивы</b> с сессиями и JSON-файлами!\n\n"
        "<b>Возможности:</b>\n"
        "• 📦 <b>Пакетный ZIP-импорт:</b> Распаковка десятков сессий за раз\n"
        "• 🔑 <b>Авто-2FA:</b> Автоматический ввод пароля из .json\n"
        "• 📊 <b>Живой прогресс-бар:</b> Контроль очистки в реальном времени\n"
        "• 🔒 <b>Privacy Reset:</b> Полное скрытие номера и онлайна\n"
        "• 🧹 <b>Полная зачистка:</b> Профиль, @username, фото, контакты, папки, каналы, боты",
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
    await message.answer("✅ Включен автоматический перехват кода.", reply_markup=main_menu_kb(message.chat.id))


@router.message(Command("manual"))
async def cmd_manual(message: Message):
    user_auto_mode[message.chat.id] = False
    await message.answer("✅ Включен ручной ввод кода.", reply_markup=main_menu_kb(message.chat.id))


@router.callback_query(F.data == "skip_current")
async def cb_skip_current(call: CallbackQuery, state: FSMContext):
    chat_id = call.from_user.id
    mig = current_migration.get(chat_id)
    if not mig:
        await call.answer("Нет активной сессии для пропуска.", show_alert=True)
        return

    mig.skip_requested = True
    stats["skipped"] += 1
    await call.answer("Сессия пропущена. Переход к следующей...")
    await cleanup_migration(mig, state)

    if user_queues.get(chat_id):
        asyncio.create_task(queue_worker(chat_id, call.bot, state))


@router.message(Command("new", "create"))
@router.callback_query(F.data == "btn_new_session")
async def cmd_new(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id

    if chat_id in current_migration or user_queues.get(chat_id):
        text = "❌ Идет обработка. Дождитесь окончания или отмените её."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    await state.set_state(MigrateStates.waiting_phone)
    prompt = "📱 Отправьте номер телефона в международном формате (например, <code>+79001234567</code>):"
    if isinstance(event, CallbackQuery):
        await event.message.answer(prompt, reply_markup=action_kb(can_skip=False))
        await event.answer()
    else:
        await event.answer(prompt, reply_markup=action_kb(can_skip=False))


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
        await status.edit_text(f"📩 Код отправлен на <code>{phone}</code>. Отправьте его:", reply_markup=action_kb(can_skip=False))

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

    text = "🛑 Очередь сброшена, все операции остановлены."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=main_menu_kb(chat_id))
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu_kb(chat_id))


# ====================== ОБРАБОТКА ФАЙЛОВ И ZIP-АРХИВОВ ======================

@router.message(F.document)
async def process_document_upload(message: Message, state: FSMContext):
    chat_id = message.chat.id
    doc: Document = message.document
    fname = (doc.file_name or "").lower()

    if not (fname.endswith(".session") or fname.endswith(".zip")):
        await message.answer("❌ Принимаются файлы <code>.session</code> или <b>.zip архивы</b> с ними.")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix=f"upload_{chat_id}_"))
    archive_file = temp_dir / doc.file_name
    await message.bot.download(doc, destination=archive_file)

    added_items: List[QueueItem] = []

    if fname.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_file, "r") as zf:
                zf.extractall(temp_dir)

            sessions = list(temp_dir.rglob("*.session"))
            if not sessions:
                await message.answer("❌ В архиве не найдено ни одного <code>.session</code> файла.")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return

            for s_path in sessions:
                # Поиск парного json для авто-2FA
                json_path = s_path.with_suffix(".json")
                auto_pass = None
                if json_path.exists():
                    try:
                        j_data = json.loads(json_path.read_text(encoding="utf-8"))
                        auto_pass = j_data.get("twoFA") or j_data.get("password") or j_data.get("2fa")
                    except Exception:
                        pass

                added_items.append(QueueItem(
                    id=str(uuid.uuid4())[:8],
                    chat_id=chat_id,
                    file_path=s_path,
                    original_name=s_path.name,
                    auto_2fa_password=str(auto_pass) if auto_pass else None,
                ))

        except Exception as e:
            await message.answer(f"❌ Ошибка чтения ZIP-архива: {e}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
    else:
        added_items.append(QueueItem(
            id=str(uuid.uuid4())[:8],
            chat_id=chat_id,
            file_path=archive_file,
            original_name=doc.file_name,
        ))

    current_queue = user_queues.setdefault(chat_id, [])
    if len(current_queue) + len(added_items) > settings.max_queue_per_user:
        await message.answer(f"❌ Превышен лимит очереди (макс. {settings.max_queue_per_user})")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    current_queue.extend(added_items)
    stats["started"] += len(added_items)

    await message.answer(
        f"📥 Добавлено в очередь сессий: <b>{len(added_items)}</b>\n"
        f"Всего в очереди: <b>{len(current_queue)}</b>",
        reply_markup=action_kb(can_skip=True),
    )

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
        if mig.auto_2fa_password:
            try:
                await mig.new_client.sign_in(password=mig.auto_2fa_password)
                mig.waiting_manual_code = False
                await state.clear()
                await finalize(mig, status, state)
                if user_queues.get(chat_id):
                    asyncio.create_task(queue_worker(chat_id, message.bot, state))
                return
            except Exception:
                pass

        mig.waiting_manual_code = False
        mig.waiting_2fa = True
        mig.last_activity = time.time()
        await state.set_state(MigrateStates.waiting_password)
        await status.edit_text("🔒 Аккаунт защищен 2FA-паролем. Отправьте его:", reply_markup=action_kb(can_skip=True))
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
        f"• Пропущено: {stats['skipped']}\n"
        f"• Ошибок: {stats['failed']}\n"
        f"• Отменено: {stats['cancelled']}\n"
        f"• В очереди прямо сейчас: {total_queue}"
    )


# ====================== ФОНОВЫЙ ТАЙМАУТ-ВОТЧДОГ ======================

async def watchdog_task(bot: Bot):
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

    logger.info("Bot v5.0 Enterprise успешно запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
