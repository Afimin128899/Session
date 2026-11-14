"""Offline checks for SpamBot classification, retry, and sorted archive delivery."""
import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('API_ID', '12345')
os.environ.setdefault('API_HASH', 'offline')
os.environ.setdefault('BOT_TOKEN', '123456:offline')
from spam_check import LABELS, SpamResult, UnblockRequest, check_spam_status, classify_reply
import bot

GEO = ('К сожалению, иногда наша антиспам-система излишне сурово реагирует на некоторые номера телефонов. '
       'Если Вы считаете, что именно это и произошло, Вы можете отправить заявку модераторам или оформить '
       'подписку Telegram Premium, при наличии которой к аккаунту применяются менее строгие ограничения.')
CLEAN = 'Good news, no limits are currently applied to your account. You’re free as a bird!'


class ClassificationTests(unittest.TestCase):
    def test_five_categories_and_unknown(self):
        samples = {
            'clean': CLEAN,
            'permanent': 'Unfortunately, your account is now permanently limited.',
            'temporary': 'Your account is limited until 24 Nov 2026, 12:00 UTC.',
            'geo': GEO,
            'frozen': 'Ваш аккаунт заморожен.',
            'unknown': 'Your account is limited. Please contact our moderators.',
        }
        for expected, text in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(classify_reply(text).category, expected)

    def test_russian_clean_and_temporary(self):
        self.assertEqual(classify_reply('Ваш аккаунт свободен от каких-либо ограничений.').category, 'clean')
        self.assertEqual(classify_reply('На Ваш аккаунт наложены ограничения до 24 ноября.').category, 'temporary')

    def test_empty_or_unrecognized_text_is_not_clean_or_permanent(self):
        for text in ('', 'Try again later.', 'This account is limited.', 'Appeal submitted.'):
            self.assertEqual(classify_reply(text).category, 'unknown')


