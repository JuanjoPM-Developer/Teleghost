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
from .config import Config, DmBridge, UserMapping
from .emoji import unicode_to_mm, mm_to_unicode
from .health import HealthServer
from .mattermost import MattermostClient
from .store import MessageStore
from .telegram_presentation import TelegramPresentationMixin
from .websocket import MattermostWebSocket
from .whisper import WhisperClient

logger = logging.getLogger("bridgemost.core")


def describe_mm_validation_failure(error: dict | None) -> tuple[str, str]:
    """Classify Mattermost token validation failures for operator-facing alerts."""
    if not error:
        return (
            "unknown",
            "⚠️ BridgeMost: Mattermost no disponible o validación fallida.",
        )

    if error.get("kind") == "http":
        status = error.get("status")
        if status in (401, 403):
            return (
                "auth",
                "⚠️ BridgeMost: Token de Mattermost expirado o rechazado.",
            )
        return (
            "availability",
            f"⚠️ BridgeMost: Mattermost no disponible o validación fallida (HTTP {status}).",
        )

    if error.get("kind") == "exception":
        error_type = error.get("type") or "Exception"
        return (
            "availability",
            f"⚠️ BridgeMost: Mattermost no disponible o validación fallida ({error_type}).",
        )

    return (
        "unknown",
        "⚠️ BridgeMost: Mattermost no disponible o validación fallida.",
    )


