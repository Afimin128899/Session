#!/usr/bin/env python3
"""
Telegram Session Migrator & Bulk Cleaner v100x+ Extreme Industrial
+ Полная очистка и миграция сессий Telethon
+ Исправлено экранирование спецсимволов HTML для Telegram API
+ Гарантированное завершение старой сессии через LogOutRequest
+ Мультипоточная обработка очередей с использованием SQLite
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import random
import re
import shutil
import socket
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    BotCommand,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from spam_check import LABELS as SPAM_LABELS, SpamResult, check_spam_status
from session_devices import LABELS as DEVICE_LABELS, reset_other_sessions

from telethon import TelegramClient
from telethon.errors.common import TypeNotFoundError
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
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
    SetAccountTTLRequest,
    SetPrivacyRequest,
    UpdateProfileRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.auth import LogOutRequest, ResetAuthorizationsRequest, ResendCodeRequest
from telethon.tl.functions.channels import DeleteChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import (
    BlockRequest,
    DeleteContactsRequest,
    GetContactsRequest,
    ResetSavedRequest,
)
from telethon.tl.functions.messages import (
    ClearAllDraftsRequest,
    DeleteChatUserRequest,
    DeleteHistoryRequest,
    GetAllStickersRequest,
    GetDialogFiltersRequest,
    UninstallStickerSetRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.payments import ClearSavedInfoRequest
from telethon.tl.functions.photos import DeletePhotosRequest, GetUserPhotosRequest
from telethon.tl.types import (
    AccountDaysTTL,
    Channel,
    Chat,
    InputPhoto,
    InputPrivacyKeyChatInvite,
    InputPrivacyKeyForwards,
    InputPrivacyKeyPhoneCall,
    InputPrivacyKeyPhoneNumber,
    InputPrivacyKeyProfilePhoto,
    InputPrivacyKeyStatusTimestamp,
    InputPrivacyValueDisallowAll,
    InputStickerSetID,
    User,
)
from telethon.tl.types.auth import SentCode, SentCodeTypeApp


# ====================== КОНФИГУРАЦИЯ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    api_id: int
    api_hash: str
    bot_token: str

    admin_ids: Annotated[Set[int], NoDecode] = Field(default_factory=set)
    session_timeout_minutes: int = Field(default=15, ge=1, le=1440)
    max_queue_per_user: int = Field(default=150, ge=1, le=10000)
    default_auto_code: int = 1
    concurrent_workers_per_chat: int = Field(default=10, ge=1, le=10)
    concurrent_dialogs_per_session: int = Field(default=10, ge=1, le=10)
    db_file: str = "sessions_v100x.db"

    proxy_protocol: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    @field_validator("concurrent_workers_per_chat", mode="before")
    @classmethod
    def cap_session_workers(cls, value):
        return min(int(value), 10)

    @field_validator("concurrent_dialogs_per_session", mode="before")
    @classmethod
    def cap_dialog_batch(cls, value):
        # Existing .env values of 50–100 must keep working after the rollback.
        return min(int(value), 10)

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if v is None:
            return set()
        if isinstance(v, int):
            return {v}
        if isinstance(v, str):
            if v.strip().startswith("["):
                return {int(x) for x in json.loads(v)}
            return {int(x.strip()) for x in v.split(",") if x.strip()}
        if isinstance(v, (list, set, tuple)):
            return {int(x) for x in v}
        return set()

    @property
    def fallback_proxy(self) -> Optional[dict[str, Any]]:
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("IndustrialWiper")


def public_error(exc: BaseException) -> str:
    """Return a safe, non-sensitive error label for Telegram messages."""
    name = type(exc).__name__
    return html.escape(name.removesuffix("Error") or "ошибка")


def public_archive_error(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        return html.escape(str(exc))
    if isinstance(exc, zipfile.BadZipFile):
        return "архив повреждён или имеет неверный формат"
    return "не удалось безопасно обработать архив"


# ====================== ПУЛ ОТПЕЧАТКОВ (ANTI-BAN) ======================

FINGERPRINTS = [
    {"device": "MacBook Pro 16", "sys": "macOS 14.5", "app": "5.4.1 x64", "lang": "en"},
    {"device": "Lenovo ThinkPad X1", "sys": "Windows 11 Pro", "app": "5.4.0 x64", "lang": "en"},
    {"device": "ASUS ROG Zephyrus", "sys": "Windows 10 Enterprise", "app": "5.3.8 x64", "lang": "en"},
    {"device": "Dell XPS 15", "sys": "Ubuntu 24.04 LTS", "app": "5.3.9 x64", "lang": "en"},
    {"device": "Surface Laptop Studio", "sys": "Windows 11 Home", "app": "5.4.1 x64", "lang": "en"},
    {"device": "HP Spectre x360", "sys": "Windows 11 Pro", "app": "5.4.0 x64", "lang": "en"},
]

def get_random_fingerprint() -> dict[str, str]:
    return random.choice(FINGERPRINTS)


# ====================== БАЗА ДАННЫХ SQLITE ======================

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    file_path TEXT,
                    original_name TEXT,
                    auto_2fa TEXT,
                    proxy_json TEXT,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    auto_mode INTEGER DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interface_settings (
                    chat_id INTEGER PRIMARY KEY,
                    compact_menu INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS upload_limits (
                    chat_id INTEGER PRIMARY KEY,
                    max_sessions INTEGER NOT NULL CHECK (max_sessions > 0)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS result_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    data BLOB NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(result_archives)")}
            if "category" not in columns:
                conn.execute("ALTER TABLE result_archives ADD COLUMN category TEXT NOT NULL DEFAULT 'accounts'")
            conn.commit()

    def save_archive(self, chat_id: int, filename: str, data: bytes, category: str = "accounts"):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO result_archives (chat_id, filename, data, category) VALUES (?, ?, ?, ?)",
                (chat_id, Path(filename).name, data, category),
            )
            return cursor.lastrowid

    def archives(self, chat_id: int, include_sent: bool = False, category: str = "accounts"):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT id, filename, data FROM result_archives WHERE chat_id = ? AND category = ? "
                + ("" if include_sent else "AND delivered = 0 ") + "ORDER BY id",
                (chat_id, category),
            ).fetchall()

    def mark_archives_delivered(self, ids):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("UPDATE result_archives SET delivered = 1 WHERE id = ?", [(i,) for i in ids])

    def add_queue_item(self, item_id: str, chat_id: int, file_path: str, original_name: str, auto_2fa: Optional[str], proxy_dict: Optional[dict]):
        self.add_queue_items(chat_id, [(item_id, file_path, original_name, auto_2fa, proxy_dict)])

    def add_queue_items(self, chat_id: int, items: List[Tuple]):
        """Enforce the quota atomically, including items already processing."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute("SELECT COUNT(*) FROM queue WHERE chat_id = ?", (chat_id,)).fetchone()[0]
            if used + len(items) > self._upload_limit(conn, chat_id):
                raise ValueError("Превышен лимит сессий в очереди. Дождитесь обработки или сбросьте очередь.")
            conn.executemany(
                "INSERT INTO queue (id, chat_id, file_path, original_name, auto_2fa, proxy_json, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                [
                    (item_id, chat_id, file_path, original_name, auto_2fa, json.dumps(proxy_dict) if proxy_dict else None)
                    for item_id, file_path, original_name, auto_2fa, proxy_dict in items
                ],
            )
            conn.commit()

    def _upload_limit(self, conn: sqlite3.Connection, chat_id: int) -> int:
        row = conn.execute("SELECT max_sessions FROM upload_limits WHERE chat_id = ?", (chat_id,)).fetchone()
        return min(row[0], settings.max_queue_per_user) if row else settings.max_queue_per_user

    def get_upload_limit(self, chat_id: int) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return self._upload_limit(conn, chat_id)

    def set_upload_limit(self, chat_id: int, limit: int):
        if not 1 <= limit <= settings.max_queue_per_user:
            raise ValueError(f"Допустимый лимит: от 1 до {settings.max_queue_per_user}.")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO upload_limits (chat_id, max_sessions) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET max_sessions = excluded.max_sessions",
                (chat_id, limit),
            )

    def count_outstanding(self, chat_id: int) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM queue WHERE chat_id = ?", (chat_id,)).fetchone()[0]

    def pop_next_item(self, chat_id: int) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM queue WHERE chat_id = ? AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
                (chat_id,),
            )
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE queue SET status = 'processing' WHERE id = ?", (row["id"],))
                conn.commit()
                return dict(row)
        return None

    def requeue_interrupted(self, chat_id: int):
        """Called only when this chat has no live worker or migration."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE queue SET status = 'pending' WHERE chat_id = ? AND status = 'processing'",
                         (chat_id,))

    def remove_item(self, item_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM queue WHERE id = ?", (item_id,))
            conn.commit()

    def clear_chat_queue(self, chat_id: int) -> List[str]:
        files_to_remove = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT file_path FROM queue WHERE chat_id = ?", (chat_id,))
            files_to_remove = [r["file_path"] for r in cur.fetchall()]
            cur.execute("DELETE FROM queue WHERE chat_id = ?", (chat_id,))
            conn.commit()
        return files_to_remove

    def count_queue(self, chat_id: int) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM queue WHERE chat_id = ? AND status = 'pending'", (chat_id,))
            return cur.fetchone()[0]

    def set_clean_mode(self, chat_id: int, mode: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (chat_id, auto_mode) VALUES (?, ?)", (chat_id, mode))
            conn.commit()

    def get_clean_mode(self, chat_id: int, default: int = 1) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT auto_mode FROM settings WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            return int(row[0]) if row is not None else default

    def compact_menu(self, chat_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT compact_menu FROM interface_settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return bool(row[0]) if row else False

    def toggle_compact_menu(self, chat_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO interface_settings (chat_id, compact_menu) VALUES (?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET compact_menu = 1 - compact_menu""",
                (chat_id,),
            )
            row = conn.execute(
                "SELECT compact_menu FROM interface_settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return bool(row[0])


db = Database(settings.db_file)


# ====================== МОДЕЛИ ДАННЫХ ======================

class MigrateStates(StatesGroup):
    waiting_phone = State()
    waiting_password = State()
    waiting_manual_code = State()


@dataclass
class SessionCheckResult:
    is_valid: bool
    status: str
    user_id: Optional[int] = None
    phone: Optional[str] = None
    username: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CleanReport:
    profile_cleared: bool = False
    username_cleared: bool = False
    photos_deleted: int = 0
    contacts_deleted: int = 0
    saved_contacts_reset: bool = False
    folders_deleted: int = 0
    stickers_removed: int = 0
    channels_deleted: int = 0
    dialogs_purged: int = 0
    dialogs_failed: int = 0
    saved_messages_cleared: bool = False
    privacy_secured: bool = False
    payments_cleared: bool = False
    ttl_set_30d: bool = False
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
    proxy_config: Optional[dict] = None
    fingerprint: dict[str, str] = field(default_factory=get_random_fingerprint)
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: float = field(default_factory=time.time)
    status_message: Optional[Message] = None
    waiting_2fa: bool = False
    waiting_manual_code: bool = False
    skip_requested: bool = False
    is_new_session: bool = False
    queue_db_id: Optional[str] = None
    old_session_terminated: bool = False
    old_session_error: Optional[str] = None


chat_semaphores: Dict[int, asyncio.Semaphore] = {}
current_migration: Dict[int, ActiveMigration] = {}
active_migrations: Dict[str, ActiveMigration] = {}
queue_workers: Dict[int, bool] = {}
queue_worker_tasks: Dict[int, asyncio.Task] = {}
queue_paused: Dict[int, bool] = {}
queue_current_items: Dict[int, str] = {}
archive_locks: Dict[int, asyncio.Lock] = {}
MAX_RESULT_BYTES = 45 * 1024 * 1024
RESULTS_PER_ARCHIVE = 10
ARCHIVE_LABELS = {"accounts": "", **{"spam_" + k: v for k, v in SPAM_LABELS.items()},
                  **{"devices_" + k: v for k, v in DEVICE_LABELS.items()}}


async def flush_queue_archives(chat_id: int, bot: Bot, *, include_sent: bool = False, minimum: int = 1, category: str = "accounts"):
    # Keep successful results on disk even after delivery, for /archives recovery.
    async with archive_locks.setdefault(chat_id, asyncio.Lock()):
        archives = db.archives(chat_id, include_sent, category)
        while len(archives) >= minimum:
            output = io.BytesIO()
            batch = []
            used_names = set()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as bundle:
                for archive_id, name, data in archives[:RESULTS_PER_ARCHIVE]:
                    with zipfile.ZipFile(io.BytesIO(data)) as account:
                        members = [entry for entry in account.infolist()
                                   if not entry.is_dir() and Path(entry.filename).suffix.lower() in {".session", ".json"}]
                        if not members:
                            raise ValueError("В архиве аккаунта нет файлов .session или .json")
                        expanded_size = sum(entry.file_size for entry in members)
                        if batch and output.tell() + expanded_size + 4096 > MAX_RESULT_BYTES:
                            break
                        # Use one suffix for the whole account to keep session/JSON pairs together.
                        suffix = 1
                        while True:
                            names = [f"{Path(entry.filename).stem}{'_' + str(suffix) if suffix > 1 else ''}{Path(entry.filename).suffix}"
                                     for entry in members]
                            if not any(filename.casefold() in used_names for filename in names):
                                break
                            suffix += 1
                        for entry, filename in zip(members, names):
                            bundle.writestr(filename, account.read(entry))
                            used_names.add(filename.casefold())
                    batch.append(archive_id)
            await bot.send_document(
                chat_id=chat_id,
                document=BufferedInputFile(output.getvalue(), filename=f"{category}.zip"),
                caption=(f"📦 {ARCHIVE_LABELS[category]}. " if ARCHIVE_LABELS.get(category) else "📦 ")
                        + f"Аккаунтов в архиве: {len(batch)}. Копии сохранены: /archives.",
            )
            db.mark_archives_delivered(batch)
            archives = archives[len(batch):]


async def try_flush_queue_archives(chat_id: int, bot: Bot, *, include_sent: bool = False, minimum: int = 1, category: str = "accounts"):
    try:
        await flush_queue_archives(chat_id, bot, include_sent=include_sent, minimum=minimum, category=category)
    except Exception:
        logger.exception("Не удалось отправить сохранённые архивы")
        try:
            await bot.send_message(chat_id, "❌ Не удалось отправить ZIP. Результаты сохранены на диске. Повторите отправку: /archives.")
        except Exception:
            logger.exception("Не удалось сообщить об ошибке доставки")


async def deliver_account_archive(chat_id: int, bot: Bot, data: bytes, filename: str,
                                  caption: str, *, queued: bool):
    # Commit before any network request and before the pipeline removes temp files.
    archive_id = db.save_archive(chat_id, filename, data)
    try:
        await bot.send_message(chat_id, caption)
    except Exception:
        logger.exception("Не удалось отправить отчёт; ZIP сохранён")
    if not queued:
        try:
            await bot.send_document(chat_id=chat_id, document=BufferedInputFile(data, filename=filename), caption=caption)
            db.mark_archives_delivered([archive_id])
        except Exception:
            await try_flush_queue_archives(chat_id, bot)
    elif len(db.archives(chat_id)) >= RESULTS_PER_ARCHIVE:
        await try_flush_queue_archives(chat_id, bot, minimum=RESULTS_PER_ARCHIVE)

async def flush_all_archives(chat_id: int, bot: Bot, *, include_sent: bool = False):
    for category in ARCHIVE_LABELS:
        if db.archives(chat_id, include_sent, category):
            await try_flush_queue_archives(chat_id, bot, include_sent=include_sent, category=category)


stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0, "skipped": 0}


