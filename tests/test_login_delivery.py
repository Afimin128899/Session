"""Offline regressions for bounded, server-advertised login retries."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from telethon.tl.types.auth import SentCode, SentCodeTypeApp, SentCodeTypeSms, CodeTypeSms


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_delivery(self, initial, responses, retry=None, *, cancelled=False):
        self.client = AsyncMock(return_value=retry)
        self.mig = bot.ActiveMigration(id='offline', chat_id=1, phone='+10000000000',
                                       phone_code_hash='old-private-hash', skip_requested=cancelled)
        with patch.object(bot, 'poll_login_code', new=AsyncMock(side_effect=responses)) as poll:
            self.poll = poll
            return await bot.wait_automatic_code(MagicMock(), self.client, self.mig,
                                                  initial, 0, 10, AsyncMock())

    def initial(self, **kwargs):
        return SentCode(SentCodeTypeApp(5), 'old-private-hash', **kwargs)

    async def test_missing_next_type_does_not_resend(self):
        with self.assertRaises(TimeoutError):
            await self.run_delivery(self.initial(), [TimeoutError()])
        self.client.assert_not_awaited()

    async def test_success_does_not_resend(self):
        result = self.initial()
        self.assertEqual(await self.run_delivery(result, ['12345']), ('12345', result))
        self.client.assert_not_awaited()

    async def test_one_resend_updates_hash_and_switches_delivery(self):
        retry = SentCode(SentCodeTypeSms(5), 'new-private-hash')
        code, response = await self.run_delivery(
            self.initial(next_type=CodeTypeSms(), timeout=0), [TimeoutError()], retry)
        self.assertIsNone(code)
        self.assertIs(response, retry)
        self.assertEqual(self.mig.phone_code_hash, 'new-private-hash')
        self.client.assert_awaited_once()
        self.assertEqual(self.client.await_args.kwargs['flood_sleep_threshold'], 0)

    async def test_second_timeout_never_resends_again(self):
        with self.assertRaises(TimeoutError):
            await self.run_delivery(self.initial(next_type=CodeTypeSms(), timeout=0),
                                    [TimeoutError(), TimeoutError()], self.initial())
        self.client.assert_awaited_once()
        self.assertEqual(self.poll.await_count, 2)

    async def test_early_read_failure_does_not_resend_before_server_timeout(self):
        with self.assertRaises(TimeoutError):
            await self.run_delivery(self.initial(next_type=CodeTypeSms(), timeout=60),
                                    [TimeoutError()])
        self.client.assert_not_awaited()

    async def test_delivery_logs_do_not_include_hash_or_phone(self):
        mig = bot.ActiveMigration(id='safe-task', chat_id=1, phone='+10000000000')
        with self.assertLogs(bot.logger, level='INFO') as logs:
            bot.log_code_delivery(mig, self.initial(), 'request')
        message = ' '.join(logs.output)
        self.assertNotIn('private-hash', message)
        self.assertNotIn(mig.phone, message)
        self.assertIn('SentCodeTypeApp', message)
