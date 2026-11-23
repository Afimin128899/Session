"""Railway entry point. Validate configuration without importing account clients."""
import asyncio
import logging
import os
import re
import sys


def prepare_environment(env):
    required = ('BOT_TOKEN', 'API_ID', 'API_HASH')
    missing = [key for key in required if not env.get(key, '').strip()]
    if not (env.get('ALLOWED_IDS', '').strip() or env.get('ADMIN_IDS', '').strip()):
        missing.append('ALLOWED_IDS')
    if missing:
        raise ValueError('В Railway → сервис → Variables задайте: ' + ', '.join(missing) + '. Файл .env из Git не загружается.')
    ids = env.get('ALLOWED_IDS') or env.get('ADMIN_IDS')
    ids = ids.strip().strip('[] ')
    if any(not re.fullmatch(r'[1-9][0-9]*', part.strip()) for part in ids.split(',')):
        raise ValueError('ALLOWED_IDS/ADMIN_IDS: нужны положительные числовые Telegram user ID через запятую.')
    env['ALLOWED_IDS'] = ids
    if not re.fullmatch(r'[1-9][0-9]*', env['API_ID'].strip()):
        raise ValueError('API_ID должен быть положительным целым числом.')
    from aiogram.utils.token import validate_token, TokenValidationError
    try:
        validate_token(env['BOT_TOKEN'].strip())
    except TokenValidationError:
        raise ValueError('BOT_TOKEN имеет неверный формат. Задайте актуальный токен в Railway Variables.') from None
    env['BOT_TOKEN'] = env['BOT_TOKEN'].strip()
    env.setdefault('DATA_DIR', '/data')
    if not env['DATA_DIR'].strip():
        raise ValueError('DATA_DIR не должен быть пустым; для Railway Volume используйте /data.')


def run():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        prepare_environment(os.environ)
    except ValueError as exc:
        logging.error('%s', exc)
        return 2
    try:
        import bot
        asyncio.run(bot.main())
    except (KeyboardInterrupt, SystemExit):
        return 0
    except Exception as exc:
        # Pydantic validation errors may include supplied secret values.
        logging.error('Запуск остановлен: %s. Проверьте Variables, токен, Volume и отсутствие другого polling-процесса.', type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(run())