# ====================== УТИЛИТЫ И ПРОВЕРКИ ======================

def get_chat_semaphore(chat_id: int) -> asyncio.Semaphore:
    if chat_id not in chat_semaphores:
        chat_semaphores[chat_id] = asyncio.Semaphore(settings.concurrent_workers_per_chat)
    return chat_semaphores[chat_id]


async def verify_proxy_alive(proxy: dict, timeout: float = 3.0) -> bool:
    host = proxy.get("addr")
    port = proxy.get("port")
    if not host or not port:
        return False
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000


def validate_upload_name(name: str) -> str:
    if not name or "\\" in name or "/" in name or "\x00" in name:
        raise ValueError("Недопустимое имя файла.")
    if Path(name).suffix.lower() not in {".session", ".zip"}:
        raise ValueError("Отправьте файл .session или .zip.")
    return name


def extract_session_archive(archive_file: Path, destination: Path, remaining: int) -> List[Path]:
    # Validate every member before writing anything to disk.
    with zipfile.ZipFile(archive_file) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("В архиве слишком много файлов.")
        total = 0
        seen = set()
        sessions = []
        root = destination.resolve()
        for entry in entries:
            name = entry.filename
            path = Path(name)
            if (not name or "\\" in name or "\x00" in name or path.is_absolute()
                    or ".." in path.parts or ":" in name
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Архив содержит недопустимые пути или ссылки.")
            target = (root / path).resolve()
            if not target.is_relative_to(root) or target == root or target in seen:
                raise ValueError("Архив содержит повторяющиеся или недопустимые пути.")
            seen.add(target)
            if entry.flag_bits & 1:
                raise ValueError("Архивы с паролем не поддерживаются.")
            total += entry.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("Распакованный архив превышает 100 МБ.")
            if not entry.is_dir() and path.suffix.lower() == ".session":
                sessions.append(root / path)
        if not sessions:
            raise ValueError("В архиве нет файлов .session.")
        if len(sessions) > remaining:
            raise ValueError("Превышен лимит сессий в очереди.")
        archive.extractall(root)
    return sessions


def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    ratio = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
    filled = int(length * ratio)
    return "█" * filled + "░" * (length - filled) + f" {int(ratio * 100)}%"


class StatusReporter:
    def __init__(self, message: Message, interval: float = 1.5):
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


def parse_session_proxy(json_data: dict) -> Optional[dict]:
    p = json_data.get("proxy")
    if isinstance(p, dict):
        proto = p.get("type") or p.get("protocol") or "socks5"
        return {
            "proxy_type": proto.lower(),
            "addr": p.get("host") or p.get("addr") or p.get("ip"),
            "port": int(p.get("port", 1080)),
            "username": p.get("user") or p.get("username"),
            "password": p.get("pass") or p.get("password"),
        }
    return None


def get_configured_client(session_target: Any, fp: dict[str, str], proxy: Optional[dict] = None) -> TelegramClient:
    active_proxy = proxy or settings.fallback_proxy
    return TelegramClient(
        session_target,
        settings.api_id,
        settings.api_hash,
        proxy=active_proxy,
        device_model=fp["device"],
        system_version=fp["sys"],
        app_version=fp["app"],
        lang_code=fp["lang"],
        system_lang_code=fp["lang"],
    )


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def main_menu_kb(chat_id: int) -> InlineKeyboardMarkup:
    current_mode = db.get_clean_mode(chat_id, settings.default_auto_code)
    if current_mode == 1:
        mode_label = "Миграция · авто"
    elif current_mode == 2:
        mode_label = "Быстрая очистка"
    elif current_mode == 3:
        mode_label = "Проверка спамблока"
    elif current_mode == 4:
        mode_label = "Завершить другие сеансы"
    else:
        mode_label = "Миграция · вручную"

    if queue_paused.get(chat_id, False):
        queue_button = InlineKeyboardButton(text="Проверить и запустить", callback_data="resume_queue")
    else:
        queue_button = InlineKeyboardButton(text="Пауза очереди", callback_data="pause_queue")

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вход по сессии", callback_data="btn_session_login")],
        [InlineKeyboardButton(text="Новая сессия", callback_data="btn_new_session")],
        [InlineKeyboardButton(text="Проверка спамблока", callback_data="check_spam")],
        [InlineKeyboardButton(text="Завершить другие сеансы", callback_data="reset_devices")],
        [InlineKeyboardButton(text=f"Режим: {mode_label}", callback_data="cycle_clean_mode")],
        [
            InlineKeyboardButton(text="Статус", callback_data="queue_status"),
            InlineKeyboardButton(text="Помощь", callback_data="show_help"),
        ],
        [InlineKeyboardButton(text="Настройки", callback_data="interface_settings")],
        [queue_button],
        [InlineKeyboardButton(text="Сбросить очередь", callback_data="confirm_cancel")],
    ])


def action_kb(can_skip: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if can_skip:
        buttons.append(InlineKeyboardButton(text="Пропустить", callback_data="skip_current"))
    buttons.append(InlineKeyboardButton(text="Отменить всё", callback_data="cancel_all"))
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons,
        [InlineKeyboardButton(text="Меню", callback_data="main_menu")],
    ])


def login_code_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Получить код", callback_data="request_login_code")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_all")],
    ])


def queued_review_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить и запустить", callback_data="resume_queue")],
        [InlineKeyboardButton(text="Сбросить очередь", callback_data="confirm_cancel")],
        [InlineKeyboardButton(text="Меню", callback_data="main_menu")],
    ])


# ====================== ДЕАВТОРИЗАЦИЯ СТАРОЙ СЕССИИ ======================

