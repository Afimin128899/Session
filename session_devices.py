"""Terminate other Telegram authorizations while retaining the supplied session."""
import asyncio
from dataclasses import dataclass

from telethon.errors import FreshResetAuthorisationForbiddenError, FloodWaitError
from telethon.tl.functions.auth import ResetAuthorizationsRequest


LABELS = {'reset': 'Другие сеансы завершены', 'failed': 'Другие сеансы не завершены'}


@dataclass(frozen=True)
class ResetResult:
    category: str
    detail: str


async def reset_other_sessions(client, *, timeout: float = 60.0) -> ResetResult:
    try:
        async with asyncio.timeout(timeout):
            result = await client(ResetAuthorizationsRequest(), flood_sleep_threshold=0)
        if result:
            return ResetResult('reset', 'Telegram подтвердил завершение других сеансов. Загруженная сессия сохранена.')
        return ResetResult('failed', 'Telegram не подтвердил завершение других сеансов.')
    except FreshResetAuthorisationForbiddenError:
        return ResetResult('failed', 'Telegram запрещает сброс других сеансов из свежей сессии. Повторите позже или используйте более старую сессию.')
    except FloodWaitError as exc:
        return ResetResult('failed', f'Telegram требует подождать {exc.seconds} с перед следующей попыткой.')
    except TimeoutError:
        return ResetResult('failed', 'Время ожидания ответа Telegram истекло. Результат сброса не подтверждён.')
    except Exception as exc:
        return ResetResult('failed', 'Ошибка завершения других сеансов: ' + type(exc).__name__)
