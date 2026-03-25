"""Tests for health server."""

from bridgemost.health import HealthServer


class TestHealthServer:
    """Test health server counters and state."""

    def test_initial_counters(self):
        hs = HealthServer(port=0)
        assert hs._stats["tg_to_mm"] == 0
        assert hs._stats["mm_to_tg"] == 0
        assert hs._stats["errors"] == 0

    def test_record_tg_to_mm(self):
        hs = HealthServer(port=0)
        hs.record_tg_to_mm()
        hs.record_tg_to_mm()
        assert hs._stats["tg_to_mm"] == 2

    def test_record_mm_to_tg(self):
        hs = HealthServer(port=0)
        hs.record_mm_to_tg()
        assert hs._stats["mm_to_tg"] == 1

    def test_record_error(self):
        hs = HealthServer(port=0)
        hs.record_error()
        hs.record_error()
        hs.record_error()
        assert hs._stats["errors"] == 3

    def test_store_count_fn_default(self):
        hs = HealthServer(port=0)
        assert hs.store_count_fn is None or callable(hs.store_count_fn)