async def terminate_old_session(old_client: Optional[TelegramClient]) -> Tuple[bool, Optional[str]]:
    if not old_client:
        return True, None

    last_error = None
    for attempt in range(1, 4):
        try:
            if not old_client.is_connected():
                await old_client.connect()

            if not await old_client.is_user_authorized():
                logger.info("Старая сессия уже сброшена сервером Telegram.")
                await old_client.disconnect()
                return True, None

            await old_client(LogOutRequest())
            logger.info(f"Старая сессия успешно завершена на попытке {attempt}.")

            try:
                await old_client.disconnect()
            except Exception:
                pass
            return True, None

        except Exception as e:
            last_error = str(e)
            logger.warning(f"Ошибка при выходе из старой сессии (попытка {attempt}/3): {e}")
            try:
                await old_client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(1.0)

    return False, last_error


async def claim_interactive_migration(mig: ActiveMigration):
    # Only one prompt may own the chat FSM; automatic work stays independent.
    while current_migration.get(mig.chat_id) not in (None, mig):
        if mig.skip_requested:
            raise asyncio.CancelledError
        await asyncio.sleep(0.1)
    if mig.skip_requested:
        raise asyncio.CancelledError
    current_migration[mig.chat_id] = mig
    mig.last_activity = time.time()


async def latest_login_message_id(client):
    async with asyncio.timeout(10):
        async for message in client.iter_messages(777000, limit=1):
            return message.id
    return 0


