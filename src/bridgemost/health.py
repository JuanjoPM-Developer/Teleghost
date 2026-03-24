"""Health check HTTP endpoint for BridgeMost monitoring."""

import asyncio
import logging
import time
from aiohttp import web

logger = logging.getLogger("bridgemost.health")


class HealthServer:
    """Minimal HTTP server exposing /health for monitoring."""

    def __init__(self, port: int = 9191):
        self.port = port
        self._start_time = time.time()
        self._stats = {
            "tg_to_mm": 0,
            "mm_to_tg": 0,
            "errors": 0,
            "last_tg_msg": 0.0,
            "last_mm_msg": 0.0,
        }
        self._runner: web.AppRunner | None = None

    def record_tg_to_mm(self):
        self._stats["tg_to_mm"] += 1
        self._stats["last_tg_msg"] = time.time()

    def record_mm_to_tg(self):
        self._stats["mm_to_tg"] += 1
        self._stats["last_mm_msg"] = time.time()

    def record_error(self):
        self._stats["errors"] += 1

    async def _handle_health(self, request: web.Request) -> web.Response:
        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        return web.json_response({
            "status": "ok",
            "version": "0.6.0",
                "transport": "websocket",
            "uptime": f"{hours}h{minutes}m{seconds}s",
            "uptime_seconds": uptime,
            "messages": {
                "tg_to_mm": self._stats["tg_to_mm"],
                "mm_to_tg": self._stats["mm_to_tg"],
                "errors": self._stats["errors"],
            },
            "last_activity": {
                "tg_msg_ago": int(time.time() - self._stats["last_tg_msg"]) if self._stats["last_tg_msg"] else None,
                "mm_msg_ago": int(time.time() - self._stats["last_mm_msg"]) if self._stats["last_mm_msg"] else None,
            },
        })

    async def start(self):
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("Health endpoint at http://127.0.0.1:%d/health", self.port)

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
