"""Mattermost API client for BridgeMost."""

import logging
import aiohttp
from pathlib import Path

logger = logging.getLogger("bridgemost.mm")


class MattermostClient:
    """Async Mattermost REST API client."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self.last_validate_error: dict | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_error_message(payload: object) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "detailed_error", "id"):
                value = payload.get(key)
                if value:
                    return str(value)
        if payload is None:
            return ""
        return str(payload)

    async def post_message(
        self, token: str, channel_id: str, message: str, file_ids: list[str] | None = None
    ) -> dict:
        """Post a message to a channel as the token owner.

        Returns a structured error dict instead of raising transport exceptions,
        so upstream Telegram handlers do not bubble out noisy uncaught errors.
        """
        session = await self._get_session()
        payload: dict = {
            "channel_id": channel_id,
            "message": message,
        }
        if file_ids:
            payload["file_ids"] = file_ids

        try:
            async with session.post(
                f"{self.base_url}/api/v4/posts",
                json=payload,
                headers=self._headers(token),
            ) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {"message": await resp.text()}
                if resp.status not in (200, 201):
                    message_text = self._extract_error_message(data) or f"HTTP {resp.status}"
                    logger.error("MM post failed (%d): %s", resp.status, data)
                    return {
                        "message": message_text,
                        "status_code": resp.status,
                        "error_type": "HTTPError",
                    }
                logger.debug("MM post OK: %s", data.get("id"))
                return data
        except Exception as e:
            logger.exception("MM post exception")
            return {
                "message": f"Mattermost post exception: {type(e).__name__}: {e}",
                "error_type": type(e).__name__,
            }

    async def upload_file(
        self, token: str, channel_id: str, file_path: str, filename: str
    ) -> str | None:
        """Upload a file and return the file_id."""
        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field("channel_id", channel_id)

        # FIX #1: Proper file handle management with context manager
        with open(file_path, "rb") as fh:
            form.add_field(
                "files",
                fh,
                filename=filename,
            )

            headers = {"Authorization": f"Bearer {token}"}
            async with session.post(
                f"{self.base_url}/api/v4/files",
                data=form,
                headers=headers,
            ) as resp:
                data = await resp.json()
                if resp.status in (200, 201):
                    file_infos = data.get("file_infos", [])
                    if file_infos:
                        fid = file_infos[0]["id"]
                        logger.debug("MM file uploaded: %s → %s", filename, fid)
                        return fid
                logger.error("MM upload failed (%d): %s", resp.status, data)
                return None

    async def get_dm_channel(self, token: str, user_id: str, other_id: str) -> str | None:
        """Get or create a DM channel between two users.
        
        Returns the 26-char channel ID, or None if the request failed.
        Validates the response to prevent garbage IDs from propagating.
        """
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/api/v4/channels/direct",
                json=[user_id, other_id],
                headers=self._headers(token),
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    logger.error(
                        "DM channel discovery failed (%d): %s",
                        resp.status, data.get("message", data),
                    )
                    return None
                channel_id = data.get("id", "")
                # Mattermost channel IDs are exactly 26 alphanumeric chars
                if not channel_id or len(channel_id) != 26 or not channel_id.isalnum():
                    logger.error(
                        "Invalid channel ID received: %r (status=%d)",
                        channel_id, resp.status,
                    )
                    return None
                return channel_id
        except Exception as e:
            logger.error("DM channel discovery exception: %s", e)
            return None

    async def validate_token(self, token: str) -> dict | None:
        """Validate a personal access token. Returns user info or None.

        Stores structured failure metadata on `last_validate_error` so callers can
        distinguish auth problems from transient Mattermost availability issues.
        """
        session = await self._get_session()
        self.last_validate_error = None
        try:
            async with session.get(
                f"{self.base_url}/api/v4/users/me",
                headers=self._headers(token),
            ) as resp:
                if resp.status == 200:
                    self.last_validate_error = None
                    return await resp.json()
                try:
                    data = await resp.json()
                except Exception:
                    data = {"message": await resp.text()}
                message_text = self._extract_error_message(data) or f"HTTP {resp.status}"
                self.last_validate_error = {
                    "kind": "http",
                    "status": resp.status,
                    "message": message_text,
                }
                logger.error("Token validation failed (%d): %s", resp.status, message_text)
                return None
        except Exception as e:
            self.last_validate_error = {
                "kind": "exception",
                "type": type(e).__name__,
                "message": str(e),
            }
            logger.error("Token validation exception: %s", e)
            return None

    async def get_posts_after(
        self, token: str, channel_id: str, after_id: str
    ) -> list[dict]:
        """Get posts in a channel after a given post ID."""
        session = await self._get_session()
        params = {"after": after_id} if after_id else {"per_page": "1"}
        async with session.get(
            f"{self.base_url}/api/v4/channels/{channel_id}/posts",
            params=params,
            headers=self._headers(token),
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                logger.error("MM get_posts failed (%d): %s", resp.status, data)
                return []

            order = data.get("order", [])
            posts = data.get("posts", {})
            return [posts[pid] for pid in reversed(order) if pid in posts]

    async def edit_post(self, token: str, post_id: str, message: str) -> dict:
        """Edit an existing post's message."""
        session = await self._get_session()
        payload = {"id": post_id, "message": message}
        async with session.put(
            f"{self.base_url}/api/v4/posts/{post_id}",
            json=payload,
            headers=self._headers(token),
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                logger.error("MM edit failed (%d): %s", resp.status, data)
            else:
                logger.debug("MM post edited: %s", post_id)
            return data

    async def delete_post(self, token: str, post_id: str) -> bool:
        """Delete a post. Returns True on success."""
        session = await self._get_session()
        async with session.delete(
            f"{self.base_url}/api/v4/posts/{post_id}",
            headers=self._headers(token),
        ) as resp:
            if resp.status == 200:
                logger.debug("MM post deleted: %s", post_id)
                return True
            data = await resp.json()
            logger.error("MM delete failed (%d): %s", resp.status, data)
            return False

    async def add_reaction(self, token: str, user_id: str, post_id: str, emoji_name: str) -> bool:
        """Add a reaction to a post."""
        session = await self._get_session()
        payload = {"user_id": user_id, "post_id": post_id, "emoji_name": emoji_name}
        async with session.post(
            f"{self.base_url}/api/v4/reactions",
            json=payload,
            headers=self._headers(token),
        ) as resp:
            if resp.status in (200, 201):
                logger.debug("MM reaction added: %s on %s", emoji_name, post_id[:8])
                return True
            data = await resp.json()
            logger.error("MM add_reaction failed (%d): %s", resp.status, data)
            return False

    async def remove_reaction(self, token: str, user_id: str, post_id: str, emoji_name: str) -> bool:
        """Remove a reaction from a post."""
        session = await self._get_session()
        async with session.delete(
            f"{self.base_url}/api/v4/reactions/{user_id}/{post_id}/{emoji_name}",
            headers=self._headers(token),
        ) as resp:
            if resp.status == 200:
                logger.debug("MM reaction removed: %s on %s", emoji_name, post_id[:8])
                return True
            data = await resp.json()
            logger.error("MM remove_reaction failed (%d): %s", resp.status, data)
            return False

    async def download_file(self, token: str, file_id: str, dest: str) -> str:
        """Download a file from MM to local path."""
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            f"{self.base_url}/api/v4/files/{file_id}",
            headers=headers,
        ) as resp:
            if resp.status == 200:
                with open(dest, "wb") as f:
                    f.write(await resp.read())
                return dest
            logger.error("MM download failed (%d)", resp.status)
            return ""

    async def get_file_info(self, token: str, file_id: str) -> dict | None:
        """Get file metadata (name, extension, mime_type, size)."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/api/v4/files/{file_id}/info",
                headers=self._headers(token),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error("MM get_file_info failed (%d)", resp.status)
                return None
        except Exception as e:
            logger.error("MM get_file_info exception: %s", e)
            return None

    async def get_user_status(self, token: str, user_id: str) -> dict | None:
        """Get a user's online status (online/away/dnd/offline)."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/api/v4/users/{user_id}/status",
                headers=self._headers(token),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception:
            return None

    async def get_user_info(self, token: str, user_id: str) -> dict | None:
        """Get user details (username, email, roles, etc.)."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/api/v4/users/{user_id}",
                headers=self._headers(token),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception:
            return None

    async def get_last_post_in_channel(self, token: str, channel_id: str) -> dict | None:
        """Get the most recent post in a channel."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/api/v4/channels/{channel_id}/posts",
                headers=self._headers(token),
                params={"per_page": 1, "page": 0},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    order = data.get("order", [])
                    posts = data.get("posts", {})
                    if order and order[0] in posts:
                        return posts[order[0]]
                return None
        except Exception:
            return None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
