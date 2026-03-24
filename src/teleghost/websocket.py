"""Mattermost WebSocket client for real-time event streaming."""

import asyncio
import json
import logging
from typing import Callable, Awaitable

import aiohttp

logger = logging.getLogger("teleghost.ws")

# Mattermost WS event types we care about
EVENT_POSTED = "posted"


class MattermostWebSocket:
    """Persistent WebSocket connection to Mattermost for real-time events.

    Handles authentication, automatic reconnection with exponential backoff,
    and sequence-based message tracking.
    """

    def __init__(
        self,
        ws_url: str,
        token: str,
        on_post: Callable[[dict], Awaitable[None]],
        reconnect_base: float = 2.0,
        reconnect_max: float = 60.0,
    ):
        self.ws_url = ws_url.rstrip("/")
        self.token = token
        self.on_post = on_post
        self._reconnect_base = reconnect_base
        self._reconnect_max = reconnect_max
        self._seq = 1
        self._running = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the WebSocket listener in a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info("WebSocket listener started")

    async def stop(self):
        """Stop the WebSocket listener gracefully."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("WebSocket listener stopped")

    async def _run_forever(self):
        """Main loop: connect, listen, reconnect on failure."""
        consecutive_failures = 0

        while self._running:
            try:
                await self._connect_and_listen()
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                delay = min(
                    self._reconnect_base * (2 ** (consecutive_failures - 1)),
                    self._reconnect_max,
                )
                logger.warning(
                    "WebSocket disconnected (%s), reconnecting in %.1fs (attempt %d)",
                    e, delay, consecutive_failures,
                )
                await asyncio.sleep(delay)

    async def _connect_and_listen(self):
        """Single connection lifecycle: connect, auth, listen until disconnect."""
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=90)
            )

        ws_endpoint = f"{self.ws_url}/api/v4/websocket"
        logger.info("Connecting to %s", ws_endpoint)

        headers = {"Authorization": f"Bearer {self.token}"}

        async with self._session.ws_connect(
            ws_endpoint,
            headers=headers,
            heartbeat=30.0,
            max_msg_size=16 * 1024 * 1024,
        ) as ws:
            self._ws = ws

            # Wait for hello event (confirms auth via header succeeded)
            hello_msg = await ws.receive()
            if hello_msg.type == aiohttp.WSMsgType.TEXT:
                hello = json.loads(hello_msg.data)
                if hello.get("event") == "hello":
                    logger.info(
                        "WS authenticated — %s (v%s)",
                        hello.get("data", {}).get("server_hostname", "?"),
                        hello.get("data", {}).get("server_version", "?")[:6],
                    )
                else:
                    logger.warning("Unexpected first WS message: %s", hello)
            elif hello_msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                raise ConnectionError("WS auth rejected (CLOSE on connect)")
            else:
                logger.warning("Unexpected WS msg type on connect: %s", hello_msg.type)

            # Listen for events
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_event(data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid WS JSON: %s", msg.data[:200])

                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # Mattermost may send binary frames; decode as UTF-8
                    try:
                        data = json.loads(msg.data.decode("utf-8"))
                        await self._handle_event(data)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.debug("Ignoring binary WS frame (%d bytes)", len(msg.data))

                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                    aiohttp.WSMsgType.CLOSE,
                ):
                    logger.warning("WS connection closed: %s", msg.type)
                    break

                elif msg.type == aiohttp.WSMsgType.PING:
                    await ws.pong(msg.data)

                elif msg.type == aiohttp.WSMsgType.PONG:
                    pass  # heartbeat response

                else:
                    logger.debug("Unknown WS msg type: %s", msg.type)

    async def _handle_event(self, data: dict):
        """Process a single WebSocket event."""
        event = data.get("event")

        if event == EVENT_POSTED:
            post_data = data.get("data", {})
            post_str = post_data.get("post", "")

            if isinstance(post_str, str):
                try:
                    post = json.loads(post_str)
                except json.JSONDecodeError:
                    return
            elif isinstance(post_str, dict):
                post = post_str
            else:
                return

            try:
                await self.on_post(post)
            except Exception as e:
                logger.error("Error handling WS post event: %s", e, exc_info=True)
