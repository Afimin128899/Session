import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from login_diagnostics import runtime_version, delivery_description
from telethon.tl.types.auth import SentCode, SentCodeTypeApp, SentCodeTypeSms


class DiagnosticsTests(unittest.TestCase):
    def test_delivery_does_not_expose_login_hash(self):
        result = SentCode(SentCodeTypeSms(5), 'private-login-hash')
        self.assertEqual(delivery_description(result), 'SMS')
        self.assertNotIn('private', delivery_description(result))

    def test_app_delivery_identifies_service_chat(self):
        self.assertIn('777000', delivery_description(
            SentCode(SentCodeTypeApp(5), 'private-login-hash')))

    def test_version_escapes_service_and_excludes_unrelated_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bot.py'
            source.write_text('offline', encoding='utf-8')
            with patch.dict('os.environ', {'RAILWAY_GIT_COMMIT_SHA': 'a' * 40,
                                          'RAILWAY_SERVICE_NAME': '<service>',
                                          'BOT_TOKEN': 'private-token'}, clear=True):
                result = runtime_version(source)
            self.assertIn('a' * 12, result)
            self.assertIn('&lt;service&gt;', result)
            self.assertNotIn('private-token', result)

    def test_invalid_commit_cannot_inject_markup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bot.py'
            source.write_text('offline')
            with patch.dict('os.environ', {'RAILWAY_GIT_COMMIT_SHA': '<bad>'}, clear=True):
                self.assertIn('неизвестен', runtime_version(source))


class AlternateDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sms_does_not_poll_777000_or_end_source_authorization(self):
        import bot
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'offline.session'
            source.write_bytes(b'offline fixture')
            database = bot.Database(str(Path(directory) / 'queue.sqlite'))
            database.set_clean_mode(-960, 1)
            database.add_queue_item('sms', -960, str(source), source.name, None, None)
            item = database.pop_next_item(-960)
            old, new = AsyncMock(), AsyncMock()
            old.is_connected = MagicMock(return_value=True)
            new.is_connected = MagicMock(return_value=True)
            old.get_me.return_value = MagicMock(phone='10000000000')
            new.send_code_request.return_value = SentCode(SentCodeTypeSms(5), 'offline-hash')
            client, state = AsyncMock(), AsyncMock()
            with patch.object(bot, 'db', database), \
                 patch.object(bot, 'get_configured_client', side_effect=[old, new]), \
                 patch.object(bot, 'poll_login_code', new=AsyncMock()) as poll:
                try:
                    await bot.process_one_session(item, client, state)
                    poll.assert_not_awaited()
                    new.sign_in.assert_not_awaited()
                    old.assert_not_awaited()
                    self.assertTrue(bot.current_migration[-960].waiting_manual_code)
                    self.assertTrue(source.exists())
                    text = client.send_message.return_value.edit_text.await_args.args[0]
                    self.assertIn('SMS', text)
                finally:
                    mig = bot.current_migration.get(-960)
                    if mig:
                        await bot.cleanup_migration(mig, state)
