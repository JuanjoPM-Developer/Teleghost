"""Core relay engine — platform-agnostic bridge between adapters and Mattermost.

This module contains ALL the routing, mapping, sync, and orchestration logic.
It knows nothing about Telegram, Google Chat, or any specific platform.
It talks to adapters via the BaseAdapter interface.
"""

import asyncio
import datetime
import logging
import tempfile
from pathlib import Path

from .adapters.base import BaseAdapter, InboundMessage, OutboundMessage
from .config import Config, UserMapping
from .emoji import unicode_to_mm, mm_to_unicode
from .health import HealthServer
from .mattermost import MattermostClient
from .store import MessageStore
from .websocket import MattermostWebSocket
from .whisper import WhisperClient

logger = logging.getLogger("bridgemost.core")


class BridgeMostCore:
    """Platform-agnostic relay engine.

    Connects one adapter (Telegram, Google Chat, etc.) to Mattermost.
    Handles: routing, message mapping, edit/delete sync, reactions,
    typing indicators, file relay, voice transcription, PAT health.
    """

    def __init__(self, config: Config, adapter: BaseAdapter):
        self.config = config
        self.adapter = adapter
        self.mm = MattermostClient(config.mm_url)

        # Persistent message store (SQLite)
        db_path = Path(config.data_dir) / "messages.db" if config.data_dir else Path("messages.db")
        self._store = MessageStore(db_path)

        # In-memory caches
        self._tg_to_mm: dict[int, str] = {}
        self._mm_to_tg: dict[str, int] = {}
        self._map_maxlen = 5000
        self._our_post_ids: list[str] = []  # Echo prevention
        self._our_post_maxlen = 1000

        # DM channel → (user, bot) mapping
        self._dm_to_user: dict[str, tuple[UserMapping, object]] = {}

        # WebSocket
        self._ws: MattermostWebSocket | None = None
        self._running = False

        # Health server
        self.health = HealthServer(port=config.health_port)

        # Voice-to-text
        self.whisper: WhisperClient | None = None
        if config.whisper_url:
            self.whisper = WhisperClient(
                url=config.whisper_url,
                api_key=config.whisper_api_key,
                model=config.whisper_model,
                language=config.whisper_language,
            )

    # --- Message tracking ---

    def _track_pair(self, platform_id: int, mm_id: str, chat_id: int = 0):
        self._tg_to_mm[platform_id] = mm_id
        self._mm_to_tg[mm_id] = platform_id
        if len(self._tg_to_mm) > self._map_maxlen:
            oldest = next(iter(self._tg_to_mm))
            self._mm_to_tg.pop(self._tg_to_mm.pop(oldest), None)
        self._store.put(platform_id, mm_id, chat_id)

    def _lookup_mm(self, platform_id: int) -> str | None:
        mm_id = self._tg_to_mm.get(platform_id)
        if mm_id:
            return mm_id
        mm_id = self._store.get_mm(platform_id)
        if mm_id:
            self._tg_to_mm[platform_id] = mm_id
            self._mm_to_tg[mm_id] = platform_id
        return mm_id

    def _lookup_platform(self, mm_id: str) -> int | None:
        p_id = self._mm_to_tg.get(mm_id)
        if p_id:
            return p_id
        p_id = self._store.get_tg(mm_id)
        if p_id:
            self._mm_to_tg[mm_id] = p_id
            self._tg_to_mm[p_id] = mm_id
        return p_id

    def _mark_our_post(self, post_id: str):
        self._our_post_ids.append(post_id)
        if len(self._our_post_ids) > self._our_post_maxlen:
            self._our_post_ids = self._our_post_ids[-self._our_post_maxlen:]

    # --- Helpers ---

    def _get_active_bot(self, user: UserMapping):
        for bot in user.bots:
            if bot.name == user.active_bot:
                return bot
        return user.bots[0] if user.bots else None

    # --- Lifecycle ---

    async def start(self):
        """Start the core + adapter."""
        from . import __version__
        logger.info("BridgeMost v%s core starting...", __version__)

        self._store.open()
        self.health.store_count_fn = self._store.count

        # Restore persisted bot selections
        for user in self.config.users:
            saved = self._store.get_active_bot(user.telegram_id)
            if saved and any(b.name == saved for b in user.bots):
                user.active_bot = saved

        # Validate tokens
        for user in self.config.users:
            info = await self.mm.validate_token(user.mm_token)
            if not info:
                logger.critical("FATAL: Token validation FAILED for %s", user.telegram_name)
                await self.mm.close()
                self._store.close()
                raise SystemExit(1)
            logger.info("Token OK for %s (MM: %s)", user.telegram_name, info.get("username", "?"))

        # Discover DM channels
        for user in self.config.users:
            for bot in user.bots:
                if not bot.mm_dm_channel:
                    for attempt in range(3):
                        channel = await self.mm.get_dm_channel(user.mm_token, user.mm_user_id, bot.mm_bot_id)
                        if channel:
                            bot.mm_dm_channel = channel
                            logger.info("DM discovered %s→%s: %s", user.telegram_name, bot.name, channel)
                            break
                        await asyncio.sleep(2.0 * (attempt + 1))

                if bot.mm_dm_channel:
                    self._dm_to_user[bot.mm_dm_channel] = (user, bot)

        if not self._dm_to_user:
            logger.critical("FATAL: Zero DM channels — nothing to relay")
            await self.mm.close()
            self._store.close()
            raise SystemExit(1)

        # Wire adapter callbacks
        self.adapter.set_callbacks(
            on_message=self._handle_inbound_message,
            on_edit=self._handle_inbound_edit,
            on_reaction=self._handle_inbound_reaction,
            on_command=self._handle_command,
        )

        # Start adapter
        await self.adapter.start()

        # Start health
        await self.health.start()

        # Start WebSocket
        if not self.config.users:
            logger.critical("FATAL: No users configured")
            raise SystemExit(1)

        ws_url = self.config.mm_url.replace("https://", "wss://").replace("http://", "ws://")
        self._ws = MattermostWebSocket(
            ws_url=ws_url,
            token=self.config.users[0].mm_token,
            on_post=self._handle_ws_post,
            on_post_edited=self._handle_ws_edit,
            on_post_deleted=self._handle_ws_delete,
            on_reaction_added=self._handle_ws_reaction_added,
            on_reaction_removed=self._handle_ws_reaction_removed,
            on_typing=self._handle_ws_typing,
        )
        await self._ws.start()

        logger.info("BridgeMost core active — %d channels", len(self._dm_to_user))

        # PAT health check loop
        self._running = True
        counter = 0
        try:
            while self._running:
                await asyncio.sleep(1)
                counter += 1
                if counter >= 300:
                    counter = 0
                    for user in self.config.users:
                        info = await self.mm.validate_token(user.mm_token)
                        if not info:
                            logger.error("PAT EXPIRED for %s", user.telegram_name)
                            self.health.record_error()
                            await self.adapter.send_message(
                                user.telegram_id,
                                OutboundMessage(text="⚠️ BridgeMost: Token de Mattermost expirado."),
                            )
        finally:
            self._running = False
            if self._ws:
                await self._ws.stop()
            await self.health.stop()
            await self.adapter.stop()
            await self.mm.close()
            self._store.close()

    # --- Inbound from adapter ---

    async def _handle_inbound_message(self, msg: InboundMessage):
        """Process a message from the chat adapter → post to MM."""
        user = self.config.get_user_by_tg_id(msg.user_id)
        if not user:
            return

        active_bot = self._get_active_bot(user)
        if not active_bot or not active_bot.mm_dm_channel:
            return

        dm = active_bot.mm_dm_channel
        self.health.record_tg_to_mm()

        file_ids = []
        text = msg.text
        voice_prefix = ""

        # Upload file to MM
        if msg.file_path:
            fid = await self.mm.upload_file(user.mm_token, dm, msg.file_path, msg.file_name)
            if fid:
                file_ids.append(fid)

        # Whisper voice-to-text
        if msg.is_voice and msg.file_path and self.whisper:
            try:
                transcript = await self.whisper.transcribe(msg.file_path)
                if transcript:
                    voice_prefix = f"🎤 {transcript}"
            except Exception as e:
                logger.error("Whisper error: %s", e)

        # Format location/venue/poll/sticker
        if msg.location:
            lat, lon = msg.location
            map_url = f"https://www.google.com/maps?q={lat},{lon}"
            if msg.venue_name:
                loc = f"📍 {msg.venue_name}"
                if msg.venue_address:
                    loc += f" — {msg.venue_address}"
                loc += f"\n[Ver en mapa]({map_url})"
            else:
                loc = f"📍 Ubicación: [{lat}, {lon}]({map_url})"
            text = f"{text}\n{loc}" if text else loc

        if msg.poll_question:
            poll_text = f"📊 **{msg.poll_question}**\n"
            for i, opt in enumerate(msg.poll_options or []):
                poll_text += f"  {i+1}. {opt}\n"
            meta = []
            if msg.poll_anonymous:
                meta.append("Anónima")
            if msg.poll_multiple:
                meta.append("Múltiple respuesta")
            if meta:
                poll_text += f"_{' · '.join(meta)}_"
            text = f"{text}\n{poll_text}" if text else poll_text

        if msg.sticker_emoji and not text and not file_ids:
            text = msg.sticker_emoji

        if voice_prefix:
            text = f"{voice_prefix}\n{text}" if text else voice_prefix

        # Cleanup temp file
        if msg.file_path:
            try:
                Path(msg.file_path).unlink(missing_ok=True)
            except Exception:
                pass

        # Post to MM
        if text or file_ids:
            result = await self._retry_mm_post(user, dm, text, file_ids or None)
            post_id = result.get("id")
            if post_id:
                self._mark_our_post(post_id)
                self._track_pair(msg.platform_msg_id, post_id)
                # Start typing (bot is processing)
                self.adapter.start_typing_loop(user.telegram_id)

    async def _handle_inbound_edit(self, msg: InboundMessage):
        """Process an edit from the adapter → edit MM post."""
        user = self.config.get_user_by_tg_id(msg.user_id)
        if not user:
            return
        mm_id = self._lookup_mm(msg.platform_msg_id)
        if not mm_id or not msg.text:
            return
        result = await self.mm.edit_post(user.mm_token, mm_id, msg.text)
        if result.get("id"):
            self._mark_our_post(result["id"])

    async def _handle_inbound_reaction(self, msg: InboundMessage):
        """Process a reaction from the adapter → add/remove on MM."""
        user = self.config.get_user_by_tg_id(msg.user_id)
        if not user:
            return
        mm_id = self._lookup_mm(msg.reaction_msg_id)
        if not mm_id:
            return

        for emoji in (msg.reaction_added or []):
            mm_name = unicode_to_mm(emoji)
            if mm_name:
                await self.mm.add_reaction(user.mm_token, user.mm_user_id, mm_id, mm_name)

        for emoji in (msg.reaction_removed or []):
            mm_name = unicode_to_mm(emoji)
            if mm_name:
                await self.mm.remove_reaction(user.mm_token, user.mm_user_id, mm_id, mm_name)

    # --- Commands from adapter ---

    async def _handle_command(self, cmd: str, args: list[str], user_id) -> str | None:
        """Handle /bot, /bots, /status commands. Returns reply text."""
        user = self.config.get_user_by_tg_id(user_id)
        if not user:
            return None

        if cmd == "bot":
            if not args:
                lines = ["🤖 *Bots disponibles:*\n"]
                for bot in user.bots:
                    marker = "→ " if bot.name == user.active_bot else "  "
                    lines.append(f"{marker}`{bot.name}`")
                lines.append(f"\nActivo: *{user.active_bot}*")
                lines.append("Usa `/bot nombre` para cambiar")
                return "\n".join(lines)
            else:
                target = args[0].lower()
                for bot in user.bots:
                    if bot.name.lower() == target:
                        self.adapter.stop_typing_loop(user.telegram_id)
                        user.active_bot = bot.name
                        self._store.set_active_bot(user.telegram_id, bot.name)
                        return f"── Ahora hablando con *{bot.name}* ──"
                return f"❌ Bot '{target}' no encontrado. Disponibles: {', '.join(b.name for b in user.bots)}"

        elif cmd == "bots":
            lines = ["🤖 *Bot Status*\n"]
            for bot in user.bots:
                status_data = await self.mm.get_user_status(user.mm_token, bot.mm_bot_id)
                if status_data:
                    raw = status_data.get("status", "offline")
                    icons = {"online": "🟢", "away": "🟡", "dnd": "🔴", "offline": "⚫"}
                    icon = icons.get(raw, "⚪")
                else:
                    icon, raw = "❓", "unknown"
                active = " ← activo" if bot.name == user.active_bot else ""
                dm = "✅" if bot.mm_dm_channel else "❌"
                lines.append(f"{icon} `{bot.name}` — {raw} | DM: {dm}{active}")
            lines.append(f"\n📊 {len(user.bots)} bots | Activo: *{user.active_bot}*")
            return "\n".join(lines)

        elif cmd == "status":
            active_bot = self._get_active_bot(user)
            if not active_bot:
                return "❌ No hay bot activo"
            lines = [f"📋 *{active_bot.name}* — Estado detallado\n"]
            status_data = await self.mm.get_user_status(user.mm_token, active_bot.mm_bot_id)
            if status_data:
                raw = status_data.get("status", "offline")
                icons = {"online": "🟢 Online", "away": "🟡 Away", "dnd": "🔴 DND", "offline": "⚫ Offline"}
                lines.append(f"Estado: {icons.get(raw, raw)}")
                last = status_data.get("last_activity_at", 0)
                if last:
                    dt = datetime.datetime.fromtimestamp(last / 1000)
                    lines.append(f"Última actividad: {dt.strftime('%H:%M:%S')}")
            lines.append(f"📦 Mappings: {self._store.count()}")
            return "\n".join(lines)

        return None

    # --- WebSocket handlers (MM → adapter) ---

    async def _handle_ws_post(self, post: dict):
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return
        if post_id in self._our_post_ids:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        # Stop typing
        self.adapter.stop_typing_loop(user.telegram_id)

        text = post.get("message", "")
        if text and len(user.bots) > 1:
            text = f"🤖 {bot.name}: {text}"

        sent_id = None
        if text:
            self.health.record_mm_to_tg()
            sent_id = await self.adapter.send_message(
                user.telegram_id, OutboundMessage(text=text)
            )

        # File attachments
        file_ids_raw = post.get("file_ids")
        file_ids_list = file_ids_raw if isinstance(file_ids_raw, list) else []
        for fid in file_ids_list:
            try:
                await self._relay_mm_file(user, fid)
            except Exception as e:
                logger.error("File relay error: %s", e)

        if sent_id and post_id:
            self._track_pair(sent_id, post_id)

    async def _relay_mm_file(self, user: UserMapping, file_id: str):
        """Download MM file and send via adapter."""
        token = user.mm_token
        file_info = await self.mm.get_file_info(token, file_id)
        if not file_info:
            return

        filename = file_info.get("name", "file")
        mime = file_info.get("mime_type", "application/octet-stream")
        size = file_info.get("size", 0)
        ext = file_info.get("extension", "")

        suffix = f".{ext}" if ext else Path(filename).suffix or ".bin"
        local_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                local_path = tmp.name
            if not await self.mm.download_file(token, file_id, local_path):
                return

            await self.adapter.send_message(
                user.telegram_id,
                OutboundMessage(
                    file_path=local_path,
                    file_name=filename,
                    file_mime=mime,
                    file_size=size,
                ),
            )
        finally:
            if local_path:
                try:
                    Path(local_path).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _handle_ws_edit(self, post: dict):
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return
        if post_id in self._our_post_ids:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        platform_id = self._lookup_platform(post_id)
        if not platform_id:
            return

        new_text = post.get("message", "")
        if new_text:
            await self.adapter.edit_message(user.telegram_id, platform_id, new_text)

    async def _handle_ws_delete(self, post: dict):
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        platform_id = self._lookup_platform(post_id)
        if not platform_id:
            return

        await self.adapter.delete_message(user.telegram_id, platform_id)
        self._mm_to_tg.pop(post_id, None)
        self._tg_to_mm.pop(platform_id, None)

    async def _handle_ws_reaction_added(self, reaction: dict):
        post_id = reaction.get("post_id", "")
        user_id = reaction.get("user_id", "")
        emoji_name = reaction.get("emoji_name", "")

        if not post_id or not emoji_name:
            return

        platform_id = self._lookup_platform(post_id)
        if not platform_id:
            return

        target_user = None
        for ch_id, (usr, bot) in self._dm_to_user.items():
            if user_id != usr.mm_user_id and self._store.has_tg(platform_id):
                target_user = usr
                break
        if not target_user:
            return

        tg_emoji = mm_to_unicode(emoji_name)
        if tg_emoji:
            await self.adapter.set_reaction(target_user.telegram_id, platform_id, tg_emoji)

    async def _handle_ws_reaction_removed(self, reaction: dict):
        post_id = reaction.get("post_id", "")
        user_id = reaction.get("user_id", "")

        if not post_id:
            return

        platform_id = self._lookup_platform(post_id)
        if not platform_id:
            return

        target_user = None
        for ch_id, (usr, bot) in self._dm_to_user.items():
            if user_id != usr.mm_user_id and self._store.has_tg(platform_id):
                target_user = usr
                break
        if not target_user:
            return

        await self.adapter.clear_reactions(target_user.telegram_id, platform_id)

    async def _handle_ws_typing(self, typing_info: dict):
        channel_id = typing_info.get("channel_id", "")
        user_id = typing_info.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        # Extend typing if adapter supports it
        if hasattr(self.adapter, 'start_typing_loop'):
            self.adapter.start_typing_loop(user.telegram_id)

    async def _retry_mm_post(self, user, channel_id, text, file_ids, max_retries=3) -> dict:
        delay = 1.0
        last_error = {}
        for attempt in range(max_retries):
            result = await self.mm.post_message(user.mm_token, channel_id, text, file_ids)
            if result.get("id"):
                return result
            last_error = result
            if attempt == max_retries - 1:
                logger.error("MM post failed after %d retries: %s", max_retries, last_error)
                await self.adapter.send_message(
                    user.telegram_id,
                    OutboundMessage(text=f"⚠️ Mensaje no entregado: {last_error.get('message', 'error')}"),
                )
                return last_error
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
        return last_error
