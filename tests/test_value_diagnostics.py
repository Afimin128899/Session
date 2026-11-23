"""Regression checks: no real accounts or network connections."""
import asyncio
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import User, InputPeerUser

import bot
from login_diagnostics import value_error_reason, safe_error_frames
from proxy_config import ProxyConfigError, normalize_proxy


@pytest.mark.parametrize('kind,expected', [('SOCKS5','socks5'),('socks5h','socks5'),(2,'socks5'),('1','socks4'),('http','http')])
def test_proxy_normalization_supports_explicit_telethon_type(kind, expected):
    value = normalize_proxy({'proxy_type':kind, 'addr':'proxy.invalid', 'port':'1080', 'password':'private'})
    assert value['proxy_type'] == expected and value['port'] == 1080
    assert value['password'] == 'private'


@pytest.mark.parametrize('values', [{'proxy_type':'unknown-private'}, {'port':'private-password'}, {'port':0},
                                   {'port':1.5}, {'addr':''}, {'rdns':'private'}])
def test_invalid_proxy_reports_format_without_echoing_value(values):
    with pytest.raises(ProxyConfigError) as exc:
        normalize_proxy({'proxy_type':'socks5', 'addr':'proxy.invalid', 'port':1080, **values})
    assert 'private' not in str(exc.value)


def test_value_error_hints_are_static_and_never_echo_secrets():
    for message, reason in [('Unknown proxy protocol type: private-password', 'proxy_protocol'),
                            ('Could not find the input entity: private-hash','entity_unavailable'),
                            ('api_hash private-secret','api_config'), ('private-secret','value_unknown')]:
        code, text = value_error_reason(ValueError(message))
        assert code == reason and 'private' not in text
        assert bot.public_error(ValueError(message)) != 'Value'


def test_safe_frame_log_contains_location_but_not_error_text_or_source():
    try:
        raise ValueError('private-code-and-password')
    except ValueError as exc:
        frames = safe_error_frames(exc.__traceback__)
        record = logging.LogRecord('test', logging.ERROR, '', 1, 'task=fixture stage=source_connect', (),
                                   (type(exc), exc, exc.__traceback__))
        bot.SafeLogFilter().filter(record)
    assert 'test_value_diagnostics.py:' in frames
    assert 'private' not in frames and 'private' not in record.getMessage()
    assert 'source_connect' in record.getMessage() and record.exc_info is None


@pytest.mark.asyncio
async def test_missing_primary_cache_is_resolved_from_accessible_dialog():
    primary = User(id=777000, access_hash=1)
    client = AsyncMock()
    async def messages(peer, **kwargs):
        if peer == 777000:
            raise ValueError('Could not find the input entity')
        assert peer is primary
        return [NS(id=50)]
    client.get_messages.side_effect = messages
    client.get_dialogs.return_value = [NS(entity=primary)]
    client.get_input_entity.return_value = InputPeerUser(777000, 1)
    assert await bot.login_message_boundaries(client) == {777000:50}
    assert client.get_messages.await_count == 2  # One failed lookup, one real read, no duplicate alias read.


@pytest.mark.asyncio
async def test_primary_unavailable_does_not_hide_secondary_boundary():
    client = AsyncMock()
    async def messages(peer, **kwargs):
        if peer == 777000:
            raise ValueError('not in cache')
        return [NS(id=7)]
    client.get_messages.side_effect = messages
    client.get_dialogs.return_value = []
    client.get_input_entity.return_value = InputPeerUser(9001, 1)
    assert await bot.login_message_boundaries(client) == {9001:7}


@pytest.mark.asyncio
async def test_phone_service_is_resolved_from_dialog_not_used_as_peer_id():
    client = AsyncMock()
    secondary = User(id=9001, access_hash=2, phone='42777')
    async def peer(value):
        if value == '+42777':
            raise ValueError('not a contact')
        assert value is secondary
        return InputPeerUser(9001, 2)
    client.get_input_entity.side_effect = peer
    client.get_dialogs.return_value = [NS(entity=secondary)]
    client.get_messages.side_effect = [[NS(id=100)], [NS(id=3)]]
    assert await bot.login_message_boundaries(client) == {777000:100, 9001:3}


