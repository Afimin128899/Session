"""Source preparation uses only bounded Telegram state/status requests."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from source_activity import prepare_source_session
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.updates import GetStateRequest


class SourceActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_is_checked_before_online_status(self):
        client = AsyncMock()
        await prepare_source_session(client)
        self.assertEqual(client.await_count, 2)
        state, online = client.await_args_list
        self.assertIsInstance(state.args[0], GetStateRequest)
        self.assertIsInstance(online.args[0], UpdateStatusRequest)
        self.assertFalse(online.args[0].offline)
        for call in client.await_args_list:
            self.assertEqual(call.kwargs, {'flood_sleep_threshold': 0})

    async def test_state_failure_stops_before_setting_online(self):
        client = AsyncMock(side_effect=ConnectionError('offline'))
        with self.assertRaises(ConnectionError):
            await prepare_source_session(client)
        client.assert_awaited_once()

    async def test_flood_wait_is_propagated_without_retry(self):
        client = AsyncMock(side_effect=FloodWaitError(request=None, capture=60))
        with self.assertRaises(FloodWaitError):
            await prepare_source_session(client)
        client.assert_awaited_once()

    async def test_hung_preparation_is_cancelled(self):
        stopped = asyncio.Event()
        async def hang(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        client = AsyncMock(side_effect=hang)
        with self.assertRaises(TimeoutError):
            await prepare_source_session(client, timeout=.02)
        self.assertTrue(stopped.is_set())
        client.assert_awaited_once()

    async def test_external_cancellation_is_not_swallowed(self):
        client = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await prepare_source_session(client)
        client.assert_awaited_once()