async def poll_login_code(client, mig, request_ts, *, timeout=90.0, read_timeout=10.0,
                          interval=1.5, after_id=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    failures = 0

    async def read_code():
        options = {"min_id": after_id} if after_id is not None else {}
        async for msg in client.iter_messages(777000, limit=20, **options):
            fresh = (msg.id > after_id if after_id is not None else
                     msg.date and msg.date.timestamp() >= request_ts)
            if fresh and not msg.out:
                code = extract_telegram_code(msg.raw_text or "")
                if code:
                    return code
        return None

    while loop.time() < deadline:
        if mig.skip_requested:
            raise asyncio.CancelledError
        try:
            code = await asyncio.wait_for(read_code(), min(read_timeout, deadline - loop.time()))
            failures = 0
            if code:
                return code
        except (AuthKeyUnregisteredError, UserDeactivatedError, PhoneNumberBannedError):
            raise
        except FloodWaitError as exc:
            delay = exc.seconds + 1
            if delay >= deadline - loop.time():
                raise TimeoutError("FloodWait превышает время ожидания кода") from exc
            await asyncio.sleep(delay)
            continue
        except (TimeoutError, ConnectionError, OSError) as exc:
            failures += 1
            logger.warning("Ошибка чтения 777000 (%s/3): %s", failures, type(exc).__name__)
            if failures >= 3:
                raise TimeoutError("Не удалось прочитать 777000 после трёх попыток") from exc
        await asyncio.sleep(min(interval, max(0, deadline - loop.time())))
    raise TimeoutError("Код от 777000 не получен за отведённое время")


def log_code_delivery(mig, result, stage):
    # Never log the response itself: it contains the login request hash.
    logger.info("login_delivery task=%s stage=%s response=%s delivery=%s next=%s timeout=%s",
                mig.id, stage, type(result).__name__, type(getattr(result, 'type', None)).__name__,
                type(getattr(result, 'next_type', None)).__name__, getattr(result, 'timeout', None))


async def wait_automatic_code(old_client, new_client, mig, result, request_ts, after_id, reporter):
    started = asyncio.get_running_loop().time()
    delay = result.timeout if isinstance(result, SentCode) else None
    retry_allowed = (isinstance(result, SentCode) and result.next_type is not None
                     and isinstance(delay, int) and 0 <= delay <= 120)
    try:
        code = await poll_login_code(old_client, mig, request_ts, after_id=after_id,
                                     timeout=max(90, delay) if retry_allowed else 90)
        return code, result
    except TimeoutError:
        if not retry_allowed or asyncio.get_running_loop().time() - started < delay:
            raise
    if mig.skip_requested:
        raise asyncio.CancelledError
    await reporter.update("⏳ Код не получен. Выполняю одну разрешённую Telegram повторную отправку.",
                          force=True)
    async with asyncio.timeout(20):
        result = await new_client(ResendCodeRequest(mig.phone, mig.phone_code_hash),
                                  flood_sleep_threshold=0)
    log_code_delivery(mig, result, 'resend')
    if not isinstance(result, SentCode) or not result.phone_code_hash:
        raise ValueError("Telegram не подтвердил повторную отправку кода")
    mig.phone_code_hash = result.phone_code_hash
    mig.last_activity = time.time()
    if not isinstance(result.type, SentCodeTypeApp):
        return None, result
    await reporter.update("📩 Telegram указал доставку в приложение. Проверяю новые сообщения 777000.",
                          force=True)
    code = await poll_login_code(old_client, mig, request_ts, after_id=after_id)
    return code, result


async def prompt_manual_delivery(mig, state, status):
    await claim_interactive_migration(mig)
    mig.waiting_manual_code = True
    await state.set_state(MigrateStates.waiting_manual_code)
    await status.edit_text(
        "📩 Telegram выбрал другой способ подтверждения входа. "
        "Код в чате 777000 не ожидается.\n"
        "Если вы получили код по SMS, почте или другим способом, введите его вручную. "
        "Если кода нет, отмените задачу: бот не может гарантировать его доставку.",
        reply_markup=action_kb(can_skip=True),
    )


async def cleanup_migration(mig: ActiveMigration, state: Optional[FSMContext] = None):
    chat_id = mig.chat_id
    active_migrations.pop(mig.id, None)

    if mig.queue_db_id:
        db.remove_item(mig.queue_db_id)

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

    if chat_id in current_migration and current_migration[chat_id].id == mig.id:
        del current_migration[chat_id]

    if mig.status_message and db.count_outstanding(chat_id) == 0:
        await try_flush_queue_archives(chat_id, mig.status_message.bot)


async def clear_user_queue(chat_id: int, state: Optional[FSMContext] = None, bot: Optional[Bot] = None):
    worker_task = queue_worker_tasks.get(chat_id)
    current_task = asyncio.current_task()
    if worker_task and worker_task is not current_task and not worker_task.done():
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    queue_worker_tasks.pop(chat_id, None)
    queue_paused[chat_id] = False

    for mig in list(active_migrations.values()):
        if mig.chat_id == chat_id:
            await cleanup_migration(mig, state)
    if chat_id in current_migration:
        await cleanup_migration(current_migration[chat_id], state)

    files_to_delete = db.clear_chat_queue(chat_id)
    for f in files_to_delete:
        try:
            p = Path(f)
            if p.exists():
                p.unlink(missing_ok=True)
                if any(p.parent.name.startswith(x) for x in ("upload_", "fast_", "mig_")):
                    shutil.rmtree(p.parent, ignore_errors=True)
        except Exception:
            pass

    queue_workers[chat_id] = False
    if state:
        await state.clear()
    if bot:
        await flush_all_archives(chat_id, bot)


# ====================== ЭКСТРЕМАЛЬНАЯ ОЧИСТКА ======================

async def clean_account_dialogs(client: TelegramClient, reporter: StatusReporter, report: CleanReport):
    """Clean a bounded number of dialogs concurrently, respecting account FloodWaits."""
    pending: Set[asyncio.Task] = set()
    blocked_until = 0.0
    loop = asyncio.get_running_loop()

    async def wait_for_account():
        while blocked_until > loop.time():
            await asyncio.sleep(blocked_until - loop.time())

    async def request(value):
        await wait_for_account()
        # Surface FloodWait to the shared account gate instead of sleeping per task.
        return await client(value, flood_sleep_threshold=0)

    async def clean_dialog(entity):
        nonlocal blocked_until
        if isinstance(entity, User) and getattr(entity, "is_self", False):
            return
        if not isinstance(entity, (Channel, Chat, User)):
            return
        for attempt in range(3):
            try:
                if isinstance(entity, Channel):
                    if getattr(entity, "creator", False):
                        try:
                            await request(DeleteChannelRequest(channel=entity))
                            report.channels_deleted += 1
                        except FloodWaitError:
                            raise
                        except Exception:
                            await request(LeaveChannelRequest(entity))
                    else:
                        await request(LeaveChannelRequest(entity))
                elif isinstance(entity, Chat):
                    await request(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
                else:
                    if getattr(entity, "bot", False):
                        try:
                            await request(BlockRequest(id=entity))
                        except FloodWaitError:
                            raise
                        except Exception:
                            pass
                    try:
                        await request(DeleteHistoryRequest(peer=entity, max_id=0, just_clear=False, revoke=True))
                    except FloodWaitError:
                        raise
                    except Exception:
                        await wait_for_account()
                        await client.delete_dialog(entity)
                report.dialogs_purged += 1
                if report.dialogs_purged % 10 == 0:
                    await reporter.update(
                        f"🧹 Очищено диалогов: <b>{report.dialogs_purged}</b>\n"
                        f"{render_progress_bar(8, 10)}"
                    )
                return
            except FloodWaitError as exc:
                blocked_until = max(blocked_until, loop.time() + exc.seconds + 1)
                await reporter.update(f"⏳ FloodWait: ожидание {exc.seconds} с...", force=True)
                if attempt < 2:
                    continue
            except Exception:
                logger.warning("Не удалось очистить диалог %s", entity.id, exc_info=True)
                break
        report.dialogs_failed += 1

    try:
        async for dialog in client.iter_dialogs():
            await wait_for_account()
            pending.add(asyncio.create_task(clean_dialog(dialog.entity)))
            if len(pending) >= settings.concurrent_dialogs_per_session:
                await asyncio.gather(*pending)
                pending.clear()
        if pending:
            await asyncio.gather(*pending)
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def deep_clean_account(client: TelegramClient, reporter: StatusReporter) -> CleanReport:
    report = CleanReport()

    # 1. Профиль и @username
    await reporter.update(f"🧹 Сброс профиля...\n{render_progress_bar(1, 10)}", force=True)
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
    await reporter.update(f"🧹 Удаление фото профиля...\n{render_progress_bar(2, 10)}")
    try:
        photos = await client(GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=200))
        if photos.photos:
            input_photos = [
                InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
                for p in photos.photos
            ]
            await client(DeletePhotosRequest(id=input_photos))
            report.photos_deleted = len(input_photos)
    except Exception as e:
        logger.warning(f"Ошибка удаления фото: {e}")

    # 3. Privacy Lockdown
    await reporter.update(f"🔒 Сброс приватности...\n{render_progress_bar(3, 10)}")
    try:
        disallow = [InputPrivacyValueDisallowAll()]
        await client(SetPrivacyRequest(key=InputPrivacyKeyPhoneNumber(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyStatusTimestamp(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyProfilePhoto(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyForwards(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyPhoneCall(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyChatInvite(), rules=disallow))
        report.privacy_secured = True
    except Exception as e:
        logger.warning(f"Ошибка параметров приватности: {e}")

    # 4. Платежные реквизиты
    try:
        await client(ClearSavedInfoRequest(credentials=True, info=True))
        report.payments_cleared = True
    except Exception as e:
        logger.debug(f"Платежные данные отсутствуют: {e}")

    # 5. Установка TTL аккаунта (30 дней)
    try:
        await client(SetAccountTTLRequest(ttl=AccountDaysTTL(days=30)))
        report.ttl_set_30d = True
    except Exception as e:
        logger.debug(f"TTL не обновлен: {e}")

    # 6. Контакты и облачный поисковый граф
    await reporter.update(f"🧹 Стирание контактов...\n{render_progress_bar(4, 10)}")
    try:
        contacts_res = await client(GetContactsRequest(hash=0))
        users = getattr(contacts_res, "users", [])
        if users:
            await client(DeleteContactsRequest(id=users))
            report.contacts_deleted = len(users)
        await client(ResetSavedRequest())
        report.saved_contacts_reset = True
    except Exception as e:
        logger.warning(f"Ошибка удаления контактов: {e}")

    # 7. Папки чатов
    await reporter.update(f"🧹 Удаление папок...\n{render_progress_bar(5, 10)}")
    try:
        filters = await client(GetDialogFiltersRequest())
        for f in getattr(filters, "filters", []):
            fid = getattr(f, "id", None)
            if fid is not None and fid != 0:
                await client(UpdateDialogFilterRequest(id=fid, filter=None))
                report.folders_deleted += 1
    except Exception as e:
        logger.warning(f"Ошибка папок: {e}")

    # 8. Стикеры и эмодзи-паки
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

    # 9. Черновики и Избранное
    try:
        await client(ClearAllDraftsRequest())
        report.drafts_cleared = True
    except Exception:
        pass

    try:
        await client(DeleteHistoryRequest(peer="me", max_id=0, just_clear=False, revoke=True))
        report.saved_messages_cleared = True
    except Exception as e:
        logger.warning(f"Очистка Избранного: {e}")

    # 10. Диалоги, каналы, группы и боты
    await reporter.update(f"🧹 Тотальная зачистка чатов...\n{render_progress_bar(7, 10)}", force=True)
    await clean_account_dialogs(client, reporter, report)

    try:
        await client(ResetWebAuthorizationsRequest())
        report.web_auths_reset = True
    except Exception as e:
        pass

    await reporter.update(f"✅ Финал очистки!\n{render_progress_bar(10, 10)}", force=True)
    return report


# ====================== БЫСТРЫЙ ЧЕКЕР + ОЧИСТКА ======================

async def quick_check_session(client: TelegramClient) -> SessionCheckResult:
    try:
        if not client.is_connected():
            await client.connect()

        if not await client.is_user_authorized():
            return SessionCheckResult(is_valid=False, status="UNAUTHORIZED", error="Сессия не авторизована")

        me = await client.get_me()
        if not me:
            return SessionCheckResult(is_valid=False, status="DEACTIVATED", error="Аккаунт деактивирован")

        phone = f"+{me.phone}" if me.phone and not str(me.phone).startswith("+") else str(me.phone or "")
        return SessionCheckResult(
            is_valid=True,
            status="ACTIVE",
            user_id=me.id,
            phone=phone,
            username=me.username,
        )

    except PhoneNumberBannedError:
        return SessionCheckResult(is_valid=False, status="BANNED", error="Номер заблокирован в Telegram")
    except UserDeactivatedError:
        return SessionCheckResult(is_valid=False, status="DEACTIVATED", error="Аккаунт удален")
    except AuthKeyUnregisteredError:
        return SessionCheckResult(is_valid=False, status="KEY_REVOKED", error="Ключ авторизации отозван")
    except Exception as e:
        return SessionCheckResult(is_valid=False, status="ERROR", error=public_error(e))


async def process_spam_check_session(item: dict, bot: Bot, state: FSMContext):
    await _process_session_operation(item, bot, state, reset_sessions=False)


async def process_reset_devices_session(item: dict, bot: Bot, state: FSMContext):
    await _process_session_operation(item, bot, state, reset_sessions=True)


async def _process_session_operation(item: dict, bot: Bot, state: FSMContext, *, reset_sessions: bool):
    """Operate on a copy of the supplied session, retaining its authorization."""
    chat_id = item["chat_id"]
    labels = DEVICE_LABELS if reset_sessions else SPAM_LABELS
    failure_category = "failed" if reset_sessions else "unknown"
    source = Path(item["file_path"])
    client = None
    temp_dir = Path(tempfile.mkdtemp(prefix=f"spam_{chat_id}_"))
    try:
        session_copy = temp_dir / "check.session"
        shutil.copy(source, session_copy)
        metadata = {}
        json_source = source.with_suffix(".json")
        if json_source.exists():
            try:
                value = json.loads(json_source.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    metadata = value
            except (ValueError, OSError):
                pass
        result = SpamResult(failure_category, "Проверка не завершена")
        try:
            proxy = json.loads(item["proxy_json"]) if item.get("proxy_json") else None
            client = get_configured_client(str(session_copy.with_suffix("")), get_random_fingerprint(), proxy)
            async with asyncio.timeout(20):
                await client.connect()
                authorized = await client.is_user_authorized()
            if authorized:
                result = await reset_other_sessions(client) if reset_sessions else await check_spam_status(client)
            else:
                result = SpamResult(failure_category, "Сессия не авторизована")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not reset_sessions and getattr(exc, "message", "") == "FROZEN_METHOD_INVALID":
                result = SpamResult("frozen", "Telegram: FROZEN_METHOD_INVALID")
            else:
                result = SpamResult(failure_category, "Истекло время подключения к Telegram (20 с)"
                                    if isinstance(exc, TimeoutError) else
                                    "Ошибка подключения: " + type(exc).__name__)
        finally:
            if client:
                try:
                    async with asyncio.timeout(10):
                        await client.disconnect()
                except Exception:
                    logger.warning("Не удалось отключить сессию операции", exc_info=True)

        # Preserve input JSON fields (including 2FA); attach the operation result.
        stem = source.stem
        metadata["other_sessions_reset" if reset_sessions else "spam_check"] = {
            "category": result.category,
            "label": labels[result.category],
            "detail": result.detail,
            "checked_at": datetime.now().astimezone().isoformat(),
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(session_copy, arcname=f"{stem}.session")
            archive.writestr(f"{stem}.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        category = ("devices_" if reset_sessions else "spam_") + result.category
        db.save_archive(chat_id, f"{stem}.zip", output.getvalue(), category)
        try:
            text = f"🔎 <code>{html.escape(item['original_name'])}</code>: <b>{labels[result.category]}</b>"
            if reset_sessions or result.category == "unknown":
                # Bound Telegram message length and escape server-provided text.
                reason = result.detail[:1500]
                prefix = "Результат" if reset_sessions and result.category == "reset" else "Причина"
                text += f"\n{prefix}: {html.escape(reason)}"
            await bot.send_message(chat_id, text)
        except Exception:
            logger.exception("Отчёт проверки не отправлен; ZIP сохранён")
        await try_flush_queue_archives(chat_id, bot, minimum=RESULTS_PER_ARCHIVE, category=category)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        db.remove_item(item["id"])
        if db.count_outstanding(chat_id) == 0:
            await flush_all_archives(chat_id, bot)


async def process_fast_clean_session(item: dict, bot: Bot, state: Optional[FSMContext] = None):
    chat_id = item["chat_id"]
    mig_id = str(uuid.uuid4())[:8]
    archive_saved = False

    status = await bot.send_message(
        chat_id,
        f"⚡ <b>[Fast Clean]</b> Проверка: <code>{html.escape(item['original_name'])}</code>...",
        reply_markup=action_kb(can_skip=True),
    )
    reporter = StatusReporter(status, interval=1.2)

    temp_dir = Path(tempfile.mkdtemp(prefix=f"fast_{chat_id}_{mig_id}_"))
    session_copy = temp_dir / "target.session"
    shutil.copy(item["file_path"], session_copy)

    proxy_dict = json.loads(item["proxy_json"]) if item.get("proxy_json") else None
    if proxy_dict:
        is_alive = await verify_proxy_alive(proxy_dict)
        if not is_alive:
            logger.warning(f"Прокси {proxy_dict.get('addr')} недоступен, откат к fallback.")
            proxy_dict = None

    fp = get_random_fingerprint()
    client = get_configured_client(str(session_copy.with_suffix("")), fp, proxy_dict)

    try:
        await reporter.update("🔍 <b>[1/3] Экспресс-валидация сессии...</b>", force=True)
        check = await quick_check_session(client)

        if not check.is_valid:
            await reporter.update(
                f"❌ <b>Сессия отсеяна:</b> <code>{html.escape(item['original_name'])}</code>\n"
                f"Причина: <code>{check.error}</code> ({check.status})",
                force=True,
            )
            return

        phone_label = check.phone or "NoNumber"
        await reporter.update(
            f"✅ <b>Сессия валидна!</b> <code>{phone_label}</code>\n"
            f"🧹 <b>[2/3] Запуск экстремальной зачистки...</b>",
            force=True,
        )

        clean_report = await deep_clean_account(client, reporter)

        try:
            await client(ResetAuthorizationsRequest())
            clean_report.sessions_reset = True
        except Exception as e:
            clean_report.sessions_reset = False
            clean_report.sessions_reset_warning = public_error(e)

        await reporter.update("📦 <b>[3/3] Сборка артефактов...</b>", force=True)
        metadata = await create_metadata(client, phone_label, fp)
        await client.disconnect()

        final_session = temp_dir / f"{phone_label}.session"
        session_copy.rename(final_session)

        meta_file = temp_dir / f"{phone_label}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = temp_dir / f"{phone_label}_cleaned.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(final_session, arcname=f"{phone_label}.session")
            zf.write(meta_file, arcname=f"{phone_label}.json")

        caption = (
            f"⚡ <b>Сессия очищена по стандарту v100x+</b>\n\n"
            f"📞 Телефон: <code>{phone_label}</code>\n"
            f"🆔 ID: <code>{check.user_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🗑 Стерто диалогов: <b>{clean_report.dialogs_purged}</b>\n"
            f"⚠️ Не очищено: <b>{clean_report.dialogs_failed}</b>\n"
            f"📇 Контакты и поисковый топ: <b>Очищены</b>\n"
            f"👑 Удалено каналов создателя: <b>{clean_report.channels_deleted}</b>\n"
            f"💳 Платежные данные: <b>Уничтожены</b>\n"
            f"⏳ TTL аккаунта: <b>30 дней</b>\n"
            f"🔒 Privacy Lockdown: <b>Включен</b>\n"
            f"🌐 Сессии: <b>{'Сброшены' if clean_report.sessions_reset else 'Пропущены (&lt; 24ч)'}</b>"
        )

        await deliver_account_archive(
            chat_id, bot, zip_path.read_bytes(), f"{phone_label}_cleaned.zip", caption,
            queued=bool(item.get("id")),
        )
        archive_saved = True
        stats["success"] += 1

    except Exception as e:
        logger.exception("Ошибка в fast pipeline")
        stats["failed"] += 1
        await reporter.update(
            f"❌ Ошибка быстрой очистки: <code>{public_error(e)}</code>", force=True
        )
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        if temp_dir.exists() and (archive_saved or not list(temp_dir.glob("*.zip"))):
            shutil.rmtree(temp_dir, ignore_errors=True)
        if item.get("id"):
            db.remove_item(item["id"])
        if db.count_outstanding(chat_id) == 0:
            await try_flush_queue_archives(chat_id, bot)


# ====================== ФИНАЛИЗАЦИЯ И МЕТАДАННЫЕ ======================

async def create_metadata(client: TelegramClient, phone: str, fp: dict[str, str]) -> dict[str, Any]:
    me: User = await client.get_me()
    ts = int(time.time())
    return {
        "app_id": settings.api_id,
        "app_hash": settings.api_hash,
        "device": fp["device"],
        "sdk": fp["sys"],
        "app_version": fp["app"],
        "system_lang_pack": fp["lang"],
        "system_lang_code": fp["lang"],
        "lang_pack": "tdesktop",
        "lang_code": fp["lang"],
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
    archive_saved = False

    try:
        if not client or not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Клиент не авторизован")

        # Дополнительная попытка деавторизации, если она еще не прошла
        if mig.old_client and not mig.old_session_terminated:
            success, err = await terminate_old_session(mig.old_client)
            mig.old_session_terminated = success
            mig.old_session_error = err
            mig.old_client = None

        # Очищаем аккаунт на новой активной сессии
        clean_info = await deep_clean_account(client, reporter)

        # Сброс остальных сессий
        try:
            await client(ResetAuthorizationsRequest())
            clean_info.sessions_reset = True
        except FreshResetAuthorisationForbiddenError:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = "Сессия свежая (&lt; 24 ч, лимит Telegram)"
        except Exception as e:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = public_error(e)

        metadata = await create_metadata(client, phone, mig.fingerprint)
        await client.disconnect()
        mig.new_client = None

        session_file = temp_dir / f"{phone}.session"
        original = Path(str(temp_dir / "new_session") + ".session")
        if original.exists():
            original.rename(session_file)

        if not session_file.exists():
            raise FileNotFoundError("Итоговый файл .session не найден")

        meta_file = temp_dir / f"{phone}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = temp_dir / f"{phone}_v100x.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_file, arcname=f"{phone}.session")
            zf.write(meta_file, arcname=f"{phone}.json")

        data = zip_path.read_bytes()
        reset_status = "сброшены" if clean_info.sessions_reset else "не сброшены"

        if mig.is_new_session:
            old_line = ""
        elif mig.old_session_terminated:
            old_line = "Старая сессия: завершена\n"
        else:
            old_line = "Старая сессия: не завершена\n"

        ttl_line = "\nАвтоудаление аккаунта: 30 дней без активности" if clean_info.ttl_set_30d else ""

        caption = (
            "<b>Сессия готова</b>\n\n"
            f"<code>{html.escape(phone)}</code> · ID <code>{metadata['id']}</code>\n"
            f"{old_line}"
            f"Удалено диалогов: {clean_info.dialogs_purged}\n"
            f"Не очищено: {clean_info.dialogs_failed}\n"
            f"Другие устройства: {reset_status}"
            f"{ttl_line}"
        )

        await deliver_account_archive(
            mig.chat_id, message.bot, data, f"{phone}_v100x.zip", caption,
            queued=bool(mig.queue_db_id),
        )
        archive_saved = True
        stats["success"] += 1

    except Exception as e:
        logger.exception("Ошибка финализации")
        stats["failed"] += 1
        await message.answer(f"❌ Ошибка миграции: <code>{public_error(e)}</code>")
    finally:
        if not archive_saved and temp_dir and list(temp_dir.glob("*.zip")):
            # Preserve the only copy if saving to SQLite failed (e.g. disk full).
            logger.error("Архив не записан в БД; временный каталог сохранён")
            mig.temp_dir = None
        await cleanup_migration(mig, state)
        if db.count_outstanding(mig.chat_id) == 0:
            await try_flush_queue_archives(mig.chat_id, message.bot)


# ====================== ОБРАБОТЧИК МИГРАЦИИ ======================

async def process_one_session(item: dict, bot: Bot, state: FSMContext):
    chat_id = item["chat_id"]
    mig_id = str(uuid.uuid4())[:8]
    auto_mode = db.get_clean_mode(chat_id, settings.default_auto_code) == 1

    status = await bot.send_message(
        chat_id,
        f"⏳ Начинаю: <b>{html.escape(item['original_name'])}</b>\n"
        f"Режим перехвата: <b>{'Авто' if auto_mode else 'Вручную'}</b>\n"
        f"2FA из JSON: <b>{'Да' if item['auto_2fa'] else 'Нет'}</b>",
        reply_markup=action_kb(can_skip=True),
    )

    proxy_dict = json.loads(item["proxy_json"]) if item["proxy_json"] else None
    if proxy_dict:
        if not await verify_proxy_alive(proxy_dict):
            proxy_dict = None

    mig = ActiveMigration(
        id=mig_id,
        chat_id=chat_id,
        status_message=status,
        auto_2fa_password=item["auto_2fa"],
        proxy_config=proxy_dict,
        queue_db_id=item["id"],
    )
    active_migrations[mig.id] = mig
    reporter = StatusReporter(status)

    try:
        if not auto_mode:
            await claim_interactive_migration(mig)
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"mig_{chat_id}_{mig_id}_"))
        old_session_path = mig.temp_dir / "old.session"
        new_session_path = str(mig.temp_dir / "new_session")
        shutil.copy(item["file_path"], old_session_path)

        old_client = get_configured_client(str(old_session_path.with_suffix("")), mig.fingerprint, mig.proxy_config)
        mig.old_client = old_client
        await old_client.connect()

        if not await old_client.is_user_authorized():
            await reporter.update(f"❌ <b>{html.escape(item['original_name'])}</b>: сессия не авторизована.", force=True)
            await cleanup_migration(mig)
            return

        me = await old_client.get_me()
        phone = me.phone
        if not phone:
            await reporter.update(f"❌ <b>{html.escape(item['original_name'])}</b>: нет номера.", force=True)
            await cleanup_migration(mig)
            return

        phone = f"+{phone}" if not str(phone).startswith("+") else str(phone)
        mig.phone = phone
        mig.old_client = old_client

        await reporter.update(
            f"📱 Сессия: <code>{phone}</code>\n"
            f"💻 Имитация: <code>{mig.fingerprint['device']}</code>\n"
            f"⏳ Запрашиваю код подтверждения...",
            force=True,
            reply_markup=action_kb(can_skip=True),
        )

        new_client = get_configured_client(new_session_path, mig.fingerprint, mig.proxy_config)
        mig.new_client = new_client
        await new_client.connect()

        # Capture the boundary before requesting the code. Server message IDs
        # remain reliable when the host clock differs from Telegram's clock.
        after_id = await latest_login_message_id(old_client) if auto_mode else None
        request_ts = int(time.time()) - 5
        async with asyncio.timeout(30):
            result = await new_client.send_code_request(phone)
        log_code_delivery(mig, result, 'request')
        mig.phone_code_hash = result.phone_code_hash
        mig.last_activity = time.time()

        if auto_mode and isinstance(result, SentCode) and not isinstance(result.type, SentCodeTypeApp):
            await prompt_manual_delivery(mig, state, status)
            return

        if auto_mode:
            await reporter.update(
                "📩 Запрос входа принят. Проверяю, появится ли код в 777000…",
                force=True,
                reply_markup=action_kb(can_skip=True),
            )
            try:
                code, result = await wait_automatic_code(
                    old_client, new_client, mig, result, request_ts, after_id, reporter)
                if code is None:
                    await prompt_manual_delivery(mig, state, status)
                    return
            except FloodWaitError as exc:
                await reporter.update(
                    f"⏳ Telegram ограничил запросы входа. Повторите через {exc.seconds} с. "
                    "Исходная сессия сохранена.", force=True)
                await cleanup_migration(mig)
                return
            except TimeoutError:
                logger.warning("login_delivery task=%s stage=wait outcome=timeout", mig.id)
                await reporter.update(
                    "❌ Код в 777000 не получен за отведённое время. "
                    "Доставка не подтверждена или чтение чата недоступно.\n"
                    f"Исходная сессия сохранена. Номер задачи: <code>{mig.id}</code>.",
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
            "🔑 Код подтверждения принят.\n⏳ Вход в новую сессию...",
            force=True,
            reply_markup=action_kb(can_skip=False),
        )

        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=mig.phone_code_hash)

            # Вход подтвержден: деавторизуем старую сессию
            success, err = await terminate_old_session(mig.old_client)
            mig.old_session_terminated = success
            mig.old_session_error = err
            mig.old_client = None

            await finalize(mig, status, state)

        except SessionPasswordNeededError:
            if mig.auto_2fa_password:
                await reporter.update("🔒 Пароль 2FA найден в JSON! Ввожу автоматически...", force=True)
                try:
                    await new_client.sign_in(password=mig.auto_2fa_password)

                    success, err = await terminate_old_session(mig.old_client)
                    mig.old_session_terminated = success
                    mig.old_session_error = err
                    mig.old_client = None

                    await finalize(mig, status, state)
                    return
                except PasswordHashInvalidError:
                    await reporter.update("⚠️ Пароль 2FA из JSON не подошел.", force=True)

            if mig.queue_db_id:
                stats["skipped"] += 1
                await reporter.update(
                    "⏭ Сессия пропущена: требуется пароль 2FA, а в JSON нет подходящего пароля.",
                    force=True,
                )
                await cleanup_migration(mig, state)
                return

            await claim_interactive_migration(mig)
            mig.waiting_2fa = True
            mig.last_activity = time.time()
            await state.set_state(MigrateStates.waiting_password)
            await status.edit_text(
                "🔑 Код подтверждения принят.\n"
                "🔒 Обнаружен пароль 2FA. Отправьте его следующим сообщением:",
                reply_markup=action_kb(can_skip=True),
            )
            return

        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            await reporter.update(
                f"❌ Неверный или истекший код: <code>{public_error(e)}</code>", force=True
            )
            await cleanup_migration(mig)
            return
        except Exception as e:
            await reporter.update(f"❌ Ошибка входа: <code>{public_error(e)}</code>", force=True)
            await cleanup_migration(mig)
            return

    except asyncio.CancelledError:
        await cleanup_migration(mig, state)
        if not mig.skip_requested:
            raise
    except PhoneNumberBannedError:
        await reporter.update(f"❌ Номер <code>{mig.phone}</code> заблокирован Telegram.", force=True)
        await cleanup_migration(mig)
    except UserDeactivatedError:
        await reporter.update(f"❌ Аккаунт <code>{mig.phone}</code> деактивирован.", force=True)
        await cleanup_migration(mig)
    except (TypeNotFoundError, AuthKeyDuplicatedError) as e:
        logger.warning("Обработка остановлена (%s)", type(e).__name__)
        reason = (
            "Telegram отозвал ключ этой сессии."
            if isinstance(e, AuthKeyDuplicatedError)
            else "Не удалось прочитать ответ Telegram."
        )
        await reporter.update(f"❌ {reason} Задача завершена.", force=True)
    except Exception as e:
        logger.exception("Ошибка process_one_session")
        await reporter.update(f"❌ Сбой обработки: <code>{public_error(e)}</code>", force=True)
        await cleanup_migration(mig)
    finally:
        if mig.id in active_migrations and not (mig.waiting_2fa or mig.waiting_manual_code):
            await cleanup_migration(mig)


# ====================== ВОРКЕР ОЧЕРЕДИ ======================

def start_queue_worker(chat_id: int, bot: Bot, state: FSMContext) -> Optional[asyncio.Task]:
    """Start at most one queue supervisor per chat."""
    if queue_paused.get(chat_id, False):
        return None
    existing = queue_worker_tasks.get(chat_id)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(queue_worker(chat_id, bot, state))
    queue_worker_tasks[chat_id] = task
    return task


async def queue_worker(chat_id: int, bot: Bot, state: FSMContext):
    current_task = asyncio.current_task()
    queue_workers[chat_id] = True
    running: Set[asyncio.Task] = set()
    active_names: Dict[str, str] = {}
    running_mode = None

    async def process_item(item: dict, mode: int):
        item_id = item["id"]
        active_names[item_id] = item.get("original_name", "session")
        queue_current_items[chat_id] = ", ".join(active_names.values())
        try:
            if mode in (2, 3, 4):
                async with get_chat_semaphore(chat_id):
                    if mode == 4:
                        await process_reset_devices_session(item, bot, state)
                    elif mode == 3:
                        await process_spam_check_session(item, bot, state)
                    else:
                        await process_fast_clean_session(item, bot, state)
            else:
                await process_one_session(item, bot, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Необработанная ошибка элемента очереди")
            stats["failed"] += 1
            db.remove_item(item_id)
            try:
                await bot.send_message(
                    chat_id,
                    f"❌ Сессия пропущена из-за ошибки: <code>{public_error(exc)}</code>",
                )
            except Exception:
                pass
        finally:
            active_names.pop(item_id, None)
            if active_names:
                queue_current_items[chat_id] = ", ".join(active_names.values())
            else:
                queue_current_items.pop(chat_id, None)

    try:
        while True:
            active = current_migration.get(chat_id)
            paused = queue_paused.get(chat_id, False)
            waiting = active and (active.waiting_2fa or active.waiting_manual_code)
            mode = db.get_clean_mode(chat_id, settings.default_auto_code)
            limit = settings.concurrent_workers_per_chat if mode in (1, 2, 3, 4) else 1
            # Drain the old mode before switching (manual mode owns one FSM).
            if not paused and not waiting and (not running or running_mode == mode):
                running_mode = mode
                detached = sum(m.chat_id == chat_id and m.queue_db_id not in active_names
                               for m in active_migrations.values())
                while len(running) + detached < limit:
                    item = db.pop_next_item(chat_id)
                    if not item:
                        break
                    running.add(asyncio.create_task(process_item(item, mode)))
            if not running:
                break
            done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()

        if not queue_paused.get(chat_id, False) and db.count_outstanding(chat_id) == 0 and not current_migration.get(chat_id):
            await flush_all_archives(chat_id, bot)
            await bot.send_message(chat_id, "✅ Очередь обработана")
    finally:
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        queue_workers[chat_id] = False
        queue_current_items.pop(chat_id, None)
        if queue_worker_tasks.get(chat_id) is current_task:
            queue_worker_tasks.pop(chat_id, None)


# ====================== ХЕНДЛЕРЫ ======================

router = Router()


@router.message(Command("archives"))
async def cmd_archives(message: Message):
    if not any(db.archives(message.chat.id, True, category)
               for category in ARCHIVE_LABELS):
        await message.answer("Сохранённых архивов для этого чата нет.")
        return
    await flush_all_archives(message.chat.id, message.bot, include_sent=True)


MODE_LABELS = {1: "Миграция · авто", 2: "Быстрая очистка", 0: "Миграция · вручную",
               3: "Проверка спамблока", 4: "Завершить другие сеансы"}


def menu_text(chat_id: int) -> str:
    mode = db.get_clean_mode(chat_id, settings.default_auto_code)
    pending = db.count_queue(chat_id)
    description = ("Завершение других сеансов с сохранением загруженной сессии." if mode == 4 else
                   "Проверка через @SpamBot и ZIP по категориям." if mode == 3 else
                   "Миграция завершает старую сессию. Очистка удаляет данные аккаунта.")
    if db.compact_menu(chat_id):
        return (
            "<b>Меню</b>\n"
            f"{MODE_LABELS.get(mode, 'Миграция · вручную')} · В очереди: {pending}\n"
            f"{description}"
        )
    return (
        "<b>Управление сессиями</b>\n\n"
        f"Режим: <b>{MODE_LABELS.get(mode, 'Миграция · вручную')}</b>\n"
        f"В очереди: <b>{pending}</b>\n\n"
        "Выберите действие. Для смены режима нажмите на его название.\n"
        f"{description}"
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    await call.answer()
    await call.message.answer(menu_text(call.message.chat.id), reply_markup=main_menu_kb(call.message.chat.id))


@router.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(queue_status_text(message.chat.id), reply_markup=main_menu_kb(message.chat.id))


def queue_status_text(chat_id: int) -> str:
    mig = current_migration.get(chat_id)
    if queue_paused.get(chat_id, False):
        activity = "Очередь приостановлена"
    elif mig and mig.waiting_2fa:
        activity = "Ожидается пароль 2FA"
    elif mig and mig.waiting_manual_code:
        activity = "Ожидается код подтверждения"
    elif mig or queue_workers.get(chat_id):
        activity = "Обработка"
    else:
        activity = "Нет активной миграции"
    current = queue_current_items.get(chat_id)
    current_line = f"\nТекущая: <code>{html.escape(current)}</code>" if current else ""
    return (
        f"<b>Статус</b>\n\n{activity}{current_line}\n"
        f"Ожидают обработки: <b>{db.count_queue(chat_id)}</b>"
    )


@router.callback_query(F.data == "queue_status")
async def cb_queue_status(call: CallbackQuery):
    await call.answer()
    await call.message.answer(queue_status_text(call.message.chat.id), reply_markup=main_menu_kb(call.message.chat.id))


@router.callback_query(F.data == "pause_queue")
async def cb_pause_queue(call: CallbackQuery):
    chat_id = call.message.chat.id
    if db.count_queue(chat_id) == 0 and chat_id not in current_migration:
        await call.answer("Очередь пуста", show_alert=True)
        return
    queue_paused[chat_id] = True
    await call.answer("Очередь приостановлена")
    await call.message.edit_text(menu_text(chat_id), reply_markup=main_menu_kb(chat_id))


@router.callback_query(F.data == "resume_queue")
async def cb_resume_queue(call: CallbackQuery, state: FSMContext):
    chat_id = call.message.chat.id
    queue_paused[chat_id] = False
    start_queue_worker(chat_id, call.bot, state)
    await call.answer("Очередь продолжена")
    await call.message.edit_text(menu_text(chat_id), reply_markup=main_menu_kb(chat_id))


@router.message(Command("pause"))
async def cmd_pause(message: Message):
    chat_id = message.chat.id
    if db.count_queue(chat_id) == 0 and chat_id not in current_migration:
        await message.answer("Очередь пуста.", reply_markup=main_menu_kb(chat_id))
        return
    queue_paused[chat_id] = True
    await message.answer("Очередь приостановлена.", reply_markup=main_menu_kb(chat_id))


@router.message(Command("resume"))
async def cmd_resume(message: Message, state: FSMContext):
    chat_id = message.chat.id
    queue_paused[chat_id] = False
    start_queue_worker(chat_id, message.bot, state)
    await message.answer("Очередь продолжена.", reply_markup=main_menu_kb(chat_id))


@router.callback_query(F.data == "show_help")
async def cb_help(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "<b>Как пользоваться</b>\n\n"
        "1. Выберите режим в меню.\n"
        "2. Нажмите «Вход по сессии» для загрузки файла или «Новая сессия» для входа по номеру.\n"
        "3. Следуйте сообщениям бота.\n\n"
        "<b>Режимы</b>\n"
        "Миграция · авто — автоматическое получение кода.\n"
        "Миграция · вручную — код вводите вы.\n"
        "Быстрая очистка — удаление данных без переноса сессии.\n"
        "Проверка спамблока — ответ @SpamBot и ZIP по категориям.\n\n"
        "Файлы: .session или ZIP с парными JSON, до 20 МБ.\n"
        f"Лимит очереди: {db.get_upload_limit(call.message.chat.id)} сессий.\n\n"
        "/start — меню, /status — очередь, /cancel — отменить всё.\n"
        "Открытие меню не прерывает обработку.\n"
        "Миграция завершает старую сессию. Очистка удаляет данные аккаунта.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Меню", callback_data="main_menu"),
        ]]),
    )


@router.callback_query(F.data == "confirm_cancel")
async def cb_confirm_cancel(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Сбросить очередь и остановить текущую миграцию? Уже выполненные изменения аккаунта не отменятся.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, сбросить", callback_data="cancel_all"),
            InlineKeyboardButton(text="Назад", callback_data="main_menu"),
        ]]),
    )


def interface_settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    label = "Краткий" if db.compact_menu(chat_id) else "Подробный"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Вид меню: {label}", callback_data="toggle_compact_menu")],
        [InlineKeyboardButton(text=f"Лимит загрузки: {db.get_upload_limit(chat_id)}", callback_data="upload_limits")],
        [InlineKeyboardButton(text="О данных", callback_data="show_privacy")],
        [InlineKeyboardButton(text="Ограничения файлов", callback_data="show_limits")],
        [InlineKeyboardButton(text="Меню", callback_data="main_menu")],
    ])


