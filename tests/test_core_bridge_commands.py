"""Tests for BridgeMost core local command handling."""

import asyncio
from types import SimpleNamespace

from bridgemost.core import BridgeMostCore, DmBridgeRelay
from bridgemost.config import BotRoute, Config, DmBridge, UserMapping
from bridgemost.adapters.base import BaseAdapter


class _DummyAdapter(BaseAdapter):
    async def start(self):
        return None

    async def stop(self):
        return None

    async def send_message(self, user_id, msg):
        return None

    async def send_typing(self, user_id):
        return None

    async def edit_message(self, user_id, platform_msg_id, new_text):
        return True

    async def delete_message(self, user_id, platform_msg_id):
        return True

    async def set_reaction(self, user_id, platform_msg_id, emoji):
        return True

    async def clear_reactions(self, user_id, platform_msg_id):
        return True


def _make_config():
    return Config(
        mm_url="http://localhost:8065",
        users=[
            UserMapping(
                telegram_id=12345,
                telegram_name="owner",
                mm_user_id="user1234567890abcdef123456",
                mm_token="pat-abc123",
                bots=[
                    BotRoute(
                        name="assistant",
                        mm_bot_id="bot12345678901234567890ab",
                        is_default=True,
                    )
                ],
                active_bot="assistant",
            )
        ],
        dm_bridges=[
            DmBridge(
                tg_bot_token="111:ABC",
                mm_bot_id="bot12345678901234567890ab",
                name="assistant",
            )
        ],
    )


def test_main_core_bridge_command_returns_bridge_help():
    core = BridgeMostCore(_make_config(), _DummyAdapter())

    reply = asyncio.run(core._handle_command("bridge", [], 12345))

    assert "/bridge bot" in reply
    assert "reenvían directamente a Hermes" in reply


def test_dm_bridge_relay_bridge_command_mentions_passthrough():
    relay = DmBridgeRelay(_make_config(), _make_config().dm_bridges[0])

    reply = asyncio.run(relay._handle_command("bridge", [], 12345))

    assert "DM bridge *assistant*" in reply
    assert "slash commands se envían al bot Hermes conectado" in reply
