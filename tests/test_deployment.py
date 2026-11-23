"""No real credentials, network calls or account sessions are used."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot
from launch import prepare_environment


VALID = {'BOT_TOKEN': '123456:offline_test_token', 'API_ID': '123',
         'API_HASH': 'offline_hash', 'ALLOWED_IDS': '123,456'}


@pytest.mark.parametrize('key', ['ALLOWED_IDS', 'BOT_TOKEN', 'API_ID', 'API_HASH'])
def test_missing_config_is_actionable_and_secret_free(key):
    env = VALID.copy()
    env.pop(key)
    with pytest.raises(ValueError) as exc:
        prepare_environment(env)
    assert key in str(exc.value) and 'offline' not in str(exc.value)


@pytest.mark.parametrize('ids', ['123,456', '[123,456]'])
def test_old_and_new_allowlist_names_are_compatible(ids):
    env = {k: v for k, v in VALID.items() if k != 'ALLOWED_IDS'}
    env['ADMIN_IDS'] = ids
    prepare_environment(env)
    with patch.dict('os.environ', env, clear=True):
        config = bot.Settings(_env_file=None)
    assert config.admin_ids == {123, 456}


@pytest.mark.parametrize('ident,chat_type,allowed', [(123, 'private', True), (999, 'private', False), (123, 'group', False)])
@pytest.mark.asyncio
async def test_private_access_gate(ident, chat_type, allowed):
    message = NS(chat=NS(type=chat_type), from_user=NS(id=ident))
    handler = AsyncMock()
    with patch.object(bot.settings, 'admin_ids', {123}):
        await bot.private_access(handler, message, {})
    assert handler.await_count == int(allowed)


@pytest.mark.asyncio
async def test_destructive_resume_shows_plan_and_does_not_start_worker():
    call = NS(message=NS(chat=NS(id=123), answer=AsyncMock()), answer=AsyncMock())
    for mode in (2, 4):
        with patch.object(bot.db, 'get_clean_mode', return_value=mode), patch.object(bot, 'start_queue_worker') as start:
            await bot.cb_resume_queue(call, AsyncMock())
        start.assert_not_called()
        assert call.message.answer.call_args.args[0] == bot.DESTRUCTIVE_PLANS[mode]


@pytest.mark.asyncio
async def test_confirmation_starts_only_same_mode():
    call = NS(data='confirm_operation:2', message=NS(chat=NS(id=123), edit_text=AsyncMock()),
              answer=AsyncMock(), bot=AsyncMock())
    with patch.object(bot.db, 'get_clean_mode', return_value=4), patch.object(bot, 'start_queue_worker') as start:
        await bot.cb_confirm_operation(call, AsyncMock())
    start.assert_not_called()
    with patch.object(bot.db, 'get_clean_mode', return_value=2), patch.object(bot, 'menu_text', return_value='menu'), \
         patch.object(bot, 'main_menu_kb', return_value=None), patch.object(bot, 'start_queue_worker') as start:
        await bot.cb_confirm_operation(call, AsyncMock())
    start.assert_called_once()
    assert bot.confirmed_modes.pop(123) == 2


@pytest.mark.parametrize('mode', [2, 4])
@pytest.mark.asyncio
async def test_direct_worker_start_cannot_bypass_confirmation(mode):
    bot.confirmed_modes.pop(456, None)
    bot.queue_paused[456] = False
    with patch.object(bot.db, 'get_clean_mode', return_value=mode):
        assert bot.start_queue_worker(456, AsyncMock(), AsyncMock()) is None
    assert bot.queue_paused[456]


@pytest.mark.asyncio
@pytest.mark.parametrize('save_fails', [False, True])
async def test_migration_never_cleans_resets_or_logs_out(tmp_path, save_fails):
    (tmp_path / 'new_session.session').write_bytes(b'offline fixture')
    new = AsyncMock()
    new.is_user_authorized.return_value = True
    old = AsyncMock()
    mig = bot.ActiveMigration(id='offline', chat_id=123, old_client=old, new_client=new,
                              phone='masked_fixture', temp_dir=tmp_path)
    message = NS(bot=AsyncMock(), answer=AsyncMock())
    delivery = AsyncMock(side_effect=OSError('offline save failure') if save_fails else None)
    with patch.object(bot, 'deep_clean_account') as clean, patch.object(bot, 'terminate_old_session') as terminate, \
         patch.object(bot, 'create_metadata', new=AsyncMock(return_value={'id':123})), \
         patch.object(bot, 'deliver_account_archive', delivery), \
         patch.object(bot, 'cleanup_migration', new=AsyncMock()), \
         patch.object(bot.db, 'count_outstanding', return_value=1):
        await bot.finalize(mig, message)
    clean.assert_not_called()
    terminate.assert_not_called()
    new.assert_not_called()  # No ResetAuthorizations or other mutation RPC.
    old.assert_not_called()
    assert list(tmp_path.glob('*.zip'))
    delivery.assert_awaited_once()
    if save_fails:
        assert mig.temp_dir is None  # Cleanup must preserve the only ZIP copy.


@pytest.mark.asyncio
async def test_cleanup_button_selects_mode_but_does_not_process():
    call = NS(message=NS(chat=NS(id=789), answer=AsyncMock()), answer=AsyncMock())
    with patch.object(bot, 'db') as database, patch.object(bot, 'start_queue_worker') as start:
        database.count_queue.return_value = 0
        database.get_clean_mode.return_value = 2
        database.compact_menu.return_value = False
        await bot.cb_select_cleanup(call)
    database.set_clean_mode.assert_called_once_with(789, 2)
    start.assert_not_called()
    assert bot.queue_paused[789]
    assert 'платёжные' in call.message.answer.call_args.args[0]
    assert '30 дней' in call.message.answer.call_args.args[0]


def test_combined_is_default_and_cleanup_has_own_main_menu_button():
    assert bot.settings.default_auto_code == 5
    with patch.object(bot.db, 'get_clean_mode', return_value=2):
        buttons = [button for row in bot.main_menu_kb(123).inline_keyboard for button in row]
    assert any(button.callback_data == 'select_cleanup' for button in buttons)


def test_safe_log_filter_masks_phone_and_exception_message():
    import logging
    record = logging.LogRecord('test', logging.ERROR, '', 1, 'Ошибка для +12345678901', (),
                               (ValueError, ValueError('secret-login-hash'), None))
    bot.SafeLogFilter().filter(record)
    assert '+12345678901' not in record.getMessage()
    assert 'secret-login-hash' not in record.getMessage()
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_floodwait_stops_deep_cleanup_before_next_mutation():
    from telethon.errors import FloodWaitError
    client = AsyncMock(side_effect=FloodWaitError(None, capture=120))
    with pytest.raises(FloodWaitError):
        await bot.deep_clean_account(client, AsyncMock())
    assert client.await_count == 1
    client.get_me.assert_not_called()
