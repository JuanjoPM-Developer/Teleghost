"""Tests for reply/thread bridging between Telegram and Mattermost."""

import asyncio
from types import SimpleNamespace

from bridgemost.adapters.base import BaseAdapter, InboundMessage, OutboundMessage
from bridgemost.adapters.telegram import TelegramAdapter
from bridgemost.config import BotRoute, Config, DmBridge, UserMapping
from bridgemost.core import BridgeMostCore, DmBridgeRelay


class FakeTelegramBot:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_to_message_id=None):
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_to_message_id": reply_to_message_id,
            }
        )
        return SimpleNamespace(message_id=501)


class RecordingAdapter(BaseAdapter):
    def __init__(self):
        self.sent = []
        self.typing_started = []
        self.typing_stopped = []
        self._next_id = 100

    async def start(self):
        return None

    async def stop(self):
        return None

    async def send_message(self, user_id, msg: OutboundMessage):
        self.sent.append(
            {
                "user_id": user_id,
                "text": msg.text,
                "reply_to": msg.reply_to_platform_msg_id,
                "file_name": msg.file_name,
            }
        )
        mid = self._next_id
        self._next_id += 1
        return mid

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

    async def stream_edit_message(self, user_id, platform_msg_id, new_text, chunk_size=0, interval=0.0):
        return True

    def start_typing_loop(self, user_id: int, timeout: float = 60.0):
        self.typing_started.append({"user_id": user_id, "timeout": timeout})

    def stop_typing_loop(self, user_id: int):
        self.typing_stopped.append(user_id)


class DummyMattermost:
    def __init__(self, root_lookup=None):
        self.root_lookup = root_lookup or {}
        self.post_calls = []

    async def post_message(self, token, channel_id, message, file_ids=None, root_id=None):
        self.post_calls.append(
            {
                "token": token,
                "channel_id": channel_id,
                "message": message,
                "file_ids": file_ids,
                "root_id": root_id,
            }
        )
        return {"id": f"mm-post-{len(self.post_calls)}"}

    async def get_thread_root_id(self, token, post_id):
        return self.root_lookup.get(post_id, post_id)


def _make_config(clean_mode: bool = False):
    cfg = Config(
        adapter="telegram",
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
                        mm_dm_channel="dm-main-chan",
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
                name="assistant-dm",
            )
        ],
    )
    cfg.telegram_presentation = SimpleNamespace(
        enabled=clean_mode,
        suppress_internal_progress=True,
        show_placeholder=True,
        placeholder_text="🧠⚡ Conectando a la red neuronal...",
        placeholder_delay_seconds=0.0,
        stream_final_response=True,
        stream_chunk_chars=64,
        stream_edit_interval=0.0,
    )
    return cfg


def _build_main_core(clean_mode: bool = False):
    adapter = RecordingAdapter()
    core = BridgeMostCore(_make_config(clean_mode=clean_mode), adapter)
    user = core.config.users[0]
    bot = user.bots[0]
    core._dm_to_user[bot.mm_dm_channel] = (user, bot)
    return core, adapter, user, bot


def _build_dm_relay(clean_mode: bool = False):
    relay = DmBridgeRelay.__new__(DmBridgeRelay)
    relay.config = _make_config(clean_mode=clean_mode)
    relay.bridge = relay.config.dm_bridges[0]
    relay.adapter = RecordingAdapter()
    relay.mm = DummyMattermost()
    relay.whisper = None
    relay._store = SimpleNamespace(
        put=lambda *args, **kwargs: None,
        get_mm=lambda *args, **kwargs: None,
        get_tg=lambda *args, **kwargs: None,
        has_tg=lambda *args, **kwargs: False,
    )
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


def test_telegram_send_message_respects_reply_target():
    adapter = TelegramAdapter("123:ABC")
    adapter._bot = FakeTelegramBot()

    asyncio.run(
        adapter.send_message(
            12345,
            OutboundMessage(text="hola", reply_to_platform_msg_id=77),
        )
    )

    assert adapter._bot.sent_messages[0]["reply_to_message_id"] == 77


