"""Tests for user preferences persistence in MessageStore."""

import tempfile
from pathlib import Path

from bridgemost.store import MessageStore


class TestActiveBot:
    """Test active bot persistence across restarts."""

    def test_set_and_get(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            store.set_active_bot(12345, "bot-gamma")
            assert store.get_active_bot(12345) == "bot-gamma"
            store.close()

    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            store = MessageStore(db)
            store.open()
            store.set_active_bot(12345, "bot-beta")
            store.close()

            # Reopen — should persist
            store2 = MessageStore(db)
            store2.open()
            assert store2.get_active_bot(12345) == "bot-beta"
            store2.close()

    def test_update_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            store.set_active_bot(12345, "bot-alpha")
            store.set_active_bot(12345, "bot-gamma")
            assert store.get_active_bot(12345) == "bot-gamma"
            store.close()

    def test_multiple_users(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            store.set_active_bot(111, "bot-alpha")
            store.set_active_bot(222, "bot-beta")
            assert store.get_active_bot(111) == "bot-alpha"
            assert store.get_active_bot(222) == "bot-beta"
            store.close()

    def test_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            assert store.get_active_bot(99999) is None
            store.close()
