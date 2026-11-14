"""Offline checks for terminating other authorizations without cleaning accounts."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('API_ID', '12345')
os.environ.setdefault('API_HASH', 'offline')
os.environ.setdefault('BOT_TOKEN', '123456:offline')
import bot
from session_devices import reset_other_sessions
from telethon.errors import FreshResetAuthorisationForbiddenError, FloodWaitError
from telethon.tl.functions.auth import ResetAuthorizationsRequest


class DeviceResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_reset_other_authorizations_is_requested(self):
        client = AsyncMock(return_value=True)
        result = await reset_other_sessions(client)
        self.assertEqual(result.category, 'reset')
        client.assert_awaited_once()
        self.assertIsInstance(client.call_args.args[0], ResetAuthorizationsRequest)
        self.assertEqual(client.call_args.kwargs['flood_sleep_threshold'], 0)

    async def test_fresh_session_error_does_not_report_success(self):
        client = AsyncMock(side_effect=FreshResetAuthorisationForbiddenError(request=None))
        result = await reset_other_sessions(client)
        self.assertEqual(result.category, 'failed')
        self.assertIn('свежей сессии', result.detail)

    async def test_flood_wait_is_reported_without_sleep_or_retry(self):
        client = AsyncMock(side_effect=FloodWaitError(request=None, capture=123))
        result = await reset_other_sessions(client)
        self.assertEqual(result.category, 'failed')
        self.assertIn('123', result.detail)
        client.assert_awaited_once()

    async def test_hung_reset_returns_unconfirmed_result(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        result = await asyncio.wait_for(reset_other_sessions(AsyncMock(side_effect=hang), timeout=.02), .3)
        self.assertEqual(result.category, 'failed')
        self.assertIn('не подтверждён', result.detail)

    async def test_cancellation_is_propagated(self):
        with self.assertRaises(asyncio.CancelledError):
            await reset_other_sessions(AsyncMock(side_effect=asyncio.CancelledError()))

    async def test_false_response_is_not_success(self):
        self.assertEqual((await reset_other_sessions(AsyncMock(return_value=False))).category, 'failed')


class DevicePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = bot.Database(str(self.root / 'test.db'))
        patcher = patch.object(bot, 'db', self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_session_and_metadata_preserved_without_cleaning_or_migration(self):
        source = self.root / 'device.session'
        source.write_bytes(b'fixture session')
        source.with_suffix('.json').write_text(json.dumps({'twoFA': 'fixture password', 'custom': 10}))
        self.database.add_queue_item('device', -990, str(source), source.name, None, None)
        item = self.database.pop_next_item(-990)
        client = AsyncMock(return_value=True)
        client.is_user_authorized.return_value = True
        message_bot = AsyncMock()
        with patch.object(bot, 'get_configured_client', return_value=client), \
             patch.object(bot, 'deep_clean_account') as clean, \
             patch.object(bot, 'terminate_old_session') as terminate, \
             patch.object(bot, 'check_spam_status') as spam:
            await bot.process_reset_devices_session(item, message_bot, AsyncMock())
        clean.assert_not_called()
        terminate.assert_not_called()
        spam.assert_not_called()
        client.assert_awaited_once()
        self.assertIsInstance(client.call_args.args[0], ResetAuthorizationsRequest)
        document = message_bot.send_document.call_args.kwargs['document']
        self.assertEqual(document.filename, 'devices_reset.zip')
        with zipfile.ZipFile(io.BytesIO(document.data)) as archive:
            self.assertEqual(set(archive.namelist()), {'device.session', 'device.json'})
            self.assertEqual(archive.read('device.session'), b'fixture session')
            metadata = json.loads(archive.read('device.json'))
            self.assertEqual(metadata['twoFA'], 'fixture password')
            self.assertEqual(metadata['custom'], 10)
            self.assertEqual(metadata['other_sessions_reset']['category'], 'reset')
            self.assertNotIn('spam_check', metadata)
        self.assertEqual(source.read_bytes(), b'fixture session')
        self.assertEqual(self.database.count_outstanding(-990), 0)

    async def test_button_selects_mode_and_awaits_existing_run_action(self):
        self.database.add_queue_item('pending', -991, 'unused', 'unused', None, None)
        call = AsyncMock()
        call.message.chat.id = -991
        with patch.object(bot, 'start_queue_worker') as start:
            await bot.cb_reset_devices(call)
        start.assert_not_called()
        self.assertEqual(self.database.get_clean_mode(-991), 4)
        self.assertTrue(bot.queue_paused.pop(-991))
        self.assertIn('Загруженная .session останется активной', call.message.answer.call_args.args[0])
        self.assertEqual(self.database.count_outstanding(-991), 1)

    async def test_mode_four_dispatches_to_device_reset(self):
        self.database.set_clean_mode(-992, 4)
        self.database.add_queue_item('dispatch', -992, 'unused', 'unused', None, None)
        async def process(item, *args):
            self.database.remove_item(item['id'])
        with patch.object(bot, 'process_reset_devices_session', side_effect=process) as reset, \
             patch.object(bot, 'process_fast_clean_session') as clean, \
             patch.object(bot, 'process_one_session') as migrate:
            await bot.queue_worker(-992, AsyncMock(), AsyncMock())
        reset.assert_awaited_once()
        clean.assert_not_called()
        migrate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