@pytest.mark.asyncio
async def test_source_floodwait_is_not_treated_as_unavailable():
    client = AsyncMock()
    client.get_messages.side_effect = FloodWaitError(None, capture=90)
    with pytest.raises(FloodWaitError):
        await bot.login_message_boundaries(client)
    client.get_input_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_connection_failure_closes_tasks_even_when_not_connected(tmp_path, caplog):
    source = tmp_path / 'original.session'
    source.write_bytes(b'offline fixture')
    database = bot.Database(str(tmp_path / 'queue.sqlite'))
    database.set_clean_mode(123, 1)
    client = AsyncMock()
    client.is_connected = MagicMock(return_value=False)
    pending = []
    async def connect():
        pending.append(asyncio.create_task(asyncio.Event().wait()))
        raise ValueError('Unknown proxy protocol type: private-password')
    async def disconnect():
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    client.connect.side_effect = connect
    client.disconnect.side_effect = disconnect
    message_bot = AsyncMock()
    item = {'id':'offline', 'chat_id':123, 'original_name':'fixture.session', 'file_path':str(source),
            'proxy_json':None, 'auto_2fa':None, '_operation_mode':1}
    with patch.object(bot, 'db', database), patch.object(bot, 'get_configured_client', return_value=client):
        await bot.process_one_session(item, message_bot, AsyncMock())
    client.disconnect.assert_awaited_once()
    assert all(task.done() for task in pending)
    assert 'stage=source_connect' in caplog.text and 'frames=' in caplog.text
    assert 'private-password' not in caplog.text
    assert source.read_bytes() == b'offline fixture'


@pytest.mark.asyncio
async def test_both_sources_unavailable_offer_manual_app_code(tmp_path):
    from telethon.tl.types.auth import SentCode, SentCodeTypeApp
    source = tmp_path / 'original.session'
    source.write_bytes(b'offline fixture')
    database = bot.Database(str(tmp_path / 'queue.sqlite'))
    database.set_clean_mode(123, 1)
    old, new = AsyncMock(), AsyncMock()
    old.get_me.return_value = NS(id=123, phone='10000000000')
    old.get_messages.side_effect = ValueError('Could not find the input entity: private')
    old.get_input_entity.side_effect = ValueError('not a contact')
    old.get_dialogs.return_value = []
    new.send_code_request.return_value = SentCode(SentCodeTypeApp(5), 'private-hash')
    item = {'id':'manual_fixture', 'chat_id':123, 'original_name':'fixture.session', 'file_path':str(source),
            'proxy_json':None, 'auto_2fa':None, '_operation_mode':1}
    with patch.object(bot, 'db', database), patch.object(bot, 'get_configured_client', side_effect=[old, new]):
        try:
            await bot.process_one_session(item, AsyncMock(), AsyncMock())
            mig = bot.current_migration[123]
            assert mig.waiting_manual_code
            new.send_code_request.assert_awaited_once()
            new.sign_in.assert_not_awaited()
            old.assert_not_awaited()
        finally:
            mig = bot.current_migration.get(123)
            if mig:
                await bot.cleanup_migration(mig)
    assert source.read_bytes() == b'offline fixture'


def test_invalid_proxy_is_rejected_before_allocating_client():
    with patch.object(bot, 'ManagedClient') as create:
        with pytest.raises(ProxyConfigError):
            bot.get_configured_client('unused', {}, {'proxy_type':'unknown', 'addr':'private', 'port':1080})
    create.assert_not_called()


@pytest.mark.asyncio
async def test_fast_clean_does_not_fall_back_to_direct_connection(tmp_path):
    source = tmp_path / 'original.session'
    source.write_bytes(b'offline fixture')
    work = tmp_path / 'work'
    work.mkdir()
    database = bot.Database(str(tmp_path / 'queue.sqlite'))
    item = {'id':'fast_fixture', 'chat_id':123, 'original_name':'fixture.session', 'file_path':str(source),
            'proxy_json':'{"proxy_type":"socks5","addr":"private-address","port":1080}', 'auto_2fa':None}
    with patch.object(bot, 'db', database), patch.object(bot.tempfile, 'mkdtemp', return_value=str(work)), \
         patch.object(bot, 'verify_proxy_alive', new=AsyncMock(return_value=False)), \
         patch.object(bot, 'get_configured_client') as create:
        await bot.process_fast_clean_session(item, AsyncMock())
    create.assert_not_called()
    assert not work.exists() and source.exists()