def limits_text(chat_id: Optional[int] = None) -> str:
    limit = db.get_upload_limit(chat_id) if chat_id is not None else settings.max_queue_per_user
    return (
        "<b>Ограничения файлов</b>\n\n"
        "Форматы: .session и .zip.\n"
        f"Размер загрузки: до {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ.\n"
        f"Размер распакованного ZIP: до {MAX_EXTRACTED_BYTES // (1024 * 1024)} МБ.\n"
        f"Файлов и папок в ZIP: до {MAX_ARCHIVE_ENTRIES}.\n"
        f"Сессий в очереди, включая обработку: до {limit}.\n"
        "ZIP с паролем, ссылками и выходом за пределы папки не принимаются."
    )


PRIVACY_TEXT = (
    "<b>О данных</b>\n\n"
    "Файл .session даёт доступ к Telegram-аккаунту. Не передавайте чужие сессии.\n"
    "Загруженные файлы сохраняются на сервере бота. Очередь, параметры прокси "
    "и пароль 2FA из JSON могут храниться в локальной базе.\n"
    "Настройки меню также сохраняются на сервере.\n\n"
    "Не отправляйте секретные данные в общие чаты. "
    "Сброс очереди не удаляет уже отправленные сообщения, резервные копии "
    "и данные на стороне Telegram. Активные подключения можно проверить "
    "в официальном Telegram: Настройки → Устройства."
)


