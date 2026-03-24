"""Tests for SQLite persistent message store."""

import tempfile
import time
from pathlib import Path

import pytest
from bridgemost.store import MessageStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh MessageStore for each test."""
    db = tmp_path / "test_messages.db"
    s = MessageStore(db, ttl_days=30)
    s.open()
    yield s
    s.close()


class TestBasicOperations:
    """Test put, get, count."""

    def test_put_and_get_mm(self, store):
        store.put(100, "abc123def456", tg_chat_id=1)
        assert store.get_mm(100, tg_chat_id=1) == "abc123def456"

    def test_put_and_get_tg(self, store):
        store.put(200, "xyz789", tg_chat_id=1)
        assert store.get_tg("xyz789") == 200

    def test_get_nonexistent_returns_none(self, store):
        assert store.get_mm(999) is None
        assert store.get_tg("nonexistent") is None

    def test_count(self, store):
        assert store.count() == 0
        store.put(1, "a", tg_chat_id=1)
        store.put(2, "b", tg_chat_id=1)
        assert store.count() == 2

    def test_has_tg(self, store):
        assert store.has_tg(42) is False
        store.put(42, "post_42", tg_chat_id=1)
        assert store.has_tg(42) is True

    def test_overwrite(self, store):
        store.put(1, "old_post", tg_chat_id=1)
        store.put(1, "new_post", tg_chat_id=1)
        assert store.get_mm(1, tg_chat_id=1) == "new_post"
        assert store.count() == 1


class TestMultiUser:
    """Test multi-user (different tg_chat_id) isolation."""

    def test_same_msg_id_different_chats(self, store):
        store.put(100, "post_user_a", tg_chat_id=111)
        store.put(100, "post_user_b", tg_chat_id=222)
        assert store.get_mm(100, tg_chat_id=111) == "post_user_a"
        assert store.get_mm(100, tg_chat_id=222) == "post_user_b"
        assert store.count() == 2


class TestPersistence:
    """Test that data survives close/reopen."""

    def test_survives_restart(self, tmp_path):
        db = tmp_path / "persist.db"

        s1 = MessageStore(db, ttl_days=30)
        s1.open()
        s1.put(1, "survived", tg_chat_id=1)
        s1.close()

        s2 = MessageStore(db, ttl_days=30)
        s2.open()
        assert s2.get_mm(1, tg_chat_id=1) == "survived"
        assert s2.count() == 1
        s2.close()


class TestPruning:
    """Test TTL-based cleanup."""

    def test_old_entries_pruned(self, tmp_path):
        db = tmp_path / "prune.db"
        s = MessageStore(db, ttl_days=0)  # 0 days = prune everything
        s.open()
        # Manually insert an old entry
        s._conn.execute(
            "INSERT INTO message_map (tg_msg_id, mm_post_id, tg_chat_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (1, "old", 1, time.time() - 86400),  # 1 day old
        )
        s._conn.commit()
        assert s.count() == 1

        s._prune()
        assert s.count() == 0
        s.close()
