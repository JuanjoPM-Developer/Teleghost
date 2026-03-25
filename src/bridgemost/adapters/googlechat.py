"""Google Chat adapter for BridgeMost.

Bridges Google Workspace Chat Spaces with Mattermost via the Google Chat API.
Uses a Service Account with domain-wide delegation to post as the real user
(ghost mode). Listens for messages via polling (Pub/Sub planned for v2.1+).

Requirements:
  - Google Workspace (not personal Gmail)
  - Service Account with domain-wide delegation enabled
  - Google Chat API enabled in the GCP project
  - Chat scopes authorized in Admin Console (Workspace admin)

Config example:
  googlechat:
    credentials_file: "/path/to/service-account.json"
    delegated_user: "user@company.com"
    space: "spaces/AAAA..."
    poll_interval: 2.0
"""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import BaseAdapter, InboundMessage, OutboundMessage

logger = logging.getLogger("bridgemost.adapter.googlechat")

# Google Chat API scopes
SCOPES = [
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
]

# Google Chat message length limit
GCHAT_MAX_LENGTH = 4096


def split_message(text: str, max_len: int = GCHAT_MAX_LENGTH) -> list[str]:
    """Split a long message into chunks that fit Google Chat's limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = max_len
        nl = remaining.rfind("\n", 0, max_len)
        if nl > max_len // 2:
            split_at = nl + 1
        else:
            sp = remaining.rfind(" ", 0, max_len)
            if sp > max_len // 2:
                split_at = sp + 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


class GoogleChatAdapter(BaseAdapter):
    """Google Chat adapter using Chat API with Service Account delegation."""

    def __init__(
        self,
        credentials_file: str,
        delegated_user: str,
        space: str,
        poll_interval: float = 2.0,
        user_id: Any = None,
    ):
        """
        Args:
            credentials_file: Path to service-account.json
            delegated_user: Email of the user to impersonate (ghost mode)
            space: Google Chat space name (e.g., "spaces/AAAAxyz...")
            poll_interval: Seconds between message polls
            user_id: Platform user ID (used by core for routing)
        """
        self.credentials_file = credentials_file
        self.delegated_user = delegated_user
        self.space = space
        self.poll_interval = poll_interval
        self.user_id = user_id or delegated_user

        self._service = None
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._last_message_time: str | None = None
        self._typing_tasks: dict[Any, asyncio.Task] = {}

        # Message ID tracking: gchat_name → local tracking
        self._seen_messages: set[str] = set()
        self._our_messages: set[str] = set()

    def _build_service(self):
        """Build the Google Chat API service with delegated credentials."""
        creds = service_account.Credentials.from_service_account_file(
            self.credentials_file,
            scopes=SCOPES,
            subject=self.delegated_user,
        )
        self._service = build("chat", "v1", credentials=creds)
        logger.info(
            "Google Chat API authenticated as %s (delegated)",
            self.delegated_user,
        )

    async def start(self) -> None:
        """Start the adapter — authenticate and begin polling."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._build_service)

        # Validate space exists
        try:
            space_info = await self._api_call(
                self._service.spaces().get(name=self.space)
            )
            display = space_info.get("displayName", self.space)
            logger.info("Connected to space: %s (%s)", display, self.space)
        except HttpError as e:
            logger.critical("Cannot access space %s: %s", self.space, e)
            raise SystemExit(1)

        # Get initial message state
        messages = await self._list_messages(page_size=1)
        if messages:
            self._last_message_time = messages[0].get("createTime", "")
            self._seen_messages.add(messages[0].get("name", ""))

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Google Chat adapter started — polling every %.1fs", self.poll_interval)

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        # Cancel all typing tasks
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        logger.info("Google Chat adapter stopped")

    async def send_message(self, user_id: Any, msg: OutboundMessage) -> Any:
        """Send a message to the space."""
        if msg.file_path and not msg.text:
            # Google Chat API doesn't support file uploads via REST in the same
            # way — files are uploaded via media endpoint. For now, send filename.
            text = f"📎 {msg.file_name or 'file'}"
        elif msg.text:
            text = msg.text
        else:
            return None

        sent_name = None
        for chunk in split_message(text):
            result = await self._create_message(chunk)
            if result:
                name = result.get("name", "")
                self._our_messages.add(name)
                if not sent_name:
                    sent_name = name
        return sent_name

    async def send_typing(self, user_id: Any) -> None:
        """Google Chat doesn't have a public typing indicator API — no-op."""
        pass

    async def edit_message(self, user_id: Any, platform_msg_id: Any, new_text: str) -> bool:
        """Edit a message in the space."""
        if not platform_msg_id or not new_text:
            return False
        try:
            body = {"text": new_text}
            await self._api_call(
                self._service.spaces().messages().patch(
                    name=platform_msg_id,
                    updateMask="text",
                    body=body,
                )
            )
            self._our_messages.add(platform_msg_id)
            return True
        except HttpError as e:
            logger.error("Edit failed for %s: %s", platform_msg_id, e)
            return False

    async def delete_message(self, user_id: Any, platform_msg_id: Any) -> bool:
        """Delete a message in the space."""
        if not platform_msg_id:
            return False
        try:
            await self._api_call(
                self._service.spaces().messages().delete(name=platform_msg_id)
            )
            return True
        except HttpError as e:
            logger.error("Delete failed for %s: %s", platform_msg_id, e)
            return False

    async def set_reaction(self, user_id: Any, platform_msg_id: Any, emoji: str) -> bool:
        """Add a reaction. Google Chat API supports emoji reactions."""
        if not platform_msg_id or not emoji:
            return False
        try:
            body = {"emoji": {"unicode": emoji}}
            await self._api_call(
                self._service.spaces().messages().reactions().create(
                    parent=platform_msg_id,
                    body=body,
                )
            )
            return True
        except HttpError as e:
            logger.warning("Reaction failed for %s: %s", platform_msg_id, e)
            return False

    async def clear_reactions(self, user_id: Any, platform_msg_id: Any) -> bool:
        """Clear reactions. Chat API doesn't have a bulk clear — best effort."""
        # Google Chat doesn't expose a "clear all my reactions" endpoint.
        # Individual reaction deletion requires the reaction resource name.
        logger.debug("clear_reactions not fully supported for Google Chat")
        return False

    # --- Typing indicator (synthetic, same pattern as Telegram adapter) ---

    def start_typing_loop(self, chat_id: Any) -> None:
        """Google Chat has no typing API, but we keep the interface."""
        pass

    def stop_typing_loop(self, chat_id: Any) -> None:
        """No-op for Google Chat."""
        pass

    # --- Internal methods ---

    async def _api_call(self, request):
        """Execute a Google API request in a thread executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, request.execute)

    async def _create_message(self, text: str) -> dict | None:
        """Create a message in the space as the delegated user."""
        try:
            body = {"text": text}
            result = await self._api_call(
                self._service.spaces().messages().create(
                    parent=self.space,
                    body=body,
                )
            )
            return result
        except HttpError as e:
            logger.error("Failed to send message: %s", e)
            return None

    async def _list_messages(self, page_size: int = 25) -> list[dict]:
        """List recent messages in the space."""
        try:
            result = await self._api_call(
                self._service.spaces().messages().list(
                    parent=self.space,
                    pageSize=page_size,
                    orderBy="createTime desc",
                )
            )
            messages = result.get("messages", [])
            messages.reverse()  # Oldest first
            return messages
        except HttpError as e:
            logger.error("Failed to list messages: %s", e)
            return []

    async def _poll_loop(self):
        """Poll for new messages in the space."""
        logger.info("Polling loop started")
        while self._running:
            try:
                messages = await self._list_messages(page_size=10)
                for msg in messages:
                    name = msg.get("name", "")
                    if name in self._seen_messages:
                        continue
                    if name in self._our_messages:
                        self._seen_messages.add(name)
                        continue

                    self._seen_messages.add(name)

                    # Prune seen set
                    if len(self._seen_messages) > 5000:
                        self._seen_messages = set(list(self._seen_messages)[-2000:])

                    sender = msg.get("sender", {})
                    sender_type = sender.get("type", "")

                    # Skip bot messages — we only want human messages inbound
                    # and bot messages are relayed via WebSocket from MM
                    if sender_type == "BOT":
                        continue

                    sender_name = sender.get("displayName", "Unknown")
                    text = msg.get("text", "").strip()

                    if not text:
                        continue

                    logger.info("GChat→Core [%s]: %s", sender_name, text[:80])

                    # Commands
                    if text.startswith("/") and self._on_command:
                        parts = text.split(maxsplit=1)
                        cmd = parts[0][1:]  # Remove /
                        args = parts[1].split() if len(parts) > 1 else []
                        reply = await self._on_command(cmd, args, self.user_id)
                        if reply:
                            await self._create_message(reply)
                        continue

                    # Regular message
                    if self._on_message:
                        inbound = InboundMessage(
                            platform_msg_id=name,
                            user_id=self.user_id,
                            text=text,
                        )
                        await self._on_message(inbound)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll error: %s", e)

            await asyncio.sleep(self.poll_interval)

        logger.info("Polling loop ended")