@router.message(Command("settings"))
async def cmd_interface_settings(message: Message):
    await message.answer("<b>Настройки</b>\nВыберите вид меню или лимит загрузки.", reply_markup=interface_settings_kb(message.chat.id))


def upload_limits_kb(chat_id: int) -> InlineKeyboardMarkup:
    maximum = settings.max_queue_per_user
    choices = sorted({n for n in (1, 5, 10, 25, 50, 100, maximum, db.get_upload_limit(chat_id)) if n <= maximum})
    buttons = [InlineKeyboardButton(text=str(n), callback_data=f"set_upload_limit:{n}") for n in choices]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="interface_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "upload_limits")
async def cb_upload_limits(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Лимит меняет только администратор", show_alert=True)
        return
    chat_id = call.message.chat.id
    await call.answer()
    await call.message.answer(
        f"<b>Лимит загрузки</b>\nСейчас: {db.get_upload_limit(chat_id)}.\n"
        f"Занято: {db.count_outstanding(chat_id)}.\n\n"
        "Лимит учитывает все ожидающие и обрабатываемые сессии этого чата. "
        "Уменьшение лимита не удаляет ранее загруженные файлы.",
        reply_markup=upload_limits_kb(chat_id),
    )


@router.callback_query(F.data.startswith("set_upload_limit:"))
async def cb_set_upload_limit(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Лимит меняет только администратор", show_alert=True)
        return
    try:
        limit = int(call.data.partition(":")[2])
        db.set_upload_limit(call.message.chat.id, limit)
    except ValueError as exc:
        await call.answer(str(exc), show_alert=True)
        return
    await call.answer(f"Лимит: {limit}")
    await call.message.answer(
        f"Лимит загрузки: <b>{limit}</b>. Настройка сохранена.",
        reply_markup=interface_settings_kb(call.message.chat.id),
    )


@router.callback_query(F.data == "interface_settings")
async def cb_interface_settings(call: CallbackQuery):
    await call.answer()
    await cmd_interface_settings(call.message)


@router.callback_query(F.data == "toggle_compact_menu")
async def cb_toggle_compact_menu(call: CallbackQuery):
    compact = db.toggle_compact_menu(call.message.chat.id)
    await call.answer("Краткое меню" if compact else "Подробное меню")
    await call.message.edit_reply_markup(reply_markup=interface_settings_kb(call.message.chat.id))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await message.answer(PRIVACY_TEXT, reply_markup=interface_settings_kb(message.chat.id))


@router.callback_query(F.data == "show_privacy")
async def cb_privacy(call: CallbackQuery):
    await call.answer()
    await cmd_privacy(call.message)


@router.message(Command("limits"))
async def cmd_limits(message: Message):
    await message.answer(limits_text(message.chat.id), reply_markup=interface_settings_kb(message.chat.id))


@router.callback_query(F.data == "show_limits")
async def cb_limits(call: CallbackQuery):
    await call.answer()
    await cmd_limits(call.message)


@router.message(Command("id"))
async def cmd_id(message: Message):
    user = message.from_user
    if user:
        await message.answer(f"Ваш Telegram ID: <code>{user.id}</code>")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Команды</b>\n\n"
        "/start — главное меню\n"
        "/status — статус очереди\n"
        "/pause и /resume — приостановить или продолжить очередь\n"
        "/settings — вид меню\n"
        "/limits — ограничения файлов\n"
        "/privacy — информация о данных\n"
        "/id — ваш Telegram ID\n"
        "/cancel — отменить обработку\n\n"
        "Подробная справка доступна по кнопке «Помощь» в меню.",
        reply_markup=main_menu_kb(message.chat.id),
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await message.answer(
        menu_text(chat_id),
        reply_markup=main_menu_kb(chat_id),
    )


@router.callback_query(F.data == "btn_session_login")
async def cb_session_login(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "Отправьте файл <code>.session</code> или ZIP-архив с сессиями и парными JSON.\n"
        "Файлы будут обработаны в выбранном режиме.\n"
        "Миграция завершает старую сессию. Очистка удаляет данные аккаунта.",
        reply_markup=main_menu_kb(call.message.chat.id),
    )


@router.callback_query(F.data == "reset_devices")
async def cb_reset_devices(call: CallbackQuery):
    chat_id = call.message.chat.id
    worker = queue_worker_tasks.get(chat_id)
    if (worker is not None and not worker.done()) or chat_id in current_migration or any(
        m.chat_id == chat_id for m in active_migrations.values()
    ):
        await call.answer("Дождитесь завершения текущей обработки", show_alert=True)
        return
    db.requeue_interrupted(chat_id)
    db.set_clean_mode(chat_id, 4)
    queue_paused[chat_id] = True
    pending = db.count_queue(chat_id)
    await call.answer("Завершить другие сеансы")
    await call.message.answer(
        "🔐 <b>Завершить другие сеансы</b>\n\n"
        "Будут завершены все остальные сеансы ваших аккаунтов, включая приложения на других устройствах. "
        "Загруженная .session останется активной.\n"
        "Если Telegram не разрешит сброс (например, сессия слишком свежая), бот покажет причину.\n\n"
        + (f"В очереди: {pending}. Нажмите «Проверить и запустить»." if pending else
           "Отправьте .session или ZIP с сессиями и JSON, затем нажмите «Проверить и запустить»."),
        reply_markup=queued_review_kb() if pending else main_menu_kb(chat_id),
    )


@router.callback_query(F.data == "check_spam")
async def cb_check_spam(call: CallbackQuery, state: FSMContext):
    chat_id = call.message.chat.id
    worker = queue_worker_tasks.get(chat_id)
    live_worker = worker is not None and not worker.done()
    live_migration = (chat_id in current_migration or
                      any(m.chat_id == chat_id for m in active_migrations.values()))
    if not live_worker and not live_migration:
        # Recover rows left in "processing" by a stopped process without deleting inputs.
        db.requeue_interrupted(chat_id)
    db.set_clean_mode(chat_id, 3)
    pending = db.count_queue(chat_id)
    if pending:
        queue_paused[chat_id] = False
        start_queue_worker(chat_id, call.bot, state)
    await call.answer("Проверка спамблока")
    if pending:
        text = (f"🔎 <b>Проверка спамблока</b>\nСессий ожидает проверки: <b>{pending}</b>.\n"
                + ("Текущая обработка завершится, затем проверю ожидающие сессии.\n"
                   if live_worker or live_migration else "Проверка загруженных сессий запущена.\n"))
    else:
        text = ("🔎 <b>Проверка спамблока</b>\n\n"
                "Отправьте .session или ZIP с сессиями и JSON, затем нажмите «Проверить и запустить».\n")
    await call.message.answer(
        text +
        "Категории: без спамблока, вечный, временный, по гео, заморожен.\n"
        "Ответ про строгую реакцию на номера телефонов относится к категории «по гео».\n"
        "Неизвестный ответ или ошибка — «Не определено» с причиной.\n\n"
        "Бот отправит /start официальному @SpamBot. При неудаче разблокирует его и повторит проверку.\n"
        "Результаты — отдельные ZIP по категориям; исходные JSON-поля сохраняются.",
        reply_markup=main_menu_kb(chat_id),
    )


@router.callback_query(F.data == "cycle_clean_mode")
async def cb_cycle_clean_mode(call: CallbackQuery):
    chat_id = call.message.chat.id
    current = db.get_clean_mode(chat_id, settings.default_auto_code)
    new_mode = 2 if current == 1 else (0 if current == 2 else 1)
    db.set_clean_mode(chat_id, new_mode)

    await call.message.edit_text(menu_text(chat_id), reply_markup=main_menu_kb(chat_id))
    names = {1: "Миграция · авто", 2: "Быстрая очистка", 0: "Миграция · вручную"}
    await call.answer(f"Включен: {names[new_mode]}")


@router.message(Command("fast"))
async def cmd_fast(message: Message):
    db.set_clean_mode(message.chat.id, 2)
    await message.answer("Режим: <b>Быстрая очистка</b>. Удаляет данные аккаунта без переноса сессии.", reply_markup=main_menu_kb(message.chat.id))


@router.message(Command("auto"))
async def cmd_auto(message: Message):
    db.set_clean_mode(message.chat.id, 1)
    await message.answer("Режим: <b>Миграция · авто</b>. Старая сессия будет завершена.", reply_markup=main_menu_kb(message.chat.id))


@router.message(Command("manual"))
async def cmd_manual(message: Message):
    db.set_clean_mode(message.chat.id, 0)
    await message.answer("Режим: <b>Миграция · вручную</b>. Старая сессия будет завершена.", reply_markup=main_menu_kb(message.chat.id))


@router.callback_query(F.data == "skip_current")
async def cb_skip_current(call: CallbackQuery, state: FSMContext):
    chat_id = call.message.chat.id
    mig = next((m for m in active_migrations.values()
                if m.chat_id == chat_id and m.status_message.message_id == call.message.message_id), None)
    if mig is None:
        candidate = current_migration.get(chat_id)
        if candidate and candidate.status_message.message_id == call.message.message_id:
            mig = candidate
    if not mig:
        await call.answer("Нет активной сессии для пропуска.", show_alert=True)
        return

    mig.skip_requested = True
    stats["skipped"] += 1
    await call.answer("Сессия пропущена. Переход к следующей...")
    if mig.waiting_2fa or mig.waiting_manual_code:
        await cleanup_migration(mig, state)

    if db.count_queue(chat_id) > 0:
        start_queue_worker(chat_id, call.bot, state)


@router.message(Command("new", "create"))
@router.callback_query(F.data == "btn_new_session")
async def cmd_new(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id

    if chat_id in current_migration or db.count_queue(chat_id) > 0:
        text = "❌ Идет обработка. Дождитесь окончания или отмените текущую очередь."
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
    raw = (message.text or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if raw.startswith("8") and len(raw) == 11:
        raw = "+7" + raw[1:]
    elif not raw.startswith("+"):
        raw = "+" + raw

    if not re.fullmatch(r"\+\d{10,15}", raw):
        await message.answer("❌ Неверный формат. Пример: <code>+79001234567</code>")
        return

    phone = raw
    await state.update_data(pending_phone=phone)
    await message.answer(
        f"Номер: <code>{phone}</code>\nНажмите «Получить код», чтобы запросить код в Telegram.",
        reply_markup=login_code_kb(),
    )


async def start_new_session_code(message: Message, state: FSMContext, phone: str):
    chat_id = message.chat.id
    mig_id = str(uuid.uuid4())[:8]
    status = await message.answer(f"⏳ Запрашиваю код для <code>{phone}</code>...")
    mig = ActiveMigration(
        id=mig_id,
        chat_id=chat_id,
        phone=phone,
        status_message=status,
        is_new_session=True,
        waiting_manual_code=True,
    )
    current_migration[chat_id] = mig
    try:
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"new_{chat_id}_{mig_id}_"))
        new_client = get_configured_client(str(mig.temp_dir / "new_session"), mig.fingerprint, None)
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
        await status.edit_text(f"❌ Ошибка запроса: <code>{public_error(e)}</code>")
        await cleanup_migration(mig, state)


@router.callback_query(F.data == "request_login_code")
async def cb_request_login_code(call: CallbackQuery, state: FSMContext):
    chat_id = call.message.chat.id
    data = await state.get_data()
    phone = data.get("pending_phone")
    if not phone or not re.fullmatch(r"\+\d{10,15}", str(phone)):
        await call.answer("Сначала укажите номер телефона", show_alert=True)
        return
    if chat_id in current_migration or db.count_queue(chat_id) > 0:
        await call.answer("Сначала завершите текущую обработку", show_alert=True)
        return
    await state.update_data(pending_phone=None)
    await call.answer("Запрашиваю код")
    await start_new_session_code(call.message, state, str(phone))


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_all")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await clear_user_queue(chat_id, state, event.bot)
    stats["cancelled"] += 1

    text = "🛑 Очередь полностью сброшена, временные файлы удалены."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=main_menu_kb(chat_id))
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu_kb(chat_id))


