"""Tests for Telegram placeholder-to-stream editing helpers."""

import asyncio

from bridgemost.adapters.telegram import TelegramAdapter


class FakeBot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )


def test_stream_edit_message_reveals_text_progressively(monkeypatch):
    adapter = TelegramAdapter("123:ABC")
    adapter._bot = FakeBot()

    async def _noop_rate_wait():
        return None

    async def _noop_sleep(_seconds):
        return None

    adapter._rate_wait = _noop_rate_wait
    monkeypatch.setattr("bridgemost.adapters.telegram.asyncio.sleep", _noop_sleep)

    asyncio.run(
        adapter.stream_edit_message(
            user_id=12345,
            platform_msg_id=99,
            new_text="Esta respuesta debe salir poco a poco en Telegram.",
            chunk_size=12,
            interval=0.0,
        )
    )

    assert len(adapter._bot.edits) >= 2
    assert adapter._bot.edits[-1]["text"].replace("\\.", ".") == "Esta respuesta debe salir poco a poco en Telegram."