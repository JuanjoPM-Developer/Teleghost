"""Health payload tests for degraded DM bridge reporting."""

import asyncio
import json

from bridgemost.health import HealthServer


def test_health_payload_includes_dm_bridge_state_and_error():
    hs = HealthServer(port=0)
    hs.dm_bridges_fn = lambda: [
        {
            "name": "eywa",
            "tg_to_mm": 0,
            "mm_to_tg": 0,
            "errors": 1,
            "channels": 0,
            "state": "failed",
            "last_error": "Unauthorized",
        }
    ]

    async def _run():
        resp = await hs._handle_health(None)
        return json.loads(resp.text)

    payload = asyncio.run(_run())
    assert payload["dm_bridges"][0]["state"] == "failed"
    assert payload["dm_bridges"][0]["last_error"] == "Unauthorized"
