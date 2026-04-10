"""Security tests for Telegram owner-only DM mode."""

import asyncio
from types import SimpleNamespace

from bridgemost.adapters.telegram import TelegramAdapter


def _make_update(
    text: str = "hola",
    *,
    user_id: int = 12345,
    message_id: int = 99,
    chat_type: str = "private",
    sender_chat=None,
    forward_origin=None,
    forward_from=None,
    forward_from_chat=None,
    forward_date=None,
):
    msg = SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        sender_chat=sender_chat,
        forward_origin=forward_origin,
        forward_from=forward_from,
        forward_from_chat=forward_from_chat,
        forward_date=forward_date,
    )
    user = SimpleNamespace(id=user_id)
    chat = SimpleNamespace(type=chat_type)
    return SimpleNamespace(effective_message=msg, effective_user=user, effective_chat=chat)


def test_group_messages_are_rejected_even_from_allowed_user():
    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[12345])
    captured = []

    async def _on_message(inbound):
        captured.append(inbound.text)

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_message(_make_update(chat_type="group"), None))

    assert captured == []


def test_forwarded_messages_are_rejected():
    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[12345])
    captured = []

    async def _on_message(inbound):
        captured.append(inbound.text)

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_message(_make_update(forward_origin=object()), None))

    assert captured == []


def test_sender_chat_messages_are_rejected():
    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[12345])
    captured = []

    async def _on_message(inbound):
        captured.append(inbound.text)

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_message(_make_update(sender_chat=object()), None))

    assert captured == []


def test_private_allowed_message_still_passes():
    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[12345])
    captured = []

    async def _on_message(inbound):
        captured.append((inbound.user_id, inbound.text))

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_message(_make_update(text="ok"), None))

    assert captured == [(12345, "ok")]