class BridgeMostCore(TelegramPresentationMixin):
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

        # Channels handled by DM bridges — main relay ignores these
        self._dm_bridge_channels: set[str] = set()

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

        self._init_telegram_presentation()

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

        # Discover which DM channels are handled by DM bridges (to avoid duplicates)
        for bridge in self.config.dm_bridges:
            for user in self.config.users:
                channel = await self.mm.get_dm_channel(user.mm_token, user.mm_user_id, bridge.mm_bot_id)
                if channel:
                    self._dm_bridge_channels.add(channel)
                    logger.info("DM bridge '%s' owns channel %s — relay will skip it", bridge.name, channel)

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
                            failure = self.mm.last_validate_error
                            failure_kind, notice = describe_mm_validation_failure(failure)
                            logger.error(
                                "Mattermost token validation failed for %s (%s): %s",
                                user.telegram_name,
                                failure_kind,
                                failure,
                            )
                            self.health.record_error()
                            if hasattr(self.adapter, "send_raw_text"):
                                await self.adapter.send_raw_text(user.telegram_id, notice)
                            else:
                                await self.adapter.send_message(
                                    user.telegram_id,
                                    OutboundMessage(text=notice),
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
                await self._schedule_placeholder(dm, user.telegram_id)

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
        """Handle BridgeMost local commands from Telegram. Returns reply text."""
        user = self.config.get_user_by_tg_id(user_id)
        if not user:
            return None

        if cmd == "bridge":
            return (
                "🌉 Usa `/bridge bot`, `/bridge bots` o `/bridge status` para los controles locales.\n"
                "Las demás slash commands se reenvían directamente a Hermes."
            )

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
        if channel_id in self._dm_bridge_channels:
            return  # Handled by dedicated DM bridge relay
        if post_id in self._our_post_ids:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        raw_text = post.get("message", "")
        if self._should_suppress_mm_text(raw_text):
            return

        # Stop typing only when a visible response arrives.
        self.adapter.stop_typing_loop(user.telegram_id)

        text = raw_text
        if text and len(user.bots) > 1:
            text = f"🤖 {bot.name}: {text}"

        sent_id = None
        if text:
            self.health.record_mm_to_tg()
            sent_id = await self._present_visible_text(
                channel_id, user.telegram_id, post_id, text
            )
        else:
            await self._clear_pending_presentation(channel_id, user.telegram_id, delete_placeholder=True)

        # File attachments
        file_ids_raw = post.get("file_ids")
        file_ids_list = file_ids_raw if isinstance(file_ids_raw, list) else []
        for fid in file_ids_list:
            try:
                await self._relay_mm_file(user, fid)
            except Exception as e:
                logger.error("File relay error: %s", e)

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
        import asyncio
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return
        if channel_id in self._dm_bridge_channels:
            return
        if post_id in self._our_post_ids:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        raw_text = post.get("message", "")
        if not raw_text:
            return
        if self._should_suppress_mm_text(raw_text):
            return

        new_text = raw_text
        if len(user.bots) > 1:
            new_text = f"🤖 {bot.name}: {new_text}"

        platform_id = self._lookup_platform(post_id)
        if not platform_id:
            self.adapter.stop_typing_loop(user.telegram_id)
            await self._present_visible_text(channel_id, user.telegram_id, post_id, new_text)
            return

        # Debounce: buffer rapid edits (streaming bots) to avoid TG flood control
        if not hasattr(self, "_edit_debounce"):
            self._edit_debounce = {}
            self._edit_pending = {}
        self._edit_pending[post_id] = (new_text, user, platform_id)
        prev_task = self._edit_debounce.get(post_id)
        if prev_task and not prev_task.done():
            prev_task.cancel()

        async def _flush_edit():
            await asyncio.sleep(2.0)
            item = self._edit_pending.pop(post_id, None)
            self._edit_debounce.pop(post_id, None)
            if item:
                text, usr, pid = item
                await self.adapter.edit_message(usr.telegram_id, pid, text)

        self._edit_debounce[post_id] = asyncio.ensure_future(_flush_edit())

    async def _handle_ws_delete(self, post: dict):
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return
        if channel_id in self._dm_bridge_channels:
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
        if channel_id in self._dm_bridge_channels:
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


class DmBridgeRelay(TelegramPresentationMixin):
    """Dedicated DM bridge: one TG bot ↔ one MM bot's DM channel per user.

    Each relay instance polls its own Telegram bot token and subscribes to the
    MM DM channel between each configured user and the target MM bot.
    No bot-switching commands — the bridge is fixed.
    """

    def __init__(self, config: Config, bridge: DmBridge):
        from .adapters.telegram import TelegramAdapter

        self.config = config
        self.bridge = bridge

        allowed_ids = [u.telegram_id for u in config.users] if config.users else None
        self.adapter = TelegramAdapter(
            bot_token=bridge.tg_bot_token,
            allowed_user_ids=allowed_ids,
        )
        self.mm = MattermostClient(config.mm_url)

        db_name = f"dm_{bridge.name}.db"
        db_path = Path(config.data_dir) / db_name if config.data_dir else Path(db_name)
        self._store = MessageStore(db_path)

        self._tg_to_mm: dict[int, str] = {}
        self._mm_to_tg: dict[str, int] = {}
        self._map_maxlen = 5000
        self._our_post_ids: list[str] = []
        self._our_post_maxlen = 1000

        # channel_id → UserMapping for this relay's DM channels
        self._dm_to_user: dict[str, UserMapping] = {}

        self._ws: MattermostWebSocket | None = None
        self._running = False

        self.whisper: WhisperClient | None = None
        if config.whisper_url:
            self.whisper = WhisperClient(
                url=config.whisper_url,
                api_key=config.whisper_api_key,
                model=config.whisper_model,
                language=config.whisper_language,
            )

        self._init_telegram_presentation()

        # Edit debounce: post_id → asyncio.Task (delays TG edit by 2s)
        self._edit_debounce: dict[str, object] = {}
        self._edit_pending: dict[str, str] = {}  # post_id → latest text
        self._edit_debounce_secs = 2.0

        # Stats for health reporting
        self._stats = {"tg_to_mm": 0, "mm_to_tg": 0, "errors": 0}
        self._state = "starting"
        self._last_error = ""

    def mark_failed(self, error: BaseException | str):
        """Mark this relay as failed but keep the main process alive."""
        self._state = "failed"
        self._last_error = str(error)
        self._stats["errors"] += 1
        self._running = False

    def stats_snapshot(self) -> dict:
        """Return current stats for health reporting."""
        return {
            "name": self.bridge.name,
            "tg_to_mm": self._stats["tg_to_mm"],
            "mm_to_tg": self._stats["mm_to_tg"],
            "errors": self._stats["errors"],
            "channels": len(self._dm_to_user),
            "state": self._state,
            "last_error": self._last_error or None,
        }

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

    # --- Lifecycle ---

    async def start(self):
        """Start this DM bridge relay."""
        from . import __version__
        logger.info(
            "DmBridgeRelay '%s' v%s starting (mm_bot_id=%s)...",
            self.bridge.name, __version__, self.bridge.mm_bot_id,
        )

        self._store.open()

        # Validate tokens and discover DM channels for each configured user
        for user in self.config.users:
            info = await self.mm.validate_token(user.mm_token)
            if not info:
                logger.critical(
                    "DmBridge '%s': Token validation FAILED for %s",
                    self.bridge.name, user.telegram_name,
                )
                await self.mm.close()
                self._store.close()
                raise SystemExit(1)

            for attempt in range(3):
                channel = await self.mm.get_dm_channel(
                    user.mm_token, user.mm_user_id, self.bridge.mm_bot_id
                )
                if channel:
                    self._dm_to_user[channel] = user
                    logger.info(
                        "DmBridge '%s': channel discovered %s→%s: %s",
                        self.bridge.name, user.telegram_name, self.bridge.mm_bot_id, channel,
                    )
                    break
                await asyncio.sleep(2.0 * (attempt + 1))

        if not self._dm_to_user:
            logger.critical(
                "DmBridge '%s': Zero DM channels discovered — nothing to relay",
                self.bridge.name,
            )
            await self.mm.close()
            self._store.close()
            raise SystemExit(1)

        # Wire adapter callbacks (no command handler — DM mode is fixed-target)
        self.adapter.set_callbacks(
            on_message=self._handle_inbound_message,
            on_edit=self._handle_inbound_edit,
            on_reaction=self._handle_inbound_reaction,
            on_command=self._handle_command,
        )

        await self.adapter.start()

        # WebSocket using first user's token
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

        logger.info(
            "DmBridgeRelay '%s' active — %d channel(s)",
            self.bridge.name, len(self._dm_to_user),
        )

        self._state = "active"
        self._last_error = ""
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            self._running = False
            if self._ws:
                await self._ws.stop()
            await self.adapter.stop()
            await self.mm.close()
            self._store.close()

    # --- Commands (DM bridges are fixed-target; commands are no-ops) ---

    async def _handle_command(self, cmd: str, args: list[str], user_id) -> str | None:
        """Handle BridgeMost-local commands for dedicated DM bridges."""
        if cmd == "bridge":
            return (
                f"🌉 DM bridge *{self.bridge.name}*\n"
                "Usa `/bridge help` para ver los controles locales.\n"
                "Las demás slash commands se envían al bot Hermes conectado."
            )
        return f"ℹ️ DM bridge *{self.bridge.name}* — fixed target, no commands needed."

    # --- Inbound (TG → MM) ---

    async def _handle_inbound_message(self, msg: InboundMessage):
        """Process a message from TG → post to this bridge's MM bot DM."""
        user = self.config.get_user_by_tg_id(msg.user_id)
        if not user:
            return

        dm = next((ch for ch, u in self._dm_to_user.items() if u.mm_user_id == user.mm_user_id), None)
        if not dm:
            return

        self._stats["tg_to_mm"] += 1

        file_ids = []
        text = msg.text
        voice_prefix = ""

        if msg.file_path:
            fid = await self.mm.upload_file(user.mm_token, dm, msg.file_path, msg.file_name)
            if fid:
                file_ids.append(fid)

        if msg.is_voice and msg.file_path and self.whisper:
            try:
                transcript = await self.whisper.transcribe(msg.file_path)
                if transcript:
                    voice_prefix = f"🎤 {transcript}"
            except Exception as e:
                logger.error("DmBridge '%s' whisper error: %s", self.bridge.name, e)

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

        if msg.file_path:
            try:
                Path(msg.file_path).unlink(missing_ok=True)
            except Exception:
                pass

        if text or file_ids:
            result = await self._retry_mm_post(user, dm, text, file_ids or None)
            post_id = result.get("id")
            if post_id:
                self._mark_our_post(post_id)
                self._track_pair(msg.platform_msg_id, post_id)
                self.adapter.start_typing_loop(user.telegram_id)
                await self._schedule_placeholder(dm, user.telegram_id)

    async def _handle_inbound_edit(self, msg: InboundMessage):
        """Process an edit from TG → edit MM post."""
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
        """Process a reaction from TG → add/remove on MM."""
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

    # --- WebSocket handlers (MM → TG) ---

    async def _handle_ws_post(self, post: dict):
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return
        if post_id in self._our_post_ids:
            return

        user = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        raw_text = post.get("message", "")
        if self._should_suppress_mm_text(raw_text):
            return

        self.adapter.stop_typing_loop(user.telegram_id)

        text = raw_text
        sent_id = None
        if text:
            self._stats["mm_to_tg"] += 1
            sent_id = await self._present_visible_text(
                channel_id, user.telegram_id, post_id, text
            )
        else:
            await self._clear_pending_presentation(channel_id, user.telegram_id, delete_placeholder=True)

        file_ids_raw = post.get("file_ids")
        file_ids_list = file_ids_raw if isinstance(file_ids_raw, list) else []
        for fid in file_ids_list:
            try:
                await self._relay_mm_file(user, fid)
            except Exception as e:
                logger.error("DmBridge '%s' file relay error: %s", self.bridge.name, e)

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
        import asyncio
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return
        if post_id in self._our_post_ids:
            return

        user = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        new_text = post.get("message", "")
        if not new_text:
            return
        if self._should_suppress_mm_text(new_text):
            return

        platform_id = self._lookup_platform(post_id)
        if not platform_id:
            self.adapter.stop_typing_loop(user.telegram_id)
            await self._present_visible_text(channel_id, user.telegram_id, post_id, new_text)
            return

        # Debounce: buffer rapid edits (streaming bots) to avoid TG flood control
        self._edit_pending[post_id] = new_text
        prev_task = self._edit_debounce.get(post_id)
        if prev_task and not prev_task.done():
            prev_task.cancel()

        async def _flush_edit():
            await asyncio.sleep(self._edit_debounce_secs)
            text = self._edit_pending.pop(post_id, None)
            self._edit_debounce.pop(post_id, None)
            if text:
                await self.adapter.edit_message(user.telegram_id, platform_id, text)

        self._edit_debounce[post_id] = asyncio.ensure_future(_flush_edit())

    async def _handle_ws_delete(self, post: dict):
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return

        user = self._dm_to_user[channel_id]
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
        for ch_id, usr in self._dm_to_user.items():
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
        for ch_id, usr in self._dm_to_user.items():
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

        user = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

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
                logger.error(
                    "DmBridge '%s' MM post failed after %d retries: %s",
                    self.bridge.name, max_retries, last_error,
                )
                self._stats["errors"] += 1
                await self.adapter.send_message(
                    user.telegram_id,
                    OutboundMessage(text=f"⚠️ Mensaje no entregado: {last_error.get('message', 'error')}"),
                )
                return last_error
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
        return last_error