def test_telegram_inbound_message_captures_reply_target():
    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[12345])
    captured = []

    async def _on_message(inbound):
        captured.append(inbound.reply_to_msg_id)

    adapter._on_message = _on_message

    reply_to = SimpleNamespace(message_id=41)
    msg = SimpleNamespace(
        message_id=99,
        text="hola",
        caption=None,
        reply_to_message=reply_to,
        sender_chat=None,
        forward_origin=None,
        forward_from=None,
        forward_from_chat=None,
        forward_date=None,
        photo=None,
        document=None,
        audio=None,
        voice=None,
        video=None,
        video_note=None,
        sticker=None,
        venue=None,
        location=None,
        poll=None,
    )
    update = SimpleNamespace(
        effective_message=msg,
        effective_user=SimpleNamespace(id=12345),
        effective_chat=SimpleNamespace(type="private"),
    )

    asyncio.run(adapter._on_tg_message(update, SimpleNamespace(bot=None)))

    assert captured == [41]


def test_main_core_inbound_reply_posts_into_mm_thread_root():
    core, adapter, user, bot = _build_main_core(clean_mode=False)
    core.mm = DummyMattermost(root_lookup={"mm-reply-1": "mm-root-1"})
    core._tg_to_mm[77] = "mm-reply-1"

    async def scenario():
        await core._handle_inbound_message(
            InboundMessage(
                platform_msg_id=88,
                user_id=user.telegram_id,
                text="respuesta",
                reply_to_msg_id=77,
            )
        )

    asyncio.run(scenario())

    assert core.mm.post_calls[0]["root_id"] == "mm-root-1"


def test_main_core_mm_thread_reply_becomes_tg_reply():
    core, adapter, user, bot = _build_main_core(clean_mode=False)
    core._mm_to_tg["mm-root-1"] = 55

    async def scenario():
        await core._handle_ws_post(
            {
                "channel_id": bot.mm_dm_channel,
                "id": "mm-reply-2",
                "root_id": "mm-root-1",
                "user_id": bot.mm_bot_id,
                "message": "respuesta en hilo",
            }
        )

    asyncio.run(scenario())

    assert adapter.sent[0]["reply_to"] == 55


def test_clean_mode_placeholder_keeps_reply_target():
    core, adapter, user, bot = _build_main_core(clean_mode=True)
    core.mm = DummyMattermost(root_lookup={"mm-reply-1": "mm-root-1"})
    core._tg_to_mm[77] = "mm-reply-1"

    async def scenario():
        await core._handle_inbound_message(
            InboundMessage(
                platform_msg_id=90,
                user_id=user.telegram_id,
                text="hola",
                reply_to_msg_id=77,
            )
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert adapter.sent[0]["text"] == "🧠⚡ Conectando a la red neuronal..."
    assert adapter.sent[0]["reply_to"] == 77


def test_dm_bridge_inbound_reply_posts_into_mm_thread_root():
    relay, adapter, user = _build_dm_relay(clean_mode=False)
    relay.mm = DummyMattermost(root_lookup={"mm-reply-1": "mm-root-1"})
    relay._tg_to_mm[77] = "mm-reply-1"

    async def scenario():
        await relay._handle_inbound_message(
            InboundMessage(
                platform_msg_id=91,
                user_id=user.telegram_id,
                text="respuesta relay",
                reply_to_msg_id=77,
            )
        )

    asyncio.run(scenario())

    assert relay.mm.post_calls[0]["root_id"] == "mm-root-1"


def test_dm_bridge_mm_thread_reply_becomes_tg_reply():
    relay, adapter, user = _build_dm_relay(clean_mode=False)
    relay._mm_to_tg["mm-root-1"] = 66

    async def scenario():
        await relay._handle_ws_post(
            {
                "channel_id": "dm-relay-chan",
                "id": "relay-reply-1",
                "root_id": "mm-root-1",
                "user_id": relay.bridge.mm_bot_id,
                "message": "respuesta relay en hilo",
            }
        )

    asyncio.run(scenario())

    assert adapter.sent[0]["reply_to"] == 66