# ====================== ЗАГРУЗКА .SESSION И .ZIP ======================

@router.message(F.document)
async def process_document_upload(message: Message, state: FSMContext):
    chat_id = message.chat.id
    doc: Document = message.document
    try:
        name = validate_upload_name(doc.file_name or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer("Файл превышает 20 МБ.")
        return
    remaining = db.get_upload_limit(chat_id) - db.count_outstanding(chat_id)
    if remaining <= 0:
        await message.answer("Очередь заполнена. Дождитесь обработки или сбросьте её.")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix=f"upload_{chat_id}_"))
    archive_file = temp_dir / name
    try:
        await message.bot.download(doc, destination=archive_file)
        if archive_file.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Файл превышает 20 МБ.")
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        await message.answer("Не удалось скачать файл. Проверьте размер и повторите отправку.")
        return

    queued_items = []

    if name.lower().endswith(".zip"):
        try:
            sessions = extract_session_archive(archive_file, temp_dir / "extracted", remaining)

            for s_path in sessions:
                json_path = s_path.with_suffix(".json")
                auto_pass = None
                proxy_dict = None
                if json_path.exists():
                    try:
                        j_data = json.loads(json_path.read_text(encoding="utf-8"))
                        auto_pass = j_data.get("twoFA") or j_data.get("password") or j_data.get("2fa")
                        proxy_dict = parse_session_proxy(j_data)
                    except Exception:
                        pass

                queued_items.append((
                    str(uuid.uuid4()), str(s_path), s_path.name,
                    str(auto_pass) if auto_pass else None, proxy_dict,
                ))

        except Exception as e:
            await message.answer("Не удалось прочитать ZIP: " + public_archive_error(e))
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
    else:
        queued_items.append((str(uuid.uuid4()), str(archive_file), doc.file_name, None, None))

    try:
        db.add_queue_items(chat_id, queued_items)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        error = html.escape(str(exc)) if isinstance(exc, ValueError) else "Не удалось сохранить очередь. Повторите загрузку."
        await message.answer(error)
        return

    added_count = len(queued_items)
    stats["started"] += added_count
    total_queue = db.count_queue(chat_id)
    queue_paused[chat_id] = True

    await message.answer(
        f"Сессий добавлено: <b>{added_count}</b>\n"
        f"В очереди: <b>{total_queue}</b>\n\n"
        f"Выбран режим: <b>{MODE_LABELS.get(db.get_clean_mode(chat_id), 'Миграция · вручную')}</b>.\n"
        + ("Будут завершены все другие сеансы. Загруженная сессия останется активной.\n"
         if db.get_clean_mode(chat_id) == 4 else
         "Проверка через @SpamBot; при сбое бот будет разблокирован для повторной попытки.\n"
         if db.get_clean_mode(chat_id) == 3 else
         "Миграция завершает старую сессию. Быстрая очистка удаляет данные аккаунта.\n")
        + "Обработка начнётся после вашего подтверждения.",
        reply_markup=queued_review_kb(),
    )

