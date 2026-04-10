"""Tests for Telegram slash-command passthrough to Mattermost/Hermes."""

import asyncio
from types import SimpleNamespace

from bridgemost.adapters.telegram import TelegramAdapter


def _make_update(text: str, user_id: int = 12345, message_id: int = 99):
    msg = SimpleNamespace(message_id=message_id, text=text, caption=None)
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(effective_message=msg, effective_user=user)


def test_unknown_command_is_forwarded_to_core():
    adapter = TelegramAdapter("123:ABC")
    captured = {}

    async def _on_message(inbound):
        captured["text"] = inbound.text
        captured["user_id"] = inbound.user_id
        captured["platform_msg_id"] = inbound.platform_msg_id

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_passthrough_command(_make_update("/new"), None))

    assert captured == {
        "text": "/new",
        "user_id": 12345,
        "platform_msg_id": 99,
    }


def test_command_bot_suffix_is_normalized_before_forwarding():
    adapter = TelegramAdapter("123:ABC")
    captured = {}

    async def _on_message(inbound):
        captured["text"] = inbound.text

    adapter._on_message = _on_message

    asyncio.run(
        adapter._on_tg_passthrough_command(
            _make_update("/model@BridgeMostBot llama-3"),
            None,
        )
    )

    assert captured["text"] == "/model llama-3"


def test_local_bridgemost_commands_are_not_forwarded():
    adapter = TelegramAdapter("123:ABC")
    captured = []

    async def _on_message(inbound):
        captured.append(inbound.text)

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_passthrough_command(_make_update("/bot assistant"), None))

    assert captured == []


def test_status_is_forwarded_to_core_for_hermes():
    adapter = TelegramAdapter("123:ABC")
    captured = []

    async def _on_message(inbound):
        captured.append(inbound.text)

    adapter._on_message = _on_message

    asyncio.run(adapter._on_tg_passthrough_command(_make_update("/status"), None))

    assert captured == ["/status"]
