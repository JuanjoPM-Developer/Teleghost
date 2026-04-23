import asyncio

import bridgemost.adapters.telegram as telegram_mod
from bridgemost.adapters.telegram import TelegramAdapter
from bridgemost.core import describe_mm_validation_failure


class DummyUpdater:
    async def start_polling(self, *args, **kwargs):
        return None

    async def stop(self):
        return None


class DummyBot:
    async def send_message(self, *args, **kwargs):
        return None


class DummyApp:
    def __init__(self):
        self.handlers = []
        self.error_handlers = []
        self.updater = DummyUpdater()
        self.bot = DummyBot()

    def add_handler(self, handler):
        self.handlers.append(handler)

    def add_error_handler(self, handler):
        self.error_handlers.append(handler)

    async def initialize(self):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None

    async def shutdown(self):
        return None


class DummyBuilder:
    def __init__(self):
        self.app = DummyApp()

    def token(self, _token):
        return self

    def build(self):
        return self.app


def test_describe_mm_validation_failure_distinguishes_auth_from_availability():
    auth = describe_mm_validation_failure({"kind": "http", "status": 401, "message": "expired"})
    outage = describe_mm_validation_failure({"kind": "http", "status": 500, "message": "boom"})
    timeout = describe_mm_validation_failure({"kind": "exception", "type": "TimeoutError", "message": "slow"})

    assert "expirado o rechazado" in auth[1]
    assert "HTTP 500" in outage[1]
    assert "TimeoutError" in timeout[1]
    assert "no disponible" in outage[1]
    assert "no disponible" in timeout[1]


def test_telegram_adapter_registers_error_handler(monkeypatch):
    builder = DummyBuilder()
    monkeypatch.setattr(telegram_mod, "ApplicationBuilder", lambda: builder)

    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[63492743])
    asyncio.run(adapter.start())
    try:
        assert len(builder.app.error_handlers) == 1
    finally:
        asyncio.run(adapter.stop())


def test_telegram_adapter_swallows_callback_exceptions_for_inbound_messages():
    adapter = TelegramAdapter("123:ABC", allowed_user_ids=[63492743])

    async def boom(_msg):
        raise TimeoutError("mattermost timeout")

    adapter._on_message = boom

    msg = type("Msg", (), {
        "message_id": 77,
        "text": "hola",
        "caption": None,
        "photo": None,
        "document": None,
        "audio": None,
        "voice": None,
        "video": None,
        "video_note": None,
        "sticker": None,
        "venue": None,
        "location": None,
        "poll": None,
    })()
    update = type("Update", (), {
        "effective_message": msg,
        "effective_user": type("User", (), {"id": 63492743})(),
    })()
    context = type("Context", (), {"bot": None})()

    asyncio.run(adapter._on_tg_message(update, context))
