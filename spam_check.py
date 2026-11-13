"""Read official SpamBot replies without testing messages on other users."""
from dataclasses import dataclass
import asyncio
import re

from telethon.tl.functions.contacts import UnblockRequest


LABELS = {
    'clean': 'Без спамблока',
    'permanent': 'Вечный спамблок',
    'temporary': 'Временный спамблок',
    'geo': 'Спамблок по гео',
    'frozen': 'Заморожен',
    'unknown': 'Не определено',
}


@dataclass(frozen=True)
class SpamResult:
    category: str
    detail: str


def classify_reply(text: str) -> SpamResult:
    normalized = ' '.join(text.casefold().replace('ё', 'е').split())
    if any(phrase in normalized for phrase in (
        'your account is frozen', 'your account has been frozen',
        'ваш аккаунт заморожен', 'ваша учетная запись заморожена',
    )):
        return SpamResult('frozen', text)
    if any(phrase in normalized for phrase in (
        'no limits are currently applied to your account',
        'ваш аккаунт свободен от каких-либо ограничений',
        'ваша учетная запись свободна от каких-либо ограничений',
        'на ваш аккаунт не наложено никаких ограничений',
    )):
        return SpamResult('clean', text)
    # "Geo" is the user's label for this specific phone-number restriction;
    # this reply does not identify a country or establish geographic causation.
    if any(phrase in normalized for phrase in (
        'антиспам-система излишне сурово реагирует на некоторые номера телефонов',
        'some phone numbers may trigger a harsh response from our anti-spam systems',
    )):
        return SpamResult('geo', text)
    if any(phrase in normalized for phrase in (
        'your account is permanently limited', 'your account has been permanently limited',
        'your account is now permanently limited', 'your account is limited forever',
        'на ваш аккаунт наложены вечные ограничения',
        'ваш аккаунт ограничен навсегда', 'ваш аккаунт заблокирован навсегда',
        'ваш аккаунт был навсегда ограничен',
        'на ваш аккаунт наложены постоянные ограничения',
    )):
        return SpamResult('permanent', text)
    if re.search(r'(?:limited until|ограничен(?:ия)?(?:[^.!?]{0,100})? до)\s+\d', normalized):
        return SpamResult('temporary', text)
    return SpamResult('unknown', text or 'Пустой ответ @SpamBot')


async def check_spam_status(client, *, timeout: float = 30.0) -> SpamResult:
    async def ask():
        async with client.conversation('@SpamBot', timeout=timeout, exclusive=True) as conversation:
            sent = await conversation.send_message('/start')
            response = await conversation.get_response(sent)
            return classify_reply(response.raw_text or '')

    for attempt in range(2):
        try:
            async with asyncio.timeout(timeout):
                result = await ask()
            if result.category != 'unknown' or attempt:
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Frozen accounts may be unable to write to SpamBot at all.
            if getattr(exc, 'message', '') == 'FROZEN_METHOD_INVALID':
                return SpamResult('frozen', 'Telegram: FROZEN_METHOD_INVALID')
            if attempt:
                return SpamResult('unknown', 'Ошибка проверки: ' + type(exc).__name__)
        if not attempt:
            try:
                async with asyncio.timeout(timeout):
                    await client(UnblockRequest(id='@SpamBot'))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return SpamResult('unknown', 'Не удалось разблокировать @SpamBot: ' + type(exc).__name__)
    return SpamResult('unknown', 'Нет ответа @SpamBot')
