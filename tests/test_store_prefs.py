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
            store.set_active_bot(12345, "agripinia")
            assert store.get_active_bot(12345) == "agripinia"
            store.close()

    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "test.db"
            store = MessageStore(db)
            store.open()
            store.set_active_bot(12345, "jarvis")
            store.close()

            # Reopen — should persist
            store2 = MessageStore(db)
            store2.open()
            assert store2.get_active_bot(12345) == "jarvis"
            store2.close()

    def test_update_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            store.set_active_bot(12345, "apex")
            store.set_active_bot(12345, "agripinia")
            assert store.get_active_bot(12345) == "agripinia"
            store.close()

    def test_multiple_users(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            store.set_active_bot(111, "apex")
            store.set_active_bot(222, "jarvis")
            assert store.get_active_bot(111) == "apex"
            assert store.get_active_bot(222) == "jarvis"
            store.close()

    def test_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            store = MessageStore(Path(d) / "test.db")
            store.open()
            assert store.get_active_bot(99999) is None
            store.close()
