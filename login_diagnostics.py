"""Non-sensitive diagnostics for the running deployment and code delivery."""
import hashlib
import html
import os
from pathlib import Path


def runtime_version(source):
    commit = os.environ.get('RAILWAY_GIT_COMMIT_SHA', '')
    commit = commit[:12] if commit and all(c in '0123456789abcdefABCDEF' for c in commit) else 'неизвестен'
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()[:12]
    service = html.escape(os.environ.get('RAILWAY_SERVICE_NAME', 'локальный запуск')[:80])
    return (f'Версия: <code>value-diagnostics-4</code>\nКоммит: <code>{commit}</code>\n'
            f'Код: <code>{digest}</code>\nСервис: <code>{service}</code>')


def delivery_description(result):
    kind = type(getattr(result, 'type', None)).__name__
    labels = {
        'SentCodeTypeApp': 'сообщение в Telegram (777000)',
        'SentCodeTypeSms': 'SMS',
        'SentCodeTypeCall': 'телефонный звонок',
        'SentCodeTypeFlashCall': 'звонок для подтверждения',
        'SentCodeTypeMissedCall': 'пропущенный звонок',
        'SentCodeTypeEmailCode': 'электронная почта',
        'SentCodeTypeSetUpEmailRequired': 'необходимо настроить почту для входа',
        'SentCodeTypeFragmentSms': 'получение кода через Fragment',
    }
    return labels.get(kind, 'другой способ подтверждения (' + html.escape(kind) + ')')



def value_error_reason(exc):
    """Return a fixed reason code, never the original exception message."""
    message = str(exc).lower()
    if 'unknown proxy protocol' in message:
        return 'proxy_protocol', 'Некорректный тип прокси; проверьте proxy в JSON и переменные PROXY_*.'
    if 'input entity' in message or 'cannot find any entity' in message:
        return 'entity_unavailable', 'Telegram-сущность не найдена в сессии; служебный чат может быть недоступен.'
    if 'api_id' in message or 'api_hash' in message:
        return 'api_config', 'Проверьте API_ID и API_HASH в Railway Variables.'
    if 'failed to get dc' in message:
        return 'dc_unavailable', 'Не удалось определить датацентр Telegram; проверьте подключение и сессию.'
    return 'value_unknown', 'ValueError: некорректное значение. Точная причина определяется по этапу и месту сбоя в журнале.'


def safe_error_frames(traceback):
    """Only code locations: no exception text, source lines or local variables."""
    import traceback as module
    frames = module.extract_tb(traceback)
    return ' > '.join(f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in frames[-8:])
