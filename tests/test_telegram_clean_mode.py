"""Tests for Telegram clean presentation mode in BridgeMost."""

import asyncio
from types import SimpleNamespace

from bridgemost.adapters.base import BaseAdapter, InboundMessage, OutboundMessage
from bridgemost.config import BotRoute, Config, DmBridge, UserMapping
from bridgemost.core import BridgeMostCore, DmBridgeRelay


class RecordingAdapter(BaseAdapter):
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
        self.streamed = []
        self.typing_started = []
        self.typing_stopped = []
        self._next_id = 100

    async def start(self):
        return None

    async def stop(self):
        return None

    async def send_message(self, user_id, msg: OutboundMessage):
        self.sent.append({"user_id": user_id, "text": msg.text})
        mid = self._next_id
        self._next_id += 1
        return mid

    async def send_typing(self, user_id):
        return None

    async def edit_message(self, user_id, platform_msg_id, new_text: str):
        self.edited.append({"user_id": user_id, "message_id": platform_msg_id, "text": new_text})
        return True

    async def delete_message(self, user_id, platform_msg_id):
        self.deleted.append({"user_id": user_id, "message_id": platform_msg_id})
        return True

    async def set_reaction(self, user_id, platform_msg_id, emoji: str):
        return True

    async def clear_reactions(self, user_id, platform_msg_id):
        return True

    async def stream_edit_message(self, user_id, platform_msg_id, new_text: str, chunk_size=0, interval=0.0):
        self.streamed.append(
            {
                "user_id": user_id,
                "message_id": platform_msg_id,
                "text": new_text,
                "chunk_size": chunk_size,
                "interval": interval,
            }
        )
        return True

    def start_typing_loop(self, user_id: int, timeout: float = 60.0):
        self.typing_started.append({"user_id": user_id, "timeout": timeout})

    def stop_typing_loop(self, user_id: int):
        self.typing_stopped.append(user_id)


def _presentation_settings():
    return SimpleNamespace(
        enabled=True,
        suppress_internal_progress=True,
        show_placeholder=True,
        placeholder_text="🧠⚡ Conectando a la red neuronal...",
        placeholder_delay_seconds=0.0,
        stream_final_response=True,
        stream_chunk_chars=64,
        stream_edit_interval=0.0,
    )


def _make_config():
    cfg = Config(
        adapter="telegram",
        mm_url="http://localhost:8065",
        users=[
            UserMapping(
                telegram_id=12345,
                telegram_name="juanjo",
                mm_user_id="user1234567890abcdef123456",
                mm_token="pat-abc123",
                bots=[
                    BotRoute(
                        name="oceana",
                        mm_bot_id="bot12345678901234567890ab",
                        mm_dm_channel="dm-main-chan",
                        is_default=True,
                    )
                ],
                active_bot="oceana",
            )
        ],
        dm_bridges=[
            DmBridge(
                tg_bot_token="111:ABC",
                mm_bot_id="bot12345678901234567890ab",
                name="oceana",
            )
        ],
    )
    cfg.telegram_presentation = _presentation_settings()
    return cfg


def _make_main_core():
    adapter = RecordingAdapter()
    core = BridgeMostCore(_make_config(), adapter)
    user = core.config.users[0]
    bot = user.bots[0]
    core._dm_to_user[bot.mm_dm_channel] = (user, bot)
    return core, adapter, user, bot


def _make_dm_relay():
    relay = DmBridgeRelay.__new__(DmBridgeRelay)
    relay.config = _make_config()
    relay.bridge = relay.config.dm_bridges[0]
    relay.adapter = RecordingAdapter()
    relay._store = SimpleNamespace(put=lambda *args, **kwargs: None, get_mm=lambda *args, **kwargs: None, get_tg=lambda *args, **kwargs: None, has_tg=lambda *args, **kwargs: False)
    relay._tg_to_mm = {}
    relay._mm_to_tg = {}
    relay._map_maxlen = 5000
    relay._our_post_ids = []
    relay._our_post_maxlen = 1000
    relay._dm_to_user = {"dm-relay-chan": relay.config.users[0]}
    relay._stats = {"tg_to_mm": 0, "mm_to_tg": 0, "errors": 0}
    relay._state = "active"
    relay._last_error = ""
    relay._edit_debounce = {}
    relay._edit_pending = {}
    relay._edit_debounce_secs = 0.0
    relay._presentation = {}
    return relay, relay.adapter, relay.config.users[0]


