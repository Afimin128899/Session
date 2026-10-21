"""Offline checks: never connect Telegram accounts or open bundled sessions."""
import asyncio
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_runtime = tempfile.TemporaryDirectory()
os.environ['DB_FILE'] = str(Path(_runtime.name) / 'test.sqlite')
import bot


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, members):
        path = self.root / 'input.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, content in members:
                archive.writestr(name, content)
        return path

    def test_valid_nested_archive(self):
        archive = self.archive([('nested/account.session', b'test'), ('nested/account.json', '{}')])
        sessions = bot.extract_session_archive(archive, self.root / 'out', 1)
        self.assertEqual([p.name for p in sessions], ['account.session'])
        self.assertEqual(sessions[0].read_bytes(), b'test')

    def test_unsafe_names(self):
        for name in ('../escape.session', '/escape.session', 'dir\\escape.session', 'C:/escape.session'):
            with self.subTest(name=name):
                archive = self.archive([('ok.session', b'ok'), (name, b'bad')])
                with self.assertRaises(ValueError):
                    bot.extract_session_archive(archive, self.root / 'out', 5)
                self.assertFalse((self.root / 'out').exists())

    def test_symlink(self):
        link = zipfile.ZipInfo('link.session')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.archive([(link, '../outside')])
        with self.assertRaises(ValueError):
            bot.extract_session_archive(archive, self.root / 'out', 1)

    def test_duplicate_paths(self):
        archive = self.archive([('a.session', b'1'), ('./a.session', b'2')])
        with self.assertRaises(ValueError):
            bot.extract_session_archive(archive, self.root / 'out', 2)

    def test_no_sessions(self):
        archive = self.archive([('info.json', '{}')])
        with self.assertRaises(ValueError):
            bot.extract_session_archive(archive, self.root / 'out', 1)

    def test_queue_limit(self):
        archive = self.archive([('1.session', b'1'), ('2.session', b'2')])
        with self.assertRaises(ValueError):
            bot.extract_session_archive(archive, self.root / 'out', 1)
        self.assertFalse((self.root / 'out').exists())

    def test_expanded_size_limit(self):
        archive = self.archive([('1.session', b'1234')])
        with patch.object(bot, 'MAX_EXTRACTED_BYTES', 3), self.assertRaises(ValueError):
            bot.extract_session_archive(archive, self.root / 'out', 1)

    def test_entry_limit(self):
        archive = self.archive([('1.session', b'1'), ('2.session', b'2')])
        with patch.object(bot, 'MAX_ARCHIVE_ENTRIES', 1), self.assertRaises(ValueError):
            bot.extract_session_archive(archive, self.root / 'out', 2)

    def test_corrupt_archive(self):
        archive = self.root / 'bad.zip'
        archive.write_bytes(b'not zip')
        with self.assertRaises(zipfile.BadZipFile):
            bot.extract_session_archive(archive, self.root / 'out', 1)

    def test_upload_filename(self):
        for name in ('../a.session', 'a/b.zip', 'a\\b.session', '', 'a.exe'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bot.validate_upload_name(name)
        self.assertEqual(bot.validate_upload_name('A.SESSION'), 'A.SESSION')


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_preserves_queue_and_state(self):
        message, state = AsyncMock(), AsyncMock()
        message.chat.id = -100
        bot.db.add_queue_item('preserve', -100, 'unused', 'unused', None, None)
        self.addCleanup(bot.db.clear_chat_queue, -100)
        await bot.cmd_start(message, state)
        self.assertEqual(bot.db.count_queue(-100), 1)
        state.clear.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_menu_actions(self):
        callbacks = {b.callback_data for row in bot.main_menu_kb(-200).inline_keyboard for b in row}
        self.assertTrue({'btn_session_login', 'btn_new_session', 'queue_status', 'show_help', 'interface_settings', 'pause_queue', 'confirm_cancel'} <= callbacks)
        self.assertNotIn('cancel_all', callbacks)

    async def test_pause_resume_controls(self):
        message = AsyncMock()
        message.chat.id = -201
        call = AsyncMock()
        call.message = message
        call.bot = AsyncMock()
        await bot.cb_pause_queue(call)
        call.answer.assert_awaited_once()
        self.assertFalse(bot.queue_workers.get(-201, False))
        self.assertFalse(bot.queue_paused.get(-201, False))

        bot.db.add_queue_item('pause-me', -201, 'unused', 'unused', None, None)
        self.addCleanup(bot.db.clear_chat_queue, -201)
        call = AsyncMock()
        call.message.chat.id = -201
        call.bot = AsyncMock()
        await bot.cb_pause_queue(call)
        self.assertTrue(bot.queue_paused[-201])
        self.assertIn('resume_queue', {b.callback_data for row in bot.main_menu_kb(-201).inline_keyboard for b in row})

        state = AsyncMock()
        call = AsyncMock()
        call.message.chat.id = -201
        call.bot = AsyncMock()
        await bot.cb_resume_queue(call, state)
        self.assertFalse(bot.queue_paused[-201])
        task = bot.queue_worker_tasks.get(-201)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_worker_start_is_deduplicated(self):
        hold = asyncio.Event()
        async def fake_worker(chat_id, message_bot, state):
            await hold.wait()
        with patch.object(bot, 'queue_worker', side_effect=fake_worker):
            first = bot.start_queue_worker(-202, AsyncMock(), AsyncMock())
            second = bot.start_queue_worker(-202, AsyncMock(), AsyncMock())
            self.assertIs(first, second)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        bot.queue_worker_tasks.pop(-202, None)

    async def test_readonly_screens(self):
        for handler in (bot.cb_session_login, bot.cb_main_menu, bot.cb_help, bot.cb_queue_status, bot.cb_confirm_cancel):
            with self.subTest(handler=handler.__name__):
                call = AsyncMock()
                call.message.chat.id = -200
                await handler(call)
                call.answer.assert_awaited_once()
                call.message.answer.assert_awaited_once()
                self.assertEqual(bot.db.count_queue(-200), 0)

    async def test_status_waiting_for_password(self):
        mig = bot.ActiveMigration(id='test', chat_id=-300, waiting_2fa=True)
        with patch.dict(bot.current_migration, {-300: mig}):
            self.assertIn('2FA', bot.queue_status_text(-300))

    async def test_download_failure_removes_temporary_files(self):
        message = AsyncMock()
        message.chat.id = -400
        message.document.file_name = 'test.session'
        message.document.file_size = 10
        message.bot.download.side_effect = OSError('network')
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / 'upload'
            destination.mkdir()
            with patch.object(bot.tempfile, 'mkdtemp', return_value=str(destination)):
                await bot.process_document_upload(message, AsyncMock())
            self.assertFalse(destination.exists())
        self.assertEqual(bot.db.count_queue(-400), 0)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_failure_releases_resources(self):
        client = AsyncMock()
        dispatcher = AsyncMock()
        dispatcher.include_router = lambda router: None
        dispatcher.start_polling.side_effect = RuntimeError('polling failed')
        async def idle_watchdog(client):
            await asyncio.Event().wait()
        with patch.object(bot, 'Bot', return_value=client), patch.object(bot, 'Dispatcher', return_value=dispatcher), patch.object(bot, 'watchdog_task', side_effect=idle_watchdog):
            with self.assertRaisesRegex(RuntimeError, 'polling failed'):
                await bot.main()
        client.session.close.assert_awaited_once()
        dispatcher.storage.close.assert_awaited_once()
        client.set_my_commands.assert_awaited_once()


class InterfaceSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_preferences_persist_and_are_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'prefs.sqlite')
            first = bot.Database(path)
            first.set_clean_mode(10, 2)
            self.assertFalse(first.compact_menu(10))
            self.assertTrue(first.toggle_compact_menu(10))
            restored = bot.Database(path)
            self.assertTrue(restored.compact_menu(10))
            self.assertFalse(restored.compact_menu(11))
            restored.set_clean_mode(10, 0)
            self.assertTrue(restored.compact_menu(10))
            self.assertFalse(restored.toggle_compact_menu(10))
            self.assertEqual(restored.get_clean_mode(10), 0)

    async def test_compact_menu_keeps_account_warning(self):
        with tempfile.TemporaryDirectory() as root:
            database = bot.Database(str(Path(root) / 'prefs.sqlite'))
            with patch.object(bot, 'db', database):
                detailed = bot.menu_text(10)
                database.toggle_compact_menu(10)
                compact = bot.menu_text(10)
                self.assertLess(len(compact), len(detailed))
                self.assertIn('Очистка удаляет данные аккаунта', compact)
                self.assertIn('В очереди: 0', compact)

    async def test_information_commands_preserve_state(self):
        for handler in (bot.cmd_help, bot.cmd_limits, bot.cmd_privacy, bot.cmd_interface_settings, bot.cmd_id):
            with self.subTest(handler=handler.__name__):
                message = AsyncMock()
                message.chat.id = -500
                message.from_user.id = 12345
                with patch.object(bot.db, 'clear_chat_queue') as clear:
                    await handler(message)
                    clear.assert_not_called()
                message.answer.assert_awaited_once()

    async def test_limits_match_runtime_constants(self):
        with patch.object(bot, 'MAX_UPLOAD_BYTES', 7 * 1024 * 1024), patch.object(bot, 'MAX_ARCHIVE_ENTRIES', 42):
            text = bot.limits_text()
        self.assertIn('до 7 МБ', text)
        self.assertIn('до 42', text)


if __name__ == '__main__':
    unittest.main()
