"""Tests for degraded startup mode of DM bridges."""

import asyncio
from types import SimpleNamespace

from bridgemost.core import DmBridgeRelay
from bridgemost.__main__ import _run_dm_bridge_relay


class FailingRelay:
    def __init__(self, exc: BaseException):
        self.bridge = SimpleNamespace(name="eywa")
        self.exc = exc
        self.failed = []

    async def start(self):
        raise self.exc

    def mark_failed(self, error):
        self.failed.append(str(error))


def test_guarded_dm_bridge_failure_does_not_bubble_runtime_error():
    relay = FailingRelay(RuntimeError("bad token"))
    asyncio.run(_run_dm_bridge_relay(relay))
    assert relay.failed == ["bad token"]


def test_guarded_dm_bridge_failure_does_not_bubble_system_exit():
    relay = FailingRelay(SystemExit("unauthorized"))
    asyncio.run(_run_dm_bridge_relay(relay))
    assert relay.failed == ["unauthorized"]


def test_dm_bridge_stats_include_state_and_last_error():
    relay = DmBridgeRelay.__new__(DmBridgeRelay)
    relay.bridge = SimpleNamespace(name="eywa")
    relay._stats = {"tg_to_mm": 0, "mm_to_tg": 0, "errors": 1}
    relay._dm_to_user = {}
    relay._state = "failed"
    relay._last_error = "unauthorized"

    assert relay.stats_snapshot() == {
        "name": "eywa",
        "tg_to_mm": 0,
        "mm_to_tg": 0,
        "errors": 1,
        "channels": 0,
        "state": "failed",
        "last_error": "unauthorized",
    }
