"""Bounded initialization of the supplied authorization before migration."""
import asyncio

from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.updates import GetStateRequest


async def prepare_source_session(client, *, timeout=20):
    """Synchronize update state and mark this authorized connection online.

    This sends no messages and changes no account data. Telegram still controls
    login-code delivery; online status is not a guarantee that a code will arrive.
    Disable Telethon's automatic FloodWait sleeps so preparation stays bounded.
    """
    async with asyncio.timeout(timeout):
        await client(GetStateRequest(), flood_sleep_threshold=0)
        await client(UpdateStatusRequest(offline=False), flood_sleep_threshold=0)
