"""Ordering and durability tests; all sessions are synthetic byte fixtures."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest
from telethon import errors
from telethon.tl.functions.auth import LogOutRequest
from telethon.tl.functions.updates import GetStateRequest

import bot
from session_results import persist_new_session

REAL_DEEP_CLEAN = bot.deep_clean_account


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    root = tmp_path / 'volume'
    root.mkdir()
    monkeypatch.setenv('DATA_DIR', str(root))
    database = bot.Database(str(root / 'queue.sqlite'))
    monkeypatch.setattr(bot, 'db', database)
    work = root / 'tmp' / 'work'
    work.mkdir(parents=True)
    (work / 'new_session.session').write_bytes(b'new-authorization-fixture')
    source = root / 'original.session'
    source.write_bytes(b'original-source-fixture')
    old, new = AsyncMock(return_value=True), AsyncMock(return_value=True)
    for client, key in ((old, b'o' * 256), (new, b'n' * 256)):
        client.session = NS(auth_key=NS(key=key))
        client.is_connected = MagicMock(return_value=True)
        client.is_user_authorized.return_value = True
        client.get_me.return_value = NS(id=123, phone='10000000000', deleted=False,
                                         username='', first_name='Fixture', last_name='', premium=False)
    events = []
    async def clean(client, reporter):
        assert client is new
        assert list((root / 'new_sessions').rglob('before_cleanup/account.session'))
        assert len(database.archives(123, True)) == 1
        events.append('clean-new')
        return bot.CleanReport()
    clean_mock = AsyncMock(side_effect=clean)
    monkeypatch.setattr(bot, 'deep_clean_account', clean_mock)
    async def rpc(request):
        if isinstance(request, LogOutRequest):
            assert events == ['clean-new']
            assert list((root / 'new_sessions').rglob('after_cleanup/account.session'))
            rows = database.archives(123, True)
            assert len(rows) == 1
            with zipfile.ZipFile(io.BytesIO(rows[0][2])) as archive:
                assert archive.read('account.session') == b'new-authorization-fixture'
            events.append('logout-source')
        else:
            assert isinstance(request, GetStateRequest)
        return True
    old.side_effect = rpc
    message = NS(bot=AsyncMock(), edit_text=AsyncMock(), answer=AsyncMock())
    mig = bot.ActiveMigration(id='combinedfixture', chat_id=123, old_client=old, new_client=new,
                              phone='fixture', source_user_id=123, temp_dir=work, operation_mode=5)
    bot.queue_paused[123] = False
    return NS(root=root, database=database, source=source, old=old, new=new, clean=clean_mock,
              message=message, mig=mig, events=events, work=work)


@pytest.mark.asyncio
async def test_new_login_clean_save_then_logout_only_source(scenario):
    s = scenario
    await bot.finalize(s.mig, s.message)
    assert s.events == ['clean-new', 'logout-source']
    assert s.mig.old_session_terminated
    s.new.assert_not_awaited()  # Neither logOut(new) nor resetAuthorizations(new).
    assert not s.work.exists()
    assert s.source.read_bytes() == b'original-source-fixture'
    saved = list((s.root / 'new_sessions').rglob('account.session'))
    assert len(saved) == 2 and all(path.read_bytes() == b'new-authorization-fixture' for path in saved)
    for path in (s.root / 'new_sessions').rglob('account.json'):
        assert json.loads(path.read_text())['session_file'] == 'account.session'
    assert len(s.database.archives(123, True)) == 1
    assert s.message.bot.send_document.await_count == 1


@pytest.mark.asyncio
async def test_send_failure_keeps_new_files_and_archive_retrievable(scenario):
    s = scenario
    s.message.bot.send_document.side_effect = OSError('synthetic network failure')
    await bot.finalize(s.mig, s.message)
    assert s.mig.old_session_terminated
    assert len(s.database.archives(123)) == 1
    assert list((s.root / 'new_sessions').rglob('after_cleanup/account.session'))
    s.new.assert_not_awaited()
    s.message.bot.send_document.side_effect = None
    await bot.try_flush_queue_archives(123, s.message.bot)
    assert s.database.archives(123) == []


@pytest.mark.parametrize('failure_stage', ['initial_file', 'initial_db', 'final_file', 'final_db'])
@pytest.mark.asyncio
async def test_storage_failure_never_logs_out_source_or_loses_new_session(scenario, monkeypatch, failure_stage):
    s = scenario
    if failure_stage == 'initial_file':
        monkeypatch.setattr(bot, 'persist_new_session', MagicMock(side_effect=OSError('disk full')))
    elif failure_stage == 'initial_db':
        monkeypatch.setattr(s.database, 'save_archive', MagicMock(side_effect=OSError('disk full')))
    elif failure_stage == 'final_file':
        def persist(*args):
            if args[3] == 'after_cleanup':
                raise OSError('disk full')
            return persist_new_session(*args)
        monkeypatch.setattr(bot, 'persist_new_session', persist)
    else:
        monkeypatch.setattr(s.database, 'replace_archive', MagicMock(side_effect=OSError('disk full')))
    await bot.finalize(s.mig, s.message)
    s.old.assert_not_awaited()
    s.new.assert_not_awaited()
    assert not s.mig.old_session_terminated
    assert s.source.exists()
    assert list(s.root.rglob('*.session'))  # Original and at least one new copy below.
    assert any(path.read_bytes() == b'new-authorization-fixture' for path in s.root.rglob('*.session'))
    if failure_stage.startswith('initial'):
        s.clean.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_cleanup_preserves_source_authorization(scenario):
    s = scenario
    s.clean.side_effect = None
    s.clean.return_value = bot.CleanReport(errors=['SyntheticError'], dialogs_failed=1)
    await bot.finalize(s.mig, s.message)
    s.old.assert_not_awaited()
    assert not s.mig.old_session_terminated
    assert list((s.root / 'new_sessions').rglob('after_cleanup/account.session'))
    assert 'частичная' in s.message.bot.send_message.await_args_list[0].args[1]


@pytest.mark.asyncio
async def test_floodwait_during_cleanup_preserves_source_and_saved_new(scenario):
    s = scenario
    s.clean.side_effect = errors.FloodWaitError(None, capture=90)
    await bot.finalize(s.mig, s.message)
    s.old.assert_not_awaited()
    assert bot.queue_paused[123]
    assert list((s.root / 'new_sessions').rglob('before_cleanup/account.session'))
    assert len(s.database.archives(123, True)) == 1


@pytest.mark.asyncio
async def test_cancellation_keeps_new_session_and_disconnects_clients(scenario):
    s = scenario
    started = asyncio.Event()
    async def cleaning(*args):
        started.set()
        await asyncio.Event().wait()
    s.clean.side_effect = cleaning
    task = asyncio.create_task(bot.finalize(s.mig, s.message))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s.old.assert_not_awaited()
    assert s.old.disconnect.await_count >= 1
    assert s.new.disconnect.await_count >= 1
    assert list((s.root / 'new_sessions').rglob('before_cleanup/account.session'))
    assert len(s.database.archives(123)) == 1


@pytest.mark.parametrize('mismatch', ['account', 'auth_key'])
@pytest.mark.asyncio
async def test_wrong_account_or_reused_authorization_cannot_clean_or_logout(scenario, mismatch):
    s = scenario
    if mismatch == 'account':
        s.new.get_me.return_value.id = 999
    else:
        s.new.session.auth_key.key = s.old.session.auth_key.key
    await bot.finalize(s.mig, s.message)
    s.clean.assert_not_awaited()
    s.old.assert_not_awaited()
    assert (s.work / 'new_session.session').exists()


@pytest.mark.asyncio
async def test_old_logout_floodwait_is_not_retried_and_new_stays_saved(scenario):
    s = scenario
    async def rpc(request):
        if isinstance(request, LogOutRequest):
            raise errors.FloodWaitError(None, capture=60)
        return True
    s.old.side_effect = rpc
    await bot.finalize(s.mig, s.message)
    requests = [call.args[0] for call in s.old.await_args_list]
    assert sum(isinstance(request, LogOutRequest) for request in requests) == 1
    assert not s.mig.old_session_terminated
    assert '60' in s.mig.old_session_error
    s.new.assert_not_awaited()
    assert list((s.root / 'new_sessions').rglob('after_cleanup/account.session'))


@pytest.mark.asyncio
async def test_confirmation_and_menu_make_combined_mode_explicit(monkeypatch):
    assert 'ТОЛЬКО' in bot.DESTRUCTIVE_PLANS[5]
    assert 'сохранятся' in bot.DESTRUCTIVE_PLANS[5]
    buttons = [button.callback_data for row in bot.main_menu_kb(123).inline_keyboard for button in row]
    assert 'select_combined' in buttons
    monkeypatch.setattr(bot.db, 'get_clean_mode', lambda *args: 5)
    call = NS(message=NS(chat=NS(id=123), answer=AsyncMock()), answer=AsyncMock())
    starter = MagicMock()
    monkeypatch.setattr(bot, 'start_queue_worker', starter)
    await bot.cb_resume_queue(call, AsyncMock())
    starter.assert_not_called()
    assert call.message.answer.await_args.args[0] == bot.DESTRUCTIVE_PLANS[5]


@pytest.mark.asyncio
async def test_cancel_while_verifying_keeps_only_local_new_file(scenario):
    s = scenario
    started = asyncio.Event()
    async def verify():
        started.set()
        await asyncio.Event().wait()
    s.new.is_user_authorized.side_effect = verify
    task = asyncio.create_task(bot.finalize(s.mig, s.message))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (s.work / 'new_session.session').exists()
    s.old.assert_not_awaited()
    s.clean.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_send_preserves_success_and_has_no_hidden_restart_retry(monkeypatch):
    from telethon.tl.types.auth import SentCodeSuccess, Authorization
    from telethon.tl.types import User
    from telethon.tl.functions.auth import SendCodeRequest
    result = SentCodeSuccess(Authorization(User(id=123)))
    rpc = AsyncMock(return_value=result)
    async def call(self, request):
        assert isinstance(request, SendCodeRequest)
        return await rpc(request)
    monkeypatch.setattr(bot.ManagedClient, '__call__', call)
    client = bot.ManagedClient(None, 123, 'offline_hash')
    assert await client.send_code_request('10000000000') is result
    rpc.side_effect = errors.AuthRestartError(None)
    with pytest.raises(errors.AuthRestartError):
        await client.send_code_request('10000000000')
    assert rpc.await_count == 2  # One RPC per explicit invocation.


@pytest.mark.parametrize('kind', ['code', '2fa'])
@pytest.mark.asyncio
async def test_manual_input_keeps_selected_combined_mode(scenario, monkeypatch, kind):
    s = scenario
    s.mig.waiting_manual_code = kind == 'code'
    s.mig.waiting_2fa = kind == '2fa'
    s.mig.phone_code_hash = 'offline_hash'
    monkeypatch.setitem(bot.current_migration, 123, s.mig)
    combined = AsyncMock()
    monkeypatch.setattr(bot, 'finalize_combined', combined)
    monkeypatch.setattr(bot, 'start_queue_worker', MagicMock())
    message = NS(chat=NS(id=123), text='12345' if kind == 'code' else 'offline-password', answer=AsyncMock())
    if kind == 'code':
        await bot.process_manual_code(message, AsyncMock())
    else:
        await bot.process_2fa(message, AsyncMock())
    combined.assert_awaited_once()
    assert combined.await_args.args[0].operation_mode == 5
    s.old.assert_not_awaited()


@pytest.mark.parametrize('delivery', ['app_2fa', 'already_authorized'])
@pytest.mark.asyncio
async def test_combined_login_handles_2fa_and_actual_sent_code_success(scenario, monkeypatch, delivery):
    from telethon.tl.types.auth import SentCode, SentCodeTypeApp, SentCodeSuccess, Authorization
    from telethon.tl.types import User
    s = scenario
    s.database.set_clean_mode(123, 5)
    monkeypatch.setitem(bot.confirmed_modes, 123, 5)
    monkeypatch.setattr(bot, 'get_configured_client', MagicMock(side_effect=[s.old, s.new]))
    monkeypatch.setattr(bot, 'login_message_boundaries', AsyncMock(return_value={777000: 7}))
    monkeypatch.setattr(bot, 'poll_login_code', AsyncMock(return_value='12345'))
    combined = AsyncMock()
    monkeypatch.setattr(bot, 'finalize_combined', combined)
    if delivery == 'app_2fa':
        s.new.send_code_request.return_value = SentCode(SentCodeTypeApp(5), 'offline_hash')
        s.new.sign_in.side_effect = [errors.SessionPasswordNeededError(None), None]
    else:
        s.new.send_code_request.return_value = SentCodeSuccess(Authorization(User(id=123)))
    item = {'id':'fixture', 'chat_id':123, 'original_name':'fixture.session', 'file_path':str(s.source),
            'proxy_json':None, 'auto_2fa':'offline-password', '_operation_mode':5}
    await bot.process_one_session(item, AsyncMock(), AsyncMock())
    combined.assert_awaited_once()
    assert combined.await_args.args[0].operation_mode == 5
    assert combined.await_args.args[0].source_user_id == 123
    if delivery == 'app_2fa':
        assert s.new.sign_in.await_count == 2
        assert s.new.sign_in.await_args.kwargs == {'password':'offline-password'}
    else:
        s.new.sign_in.assert_not_awaited()
    s.old.assert_not_awaited()


@pytest.mark.asyncio
async def test_archives_reindexes_saved_new_session_after_sqlite_save_failure(scenario, monkeypatch):
    s = scenario
    original_save = s.database.save_archive
    monkeypatch.setattr(s.database, 'save_archive', MagicMock(side_effect=OSError('disk full')))
    await bot.finalize(s.mig, s.message)
    s.old.assert_not_awaited()
    assert not s.database.archives(123, True)
    monkeypatch.setattr(s.database, 'save_archive', original_save)
    bot.recover_saved_new_sessions(999)
    assert not s.database.archives(999, True)
    bot.recover_saved_new_sessions(123)
    bot.recover_saved_new_sessions(123)
    assert len(s.database.archives(123, True)) == 1


@pytest.mark.asyncio
async def test_app_code_timeout_offers_manual_input_without_source_logout(scenario, monkeypatch):
    from telethon.tl.types.auth import SentCode, SentCodeTypeApp
    s = scenario
    monkeypatch.setitem(bot.confirmed_modes, 123, 5)
    monkeypatch.setattr(bot, 'get_configured_client', MagicMock(side_effect=[s.old, s.new]))
    monkeypatch.setattr(bot, 'login_message_boundaries', AsyncMock(return_value={777000:7}))
    monkeypatch.setattr(bot, 'poll_login_code', AsyncMock(side_effect=TimeoutError()))
    s.new.send_code_request.return_value = SentCode(SentCodeTypeApp(5), 'offline_hash')
    item = {'id':'fixture', 'chat_id':123, 'original_name':'fixture.session', 'file_path':str(s.source),
            'proxy_json':None, 'auto_2fa':None, '_operation_mode':5}
    state = AsyncMock()
    try:
        await bot.process_one_session(item, AsyncMock(), state)
        waiting = bot.current_migration[123]
        assert waiting.waiting_manual_code and waiting.operation_mode == 5
        s.new.sign_in.assert_not_awaited()
        s.old.assert_not_awaited()
    finally:
        waiting = bot.current_migration.get(123)
        if waiting:
            await bot.cleanup_migration(waiting, state)


@pytest.mark.asyncio
async def test_combined_with_real_cleanup_never_resets_all_authorizations(scenario, monkeypatch):
    s = scenario
    monkeypatch.setattr(bot, 'deep_clean_account', REAL_DEEP_CLEAN)
    requested = []
    async def new_rpc(request, **kwargs):
        name = type(request).__name__
        requested.append(name)
        assert name not in {'ResetAuthorizationsRequest', 'LogOutRequest'}
        return {'GetUserPhotosRequest':NS(photos=[]), 'GetContactsRequest':NS(users=[]),
                'GetDialogFiltersRequest':NS(filters=[]), 'GetAllStickersRequest':NS(sets=[])}.get(name, True)
    async def dialogs():
        if False:
            yield
    s.new.side_effect = new_rpc
    s.new.iter_dialogs = dialogs
    async def source_rpc(request):
        if isinstance(request, LogOutRequest):
            assert 'ClearAllDraftsRequest' in requested
            assert 'ResetWebAuthorizationsRequest' in requested
            assert list((s.root / 'new_sessions').rglob('after_cleanup/account.session'))
            assert len(s.database.archives(123, True)) == 1
        return True
    s.old.side_effect = source_rpc
    await bot.finalize(s.mig, s.message)
    assert s.mig.old_session_terminated
    assert 'UpdateProfileRequest' in requested
    assert 'DeleteHistoryRequest' in requested
    assert 'ResetAuthorizationsRequest' not in requested
    assert 'LogOutRequest' not in requested
