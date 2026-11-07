"""Offline checks: never connect Telegram accounts or open bundled sessions."""
import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
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


class ArchiveDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def account_zip(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('account.session', b'offline fixture')
            archive.writestr('account.json', b'{"id": 123}')
        return output.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = bot.Database(str(Path(self.tmp.name) / 'results.sqlite'))
        patcher = patch.object(bot, 'db', self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_queue_flushes_successes_even_when_last_account_fails(self):
        client = AsyncMock()
        chat_id = -904
        with tempfile.TemporaryDirectory() as root:
            database = bot.Database(str(Path(root) / 'queue.sqlite'))
            database.set_clean_mode(chat_id, 2)
            for index in range(3):
                database.add_queue_item(str(index), chat_id, 'unused', str(index), None, None)

            async def process(item, message_bot, state):
                if item['id'] == '2':
                    raise ValueError('last account failed')
                await bot.deliver_account_archive(
                    chat_id, message_bot, self.account_zip(), item['id'] + '.zip', 'Ready', queued=True,
                )
                database.remove_item(item['id'])

            with patch.object(bot, 'db', database), patch.object(bot, 'process_fast_clean_session', side_effect=process), patch.object(bot.asyncio, 'sleep', new=AsyncMock()):
                await bot.queue_worker(chat_id, client, AsyncMock())
            self.assertEqual(database.count_outstanding(chat_id), 0)
        client.send_document.assert_awaited_once()
        with zipfile.ZipFile(io.BytesIO(client.send_document.call_args.kwargs['document'].data)) as archive:
            self.assertEqual(len(archive.namelist()), 4)

    async def test_three_accounts_are_sent_as_flat_session_json_files(self):
        client = AsyncMock()
        chat_id = -901
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as archive:
            archive.writestr('account.session', b'offline fixture')
            archive.writestr('account.json', b'{"id": 123}')
        for _ in range(3):
            await bot.deliver_account_archive(
                chat_id, client, inner.getvalue(), 'same.zip', 'Ready', queued=True,
            )
        # Third result triggers delivery without waiting for the rest of the queue.
        await bot.flush_queue_archives(chat_id, client)
        client.send_document.assert_awaited_once()
        document = client.send_document.call_args.kwargs['document']
        self.assertEqual(document.filename, 'accounts.zip')
        with zipfile.ZipFile(io.BytesIO(document.data)) as bundle:
            self.assertEqual(bundle.namelist(), [
                'account.session', 'account.json', 'account_2.session', 'account_2.json',
                'account_3.session', 'account_3.json',
            ])
            for name in bundle.namelist():
                expected = b'offline fixture' if name.endswith('.session') else b'{"id": 123}'
                self.assertEqual(bundle.read(name), expected)
        self.assertEqual(self.database.archives(chat_id), [])
        await bot.flush_queue_archives(chat_id, client)
        client.send_document.assert_awaited_once()

    async def test_send_failure_keeps_results_for_retry(self):
        client = AsyncMock()
        chat_id = -902
        await bot.deliver_account_archive(chat_id, client, self.account_zip(), 'one.zip', 'Ready', queued=True)
        client.send_document.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            await bot.flush_queue_archives(chat_id, client)
        self.assertEqual(len(self.database.archives(chat_id)), 1)
        client.send_document.side_effect = None
        await bot.flush_queue_archives(chat_id, client)
        self.assertEqual(self.database.archives(chat_id), [])

    async def test_nine_accounts_send_three_batches_before_queue_finishes(self):
        client = AsyncMock()
        for index in range(9):
            await bot.deliver_account_archive(-908, client, self.account_zip(), f'{index}.zip', 'Ready', queued=True)
            self.assertEqual(client.send_document.await_count, (index + 1) // 3)
        self.assertEqual(self.database.archives(-908), [])
        self.assertEqual(len(self.database.archives(-908, include_sent=True)), 9)

    async def test_automatic_delivery_failure_preserves_all_three(self):
        client = AsyncMock()
        client.send_document.side_effect = OSError('offline')
        for index in range(3):
            await bot.deliver_account_archive(-909, client, self.account_zip(), f'{index}.zip', 'Ready', queued=True)
        self.assertEqual(len(self.database.archives(-909)), 3)
        self.assertIn('/archives', client.send_message.call_args.args[1])
        restored = bot.Database(self.database.db_path)
        client.send_document.side_effect = None
        with patch.object(bot, 'db', restored):
            await bot.flush_queue_archives(-909, client)
        self.assertEqual(restored.archives(-909), [])

    async def test_restart_and_recovery_after_delivery(self):
        client = AsyncMock()
        await bot.deliver_account_archive(-905, client, self.account_zip(), 'one.zip', 'Ready', queued=True)
        restored = bot.Database(self.database.db_path)
        with patch.object(bot, 'db', restored):
            await bot.flush_queue_archives(-905, client)
            self.assertEqual(restored.archives(-905), [])
            self.assertEqual(len(restored.archives(-905, include_sent=True)), 1)
            await bot.flush_queue_archives(-905, client, include_sent=True)
        self.assertEqual(client.send_document.await_count, 2)

    async def test_caption_failure_does_not_lose_archive(self):
        client = AsyncMock()
        client.send_message.side_effect = OSError('offline')
        await bot.deliver_account_archive(-906, client, self.account_zip(), 'one.zip', 'Ready', queued=True)
        self.assertEqual(len(self.database.archives(-906)), 1)
        await bot.flush_queue_archives(-906, client)
        client.send_document.assert_awaited_once()

    async def test_concurrent_flush_does_not_duplicate_delivery(self):
        client = AsyncMock()
        await bot.deliver_account_archive(-907, client, self.account_zip(), 'one.zip', 'Ready', queued=True)
        await asyncio.gather(bot.flush_queue_archives(-907, client), bot.flush_queue_archives(-907, client))
        client.send_document.assert_awaited_once()

    async def test_individual_account_is_sent_immediately(self):
        client = AsyncMock()
        await bot.deliver_account_archive(-903, client, self.account_zip(), 'one.zip', 'Ready', queued=False)
        client.send_document.assert_awaited_once()
        self.assertEqual(client.send_document.call_args.kwargs['document'].filename, 'one.zip')


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

    async def test_phone_step_waits_for_explicit_code_request(self):
        message = AsyncMock()
        message.chat.id = -301
        message.text = '+79001234567'
        state = AsyncMock()
        await bot.process_phone(message, state)
        state.update_data.assert_awaited_once_with(pending_phone='+79001234567')
        callbacks = {
            button.callback_data
            for row in message.answer.call_args.kwargs['reply_markup'].inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks, {'request_login_code', 'cancel_all'})
        self.assertNotIn(-301, bot.current_migration)

    async def test_code_request_requires_pending_phone(self):
        call = AsyncMock()
        call.message.chat.id = -302
        state = AsyncMock()
        state.get_data.return_value = {}
        await bot.cb_request_login_code(call, state)
        call.answer.assert_awaited_once()
        self.assertTrue(call.answer.call_args.kwargs['show_alert'])

    async def test_status_shows_current_item_safely(self):
        with patch.dict(bot.queue_current_items, {-303: 'account <one>.session'}):
            text = bot.queue_status_text(-303)
        self.assertIn('account &lt;one&gt;.session', text)

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

    async def test_uploaded_sessions_wait_for_explicit_start(self):
        message = AsyncMock()
        message.chat.id = -405
        message.document.file_name = 'own.session'
        message.document.file_size = 16
        message.bot.download.side_effect = lambda doc, destination: Path(destination).write_bytes(b'local test session')
        with tempfile.TemporaryDirectory() as root, patch.object(bot.tempfile, 'mkdtemp', return_value=str(Path(root) / 'upload')):
            # tempfile's contract creates the requested directory.
            Path(root, 'upload').mkdir()
            with patch.object(bot, 'start_queue_worker') as start_worker:
                await bot.process_document_upload(message, AsyncMock())
                start_worker.assert_not_called()
        self.assertTrue(bot.queue_paused[-405])
        self.assertEqual(bot.db.count_queue(-405), 1)
        markup = message.answer.call_args.kwargs['reply_markup']
        callbacks = {button.callback_data for row in markup.inline_keyboard for button in row}
        self.assertIn('resume_queue', callbacks)
        bot.db.clear_chat_queue(-405)
        bot.queue_paused.pop(-405, None)


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


class UploadQuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'quota.sqlite')
        self.db = bot.Database(self.path)

    def item(self, name):
        return (name, 'unused.session', 'unused.session', None, None)

    async def test_limit_persists_and_never_exceeds_server_cap(self):
        self.db.set_upload_limit(1, 5)
        self.assertEqual(bot.Database(self.path).get_upload_limit(1), 5)
        self.assertEqual(self.db.get_upload_limit(2), bot.settings.max_queue_per_user)
        for limit in (0, bot.settings.max_queue_per_user + 1):
            with self.assertRaises(ValueError):
                self.db.set_upload_limit(1, limit)
        with patch.object(bot.settings, 'max_queue_per_user', 2):
            self.assertEqual(self.db.get_upload_limit(1), 2)

    async def test_over_quota_batch_is_rejected_without_partial_insert(self):
        self.db.set_upload_limit(1, 2)
        self.db.add_queue_items(1, [self.item('first')])
        with self.assertRaises(ValueError):
            self.db.add_queue_items(1, [self.item('second'), self.item('third')])
        self.assertEqual(self.db.count_outstanding(1), 1)

    async def test_processing_counts_and_lowering_limit_keeps_existing_items(self):
        self.db.set_upload_limit(1, 2)
        self.db.add_queue_items(1, [self.item('first'), self.item('second')])
        self.db.pop_next_item(1)
        self.assertEqual(self.db.count_queue(1), 1)
        with self.assertRaises(ValueError):
            self.db.add_queue_items(1, [self.item('third')])
        self.db.set_upload_limit(1, 1)
        self.assertEqual(self.db.count_outstanding(1), 2)

    async def test_simultaneous_uploads_cannot_bypass_limit(self):
        self.db.set_upload_limit(1, 3)
        def upload(prefix):
            try:
                self.db.add_queue_items(1, [self.item(prefix + '1'), self.item(prefix + '2')])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(upload, ('a', 'b')))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(self.db.count_outstanding(1), 2)

    async def test_non_admin_cannot_change_limit(self):
        call = AsyncMock()
        call.message.chat.id = 1
        call.from_user.id = 999
        call.data = 'set_upload_limit:1'
        with patch.object(bot, 'db', self.db), patch.object(bot, 'is_admin', return_value=False):
            await bot.cb_set_upload_limit(call)
        self.assertEqual(self.db.get_upload_limit(1), bot.settings.max_queue_per_user)
        self.assertTrue(call.answer.call_args.kwargs['show_alert'])

    async def test_admin_can_set_and_view_limit(self):
        call = AsyncMock()
        call.message.chat.id = 1
        call.from_user.id = 123
        call.data = 'set_upload_limit:5'
        with patch.object(bot, 'db', self.db), patch.object(bot, 'is_admin', return_value=True):
            await bot.cb_set_upload_limit(call)
            self.assertIn('до 5.', bot.limits_text(1))
        self.assertEqual(self.db.get_upload_limit(1), 5)
        call.message.answer.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