class SpamBotRequestTests(unittest.IsolatedAsyncioTestCase):
    def client(self, replies):
        client = AsyncMock()
        contexts = []
        self.conversations = []
        for reply in replies:
            conversation = AsyncMock()
            if isinstance(reply, BaseException):
                conversation.get_response.side_effect = reply
            else:
                conversation.get_response.return_value = MagicMock(raw_text=reply)
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=conversation)
            context.__aexit__ = AsyncMock(return_value=False)
            contexts.append(context)
            self.conversations.append(conversation)
        client.conversation = MagicMock(side_effect=contexts)
        return client

    async def test_success_uses_reply_to_sent_start_without_unblocking(self):
        client = self.client([CLEAN])
        result = await check_spam_status(client)
        self.assertEqual(result.category, 'clean')
        client.assert_not_awaited()
        conversation = self.conversations[0]
        conversation.send_message.assert_awaited_once_with('/start')
        conversation.get_response.assert_awaited_once_with(conversation.send_message.return_value)
        self.assertEqual(client.conversation.call_count, 1)

    async def test_blocked_bot_is_unblocked_then_rechecked(self):
        client = self.client([ConnectionError('blocked'), GEO])
        result = await check_spam_status(client)
        self.assertEqual(result.category, 'geo')
        client.assert_awaited_once()
        self.assertIsInstance(client.call_args.args[0], UnblockRequest)
        self.assertEqual(client.conversation.call_count, 2)

    async def test_unknown_reply_is_retried_once(self):
        client = self.client(['Unknown response', CLEAN])
        self.assertEqual((await check_spam_status(client)).category, 'clean')
        client.assert_awaited_once()

    async def test_hung_responses_have_a_bounded_total_duration(self):
        client = self.client(['unused', 'unused'])
        async def hang(*args):
            await asyncio.Event().wait()
        for conversation in self.conversations:
            conversation.get_response.side_effect = hang
        result = await asyncio.wait_for(check_spam_status(client, timeout=.02), .3)
        self.assertEqual(result.category, 'unknown')
        self.assertEqual(client.conversation.call_count, 2)

    async def test_second_failure_returns_unknown(self):
        client = self.client([TimeoutError(), TimeoutError()])
        self.assertEqual((await check_spam_status(client)).category, 'unknown')
        self.assertEqual(client.conversation.call_count, 2)

    async def test_frozen_rpc_is_classified_without_retry(self):
        error = RuntimeError()
        error.message = 'FROZEN_METHOD_INVALID'
        client = self.client([error])
        self.assertEqual((await check_spam_status(client)).category, 'frozen')
        client.assert_not_awaited()

    async def test_cancellation_does_not_unblock(self):
        client = self.client([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await check_spam_status(client)
        client.assert_not_awaited()

    async def test_unblock_failure_returns_unknown_without_another_request(self):
        client = self.client([TimeoutError()])
        client.side_effect = ConnectionError()
        self.assertEqual((await check_spam_status(client)).category, 'unknown')
        self.assertEqual(client.conversation.call_count, 1)


class SortedResultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = bot.Database(str(self.root / 'test.db'))
        patcher = patch.object(bot, 'db', self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_check_preserves_session_and_json_and_sends_category_zip(self):
        source = self.root / 'account.session'
        source.write_bytes(b'offline session fixture')
        source.with_suffix('.json').write_text(json.dumps({'twoFA': 'offline password', 'custom': 42}))
        self.database.add_queue_item('spam-one', -980, str(source), source.name, None, None)
        item = self.database.pop_next_item(-980)
        client = AsyncMock()
        client.is_user_authorized.return_value = True
        message_bot = AsyncMock()
        with patch.object(bot, 'get_configured_client', return_value=client), \
             patch.object(bot, 'check_spam_status', return_value=SpamResult('geo', GEO)), \
             patch.object(bot, 'deep_clean_account') as clean, \
             patch.object(bot, 'terminate_old_session') as terminate:
            await bot.process_spam_check_session(item, message_bot, AsyncMock())
        clean.assert_not_called()
        terminate.assert_not_called()
        document = message_bot.send_document.call_args.kwargs['document']
        self.assertEqual(document.filename, 'spam_geo.zip')
        with zipfile.ZipFile(io.BytesIO(document.data)) as archive:
            self.assertEqual(set(archive.namelist()), {'account.session', 'account.json'})
            self.assertEqual(archive.read('account.session'), b'offline session fixture')
            metadata = json.loads(archive.read('account.json'))
            self.assertEqual(metadata['twoFA'], 'offline password')
            self.assertEqual(metadata['custom'], 42)
            self.assertEqual(metadata['spam_check']['category'], 'geo')
            self.assertEqual(metadata['spam_check']['detail'], GEO)
        self.assertEqual(self.database.count_outstanding(-980), 0)
        self.assertEqual(source.read_bytes(), b'offline session fixture')
        self.assertEqual(len(self.database.archives(-980, True, 'spam_geo')), 1)
        self.assertEqual(self.database.archives(-980, True), [])

    async def test_unknown_report_displays_escaped_reason(self):
        source = self.root / 'unknown.session'
        source.write_bytes(b'offline fixture')
        self.database.add_queue_item('unknown', -985, str(source), source.name, None, None)
        item = self.database.pop_next_item(-985)
        client = AsyncMock()
        client.is_user_authorized.return_value = True
        message_bot = AsyncMock()
        with patch.object(bot, 'get_configured_client', return_value=client), \
             patch.object(bot, 'check_spam_status', return_value=SpamResult('unknown', 'Ответ: <unrecognized> & text')):
            await bot.process_spam_check_session(item, message_bot, AsyncMock())
        text = message_bot.send_message.call_args.args[1]
        self.assertIn('Не определено', text)
        self.assertIn('Причина: Ответ: &lt;unrecognized&gt; &amp; text', text)

    async def test_categories_are_never_mixed_on_recovery(self):
        for category in LABELS:
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('account.session', b'fixture')
                archive.writestr('account.json', '{}')
            self.database.save_archive(-981, 'account.zip', output.getvalue(), 'spam_' + category)
        message_bot = AsyncMock()
        await bot.flush_all_archives(-981, message_bot)
        self.assertEqual(message_bot.send_document.await_count, len(LABELS))
        self.assertEqual({c.kwargs['document'].filename for c in message_bot.send_document.call_args_list},
                         {'spam_' + key + '.zip' for key in LABELS})

    async def test_button_selects_mode_and_explains_retry(self):
        call = AsyncMock()
        call.message.chat.id = -983
        await bot.cb_check_spam(call, AsyncMock())
        self.assertEqual(self.database.get_clean_mode(-983), 3)
        self.assertIn('@SpamBot', call.message.answer.call_args.args[0])
        self.assertIn('разблокирует', call.message.answer.call_args.args[0])
        self.assertIn('Проверка спамблока', bot.menu_text(-983))

    async def test_button_starts_previously_uploaded_sessions(self):
        self.database.add_queue_item('queued', -984, 'unused', 'unused', None, None)
        call = AsyncMock()
        call.message.chat.id = -984
        bot.queue_paused[-984] = True
        with patch.object(bot, 'start_queue_worker') as start:
            await bot.cb_check_spam(call, AsyncMock())
        self.assertEqual(self.database.get_clean_mode(-984), 3)
        self.assertEqual(self.database.count_outstanding(-984), 1)
        self.assertFalse(bot.queue_paused.pop(-984))
        start.assert_called_once()
        self.assertIn('запущена', call.message.answer.call_args.args[0])

    async def test_button_recovers_interrupted_rows_without_deleting_files(self):
        source = self.root / 'interrupted.session'
        source.write_bytes(b'fixture')
        self.database.add_queue_item('interrupted', -986, str(source), source.name, None, None)
        self.database.pop_next_item(-986)
        self.assertEqual(self.database.count_queue(-986), 0)
        call = AsyncMock()
        call.message.chat.id = -986
        with patch.object(bot, 'start_queue_worker') as start:
            await bot.cb_check_spam(call, AsyncMock())
        self.assertEqual(self.database.count_queue(-986), 1)
        self.assertTrue(source.exists())
        start.assert_called_once()
        bot.queue_paused.pop(-986, None)

    async def test_button_keeps_live_processing_rows_untouched(self):
        self.database.add_queue_item('live', -987, 'unused', 'unused', None, None)
        self.database.pop_next_item(-987)
        self.database.add_queue_item('next', -987, 'unused', 'unused', None, None)
        worker = MagicMock()
        worker.done.return_value = False
        call = AsyncMock()
        call.message.chat.id = -987
        with patch.dict(bot.queue_worker_tasks, {-987: worker}), \
             patch.object(bot, 'start_queue_worker') as start:
            await bot.cb_check_spam(call, AsyncMock())
        self.assertEqual(self.database.count_queue(-987), 1)
        self.assertEqual(self.database.count_outstanding(-987), 2)
        self.assertIn('Текущая обработка завершится', call.message.answer.call_args.args[0])
        start.assert_called_once()
        bot.queue_paused.pop(-987, None)

    async def test_mode_three_dispatches_to_spam_check_only(self):
        self.database.set_clean_mode(-982, 3)
        self.database.add_queue_item('test', -982, 'unused', 'unused', None, None)
        async def process(item, *args):
            self.database.remove_item(item['id'])
        with patch.object(bot, 'process_spam_check_session', side_effect=process) as check, \
             patch.object(bot, 'process_one_session') as migrate, \
             patch.object(bot, 'process_fast_clean_session') as clean:
            await bot.queue_worker(-982, AsyncMock(), AsyncMock())
        check.assert_awaited_once()
        migrate.assert_not_called()
        clean.assert_not_called()


class ArchiveSchemaMigrationTests(unittest.TestCase):
    def test_old_results_keep_default_category(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'old.db')
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE result_archives (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                             'chat_id INTEGER NOT NULL, filename TEXT NOT NULL, data BLOB NOT NULL, '
                             'delivered INTEGER NOT NULL DEFAULT 0)')
                conn.execute('INSERT INTO result_archives(chat_id, filename, data) VALUES (1, ?, ?)',
                             ('existing.zip', b'fixture'))
            database = bot.Database(path)
            self.assertEqual(len(database.archives(1)), 1)
            self.assertEqual(database.archives(1, category='spam_clean'), [])
            bot.Database(path)  # Migration remains idempotent.


if __name__ == '__main__':
    unittest.main()