@router.message(MigrateStates.waiting_manual_code)
async def process_manual_code(message: Message, state: FSMContext):
    chat_id = message.chat.id
    code = (message.text or "").strip().replace(" ", "").replace("-", "")

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_manual_code:
        await message.answer("Нет сессий, ожидающих ввода кода.")
        return

    if not code.isdigit() or not (4 <= len(code) <= 8):
        await message.answer("❌ Код должен содержать от 4 до 8 цифр.")
        return

    status = await message.answer("⏳ Авторизация...")
    try:
        await mig.new_client.sign_in(phone=mig.phone, code=code, phone_code_hash=mig.phone_code_hash)

        # Вход подтвержден: деавторизуем старую сессию
        success, err = await terminate_old_session(mig.old_client)
        mig.old_session_terminated = success
        mig.old_session_error = err
        mig.old_client = None

        mig.waiting_manual_code = False
        await state.clear()
        await finalize(mig, status, state)

        if db.count_queue(chat_id) > 0:
            start_queue_worker(chat_id, message.bot, state)

    except SessionPasswordNeededError:
        if mig.auto_2fa_password:
            try:
                await mig.new_client.sign_in(password=mig.auto_2fa_password)

                success, err = await terminate_old_session(mig.old_client)
                mig.old_session_terminated = success
                mig.old_session_error = err
                mig.old_client = None

                mig.waiting_manual_code = False
                await state.clear()
                await finalize(mig, status, state)
                if db.count_queue(chat_id) > 0:
                    start_queue_worker(chat_id, message.bot, state)
                return
            except Exception:
                pass

        if mig.queue_db_id:
            stats["skipped"] += 1
            await status.edit_text("⏭ Сессия пропущена: требуется пароль 2FA, а в JSON нет подходящего пароля.")
            await cleanup_migration(mig, state)
            if db.count_queue(chat_id) > 0:
                start_queue_worker(chat_id, message.bot, state)
            return

        mig.waiting_manual_code = False
        mig.waiting_2fa = True
        mig.last_activity = time.time()
        await state.set_state(MigrateStates.waiting_password)
        await status.edit_text("🔒 Аккаунт защищен 2FA-паролем. Отправьте его:", reply_markup=action_kb(can_skip=True))
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await status.edit_text(f"❌ Неверный или истекший код: <code>{public_error(e)}</code>")
    except Exception as e:
        await status.edit_text(f"❌ Сбой авторизации: <code>{public_error(e)}</code>")


@router.message(MigrateStates.waiting_password)
async def process_2fa(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = (message.text or "").strip()

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_2fa:
        await message.answer("Нет сессий, ожидающих пароль 2FA.")
        return

    status = await message.answer("⏳ Проверка 2FA...")
    try:
        await mig.new_client.sign_in(password=password)

        # 2FA подтвержден: деавторизуем старую сессию
        success, err = await terminate_old_session(mig.old_client)
        mig.old_session_terminated = success
        mig.old_session_error = err
        mig.old_client = None

        mig.waiting_2fa = False
        await state.clear()
        await finalize(mig, status, state)

        if db.count_queue(chat_id) > 0:
            start_queue_worker(chat_id, message.bot, state)

    except PasswordHashInvalidError:
        await status.edit_text("❌ Пароль 2FA не подходит. Попробуйте еще раз:")
    except Exception as e:
        await status.edit_text(f"❌ Ошибка 2FA: <code>{public_error(e)}</code>")
        await cleanup_migration(mig, state)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    with sqlite3.connect(settings.db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM queue WHERE status = 'pending'")
        total_in_db = cur.fetchone()[0]

    await message.answer(
        f"<b>Статистика</b>\n\n"
        f"• Всего запусков: {stats['started']}\n"
        f"• Успешно очищено: {stats['success']}\n"
        f"• Пропущено оператором: {stats['skipped']}\n"
        f"• Ошибок обработки: {stats['failed']}\n"
        f"• Отменено: {stats['cancelled']}\n"
        f"• В SQLite очереди: {total_in_db}"
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


# ====================== ТОЧКА ВХОДА И SHUTDOWN ======================

async def shutdown(dp: Dispatcher, bot: Bot):
    logger.info("Получен сигнал завершения работы. Отключение сессий...")
    tasks = [task for task in queue_worker_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    migrations = {m.id: m for m in [*active_migrations.values(), *current_migration.values()]}
    for mig in migrations.values():
        await cleanup_migration(mig)
    await bot.session.close()
    logger.info("Все ресурсы корректно освобождены.")


async def main():
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    watchdog = asyncio.create_task(watchdog_task(bot))
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="status", description="Статус очереди"),
            BotCommand(command="archives", description="Получить сохранённые архивы"),
            BotCommand(command="pause", description="Пауза очереди"),
            BotCommand(command="resume", description="Продолжить очередь"),
            BotCommand(command="help", description="Меню и помощь"),
            BotCommand(command="settings", description="Настройки интерфейса"),
            BotCommand(command="limits", description="Ограничения файлов"),
            BotCommand(command="privacy", description="О данных"),
            BotCommand(command="id", description="Мой Telegram ID"),
            BotCommand(command="cancel", description="Отменить обработку"),
        ])
        logger.info("Бот запущен")
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await shutdown(dp, bot)
        await dp.storage.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")