def test_internal_progress_post_is_suppressed_for_telegram():
    core, adapter, user, bot = _make_main_core()

    async def scenario():
        await core._handle_ws_post(
            {
                "channel_id": bot.mm_dm_channel,
                "id": "post-tool-1",
                "user_id": bot.mm_bot_id,
                "message": '💻 terminal: "pwd"',
            }
        )

    asyncio.run(scenario())

    assert adapter.sent == []
    assert adapter.typing_stopped == []


def test_placeholder_message_is_sent_while_waiting_for_final_response():
    core, adapter, user, bot = _make_main_core()

    async def fake_retry(_user, _channel, _text, _files, max_retries=3):
        return {"id": "mm-user-post-1"}

    core._retry_mm_post = fake_retry

    async def scenario():
        await core._handle_inbound_message(
            InboundMessage(platform_msg_id=77, user_id=user.telegram_id, text="hola")
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert adapter.sent == [{"user_id": user.telegram_id, "text": "🧠⚡ Conectando a la red neuronal..."}]
    assert adapter.typing_started[0]["user_id"] == user.telegram_id


def test_final_post_reuses_placeholder_and_streams_into_same_message():
    core, adapter, user, bot = _make_main_core()

    async def fake_retry(_user, _channel, _text, _files, max_retries=3):
        return {"id": "mm-user-post-1"}

    core._retry_mm_post = fake_retry

    async def scenario():
        await core._handle_inbound_message(
            InboundMessage(platform_msg_id=77, user_id=user.telegram_id, text="hola")
        )
        await asyncio.sleep(0)
        await core._handle_ws_post(
            {
                "channel_id": bot.mm_dm_channel,
                "id": "mm-final-1",
                "user_id": bot.mm_bot_id,
                "message": "Respuesta final limpia",
            }
        )

    asyncio.run(scenario())

    assert adapter.streamed == [
        {
            "user_id": user.telegram_id,
            "message_id": 100,
            "text": "Respuesta final limpia",
            "chunk_size": 64,
            "interval": 0.0,
        }
    ]
    assert core._lookup_platform("mm-final-1") == 100
    assert adapter.typing_stopped == [user.telegram_id]


def test_final_response_can_arrive_via_edit_after_internal_progress_post():
    core, adapter, user, bot = _make_main_core()

    async def fake_retry(_user, _channel, _text, _files, max_retries=3):
        return {"id": "mm-user-post-1"}

    core._retry_mm_post = fake_retry

    async def scenario():
        await core._handle_inbound_message(
            InboundMessage(platform_msg_id=77, user_id=user.telegram_id, text="hola")
        )
        await asyncio.sleep(0)
        await core._handle_ws_post(
            {
                "channel_id": bot.mm_dm_channel,
                "id": "mm-progress-1",
                "user_id": bot.mm_bot_id,
                "message": '📚 skill_view: "mattermost-ops"',
            }
        )
        await core._handle_ws_edit(
            {
                "channel_id": bot.mm_dm_channel,
                "id": "mm-progress-1",
                "user_id": bot.mm_bot_id,
                "message": "Respuesta final por edición",
            }
        )

    asyncio.run(scenario())

    assert adapter.streamed[-1]["text"] == "Respuesta final por edición"
    assert core._lookup_platform("mm-progress-1") == 100


def test_dm_bridge_also_reuses_placeholder_for_clean_final_response():
    relay, adapter, user = _make_dm_relay()

    async def fake_retry(_user, _channel, _text, _files, max_retries=3):
        return {"id": "mm-user-post-1"}

    relay._retry_mm_post = fake_retry

    async def scenario():
        await relay._handle_inbound_message(
            InboundMessage(platform_msg_id=88, user_id=user.telegram_id, text="hola")
        )
        await asyncio.sleep(0)
        await relay._handle_ws_post(
            {
                "channel_id": "dm-relay-chan",
                "id": "relay-final-1",
                "user_id": relay.bridge.mm_bot_id,
                "message": "Respuesta relay limpia",
            }
        )

    asyncio.run(scenario())

    assert adapter.sent[0]["text"] == "🧠⚡ Conectando a la red neuronal..."
    assert adapter.streamed[0]["message_id"] == 100
    assert relay._lookup_platform("relay-final-1") == 100