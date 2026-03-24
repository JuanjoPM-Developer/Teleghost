"""Mattermost WebSocket client for real-time event streaming."""

import asyncio
import json
import logging
from typing import Callable, Awaitable

import aiohttp

logger = logging.getLogger("bridgemost.ws")

# Mattermost WS event types we care about
EVENT_POSTED = "posted"
EVENT_POST_EDITED = "post_edited"
EVENT_POST_DELETED = "post_deleted"
EVENT_REACTION_ADDED = "reaction_added"
EVENT_REACTION_REMOVED = "reaction_removed"
EVENT_TYPING = "typing"


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
        on_post_edited: Callable[[dict], Awaitable[None]] | None = None,
        on_post_deleted: Callable[[dict], Awaitable[None]] | None = None,
        on_reaction_added: Callable[[dict], Awaitable[None]] | None = None,
        on_reaction_removed: Callable[[dict], Awaitable[None]] | None = None,
        on_typing: Callable[[dict], Awaitable[None]] | None = None,
        reconnect_base: float = 2.0,
        reconnect_max: float = 60.0,
    ):
        self.ws_url = ws_url.rstrip("/")
        self.token = token
        self.on_post = on_post
        self.on_post_edited = on_post_edited
        self.on_post_deleted = on_post_deleted
        self.on_reaction_added = on_reaction_added
        self.on_reaction_removed = on_reaction_removed
        self.on_typing = on_typing
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

    def _parse_post(self, data: dict) -> dict | None:
        """Extract post dict from WS event data."""
        post_data = data.get("data", {})
        post_str = post_data.get("post", "")

        if isinstance(post_str, str):
            try:
                return json.loads(post_str)
            except json.JSONDecodeError:
                return None
        elif isinstance(post_str, dict):
            return post_str
        return None

    async def _handle_event(self, data: dict):
        """Process a single WebSocket event."""
        event = data.get("event")

        if event == EVENT_POSTED:
            post = self._parse_post(data)
            if post:
                try:
                    await self.on_post(post)
                except Exception as e:
                    logger.error("Error handling posted event: %s", e, exc_info=True)

        elif event == EVENT_POST_EDITED:
            if self.on_post_edited:
                post = self._parse_post(data)
                if post:
                    try:
                        await self.on_post_edited(post)
                    except Exception as e:
                        logger.error("Error handling post_edited event: %s", e, exc_info=True)

        elif event == EVENT_POST_DELETED:
            if self.on_post_deleted:
                post = self._parse_post(data)
                if post:
                    try:
                        await self.on_post_deleted(post)
                    except Exception as e:
                        logger.error("Error handling post_deleted event: %s", e, exc_info=True)

        elif event == EVENT_REACTION_ADDED:
            if self.on_reaction_added:
                reaction = data.get("data", {}).get("reaction", "")
                if isinstance(reaction, str):
                    try:
                        reaction = json.loads(reaction)
                    except json.JSONDecodeError:
                        return
                if reaction:
                    try:
                        await self.on_reaction_added(reaction)
                    except Exception as e:
                        logger.error("Error handling reaction_added: %s", e, exc_info=True)

        elif event == EVENT_REACTION_REMOVED:
            if self.on_reaction_removed:
                reaction = data.get("data", {}).get("reaction", "")
                if isinstance(reaction, str):
                    try:
                        reaction = json.loads(reaction)
                    except json.JSONDecodeError:
                        return
                if reaction:
                    try:
                        await self.on_reaction_removed(reaction)
                    except Exception as e:
                        logger.error("Error handling reaction_removed: %s", e, exc_info=True)

        elif event == EVENT_TYPING:
            if self.on_typing:
                evt_data = data.get("data", {})
                broadcast = data.get("broadcast", {})
                typing_info = {
                    "user_id": evt_data.get("user_id") or broadcast.get("user_id", ""),
                    "channel_id": broadcast.get("channel_id", ""),
                }
                if typing_info["user_id"] and typing_info["channel_id"]:
                    try:
                        await self.on_typing(typing_info)
                    except Exception as e:
                        logger.error("Error handling typing: %s", e, exc_info=True)
