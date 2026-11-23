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
    return (f'Версия: <code>new-clean-logout-3</code>\nКоммит: <code>{commit}</code>\n'
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

