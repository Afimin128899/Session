"""Code-reader regressions: no live account or message contents in logs."""
import unittest
from unittest.mock import AsyncMock, MagicMock

import bot


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def migration(self):
        return bot.ActiveMigration(id='reader-test', chat_id=1)

    async def test_server_id_boundary_ignores_wrong_host_clock_and_outgoing_codes(self):
        async def messages(*args, **kwargs):
            self.assertEqual(kwargs['min_id'], 100)
            yield MagicMock(id=100, out=False, raw_text='Code: 11111')
            yield MagicMock(id=103, out=True, raw_text='Code: 33333')
            yield MagicMock(id=102, out=False, date=None, raw_text='Code: 22222')
        code = await bot.poll_login_code(MagicMock(iter_messages=messages), self.migration(),
                                         9999999999, after_id=100, timeout=1)
        self.assertEqual(code, '22222')

    async def test_code_below_first_three_messages_is_found(self):
        async def messages(*args, **kwargs):
            self.assertEqual(kwargs['limit'], 20)
            for index in range(4):
                yield MagicMock(id=110-index, out=False, raw_text='Account notice')
            yield MagicMock(id=105, out=False, raw_text='Code: 12345')
        self.assertEqual(await bot.poll_login_code(
            MagicMock(iter_messages=messages), self.migration(), 0,
            after_id=100, timeout=1), '12345')

    async def test_empty_chat_has_distinct_reason_and_safe_logs(self):
        async def messages(*args, **kwargs):
            if False:
                yield
        with self.assertLogs(bot.logger, level='WARNING') as logs:
            with self.assertRaisesRegex(bot.LoginCodeTimeout, 'новых входящих сообщений'):
                await bot.poll_login_code(MagicMock(iter_messages=messages), self.migration(),
                                          0, after_id=100, timeout=.02, interval=.001)
        self.assertIn('outcome=no_new_messages', ' '.join(logs.output))

    async def test_unrecognized_message_does_not_log_its_contents(self):
        async def messages(*args, **kwargs):
            yield MagicMock(id=101, out=False, raw_text='private-message-no-code')
        with self.assertLogs(bot.logger, level='WARNING') as logs:
            with self.assertRaisesRegex(bot.LoginCodeTimeout, 'не распознан'):
                await bot.poll_login_code(MagicMock(iter_messages=messages), self.migration(),
                                          0, after_id=100, timeout=.02, interval=.001)
        self.assertNotIn('private-message', ' '.join(logs.output))
        self.assertIn('outcome=unrecognized', ' '.join(logs.output))

    async def test_network_failure_has_distinct_reason(self):
        client = MagicMock()
        client.iter_messages.side_effect = ConnectionError('private network detail')
        with self.assertLogs(bot.logger, level='WARNING') as logs:
            with self.assertRaisesRegex(bot.LoginCodeTimeout, 'трёх попыток'):
                await bot.poll_login_code(client, self.migration(), 0,
                                          after_id=100, timeout=1, interval=0)
        self.assertIn('outcome=read_failed', ' '.join(logs.output))
        self.assertNotIn('private network detail', ' '.join(logs.output))

    async def test_boundary_for_empty_and_populated_history(self):
        client = AsyncMock()
        client.get_messages.return_value = []
        self.assertEqual(await bot.login_message_boundary(client), 0)
        client.get_messages.return_value = [MagicMock(id=321)]
        self.assertEqual(await bot.login_message_boundary(client), 321)
        client.get_messages.assert_awaited_with(777000, limit=1)
