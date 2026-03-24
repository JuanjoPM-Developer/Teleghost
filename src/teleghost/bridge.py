"""Core bridge logic — connects Telegram ↔ Mattermost."""

import asyncio
import logging
import tempfile
from collections import deque
from pathlib import Path

from telegram import Update, Message as TGMessage
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

from .config import Config, UserMapping
from .emoji import tg_emoji_to_mm, mm_emoji_to_tg
from .health import HealthServer
from .markdown import mm_to_telegram
from .mattermost import MattermostClient
from .websocket import MattermostWebSocket
from .whisper import WhisperClient

logger = logging.getLogger("teleghost.bridge")

# Telegram message length limit
TG_MAX_LENGTH = 4096


def split_message(text: str, max_len: int = TG_MAX_LENGTH) -> list[str]:
    """Split a long message into chunks that fit Telegram's limit.
    
    Tries to split on newlines first, then on spaces, then hard-cuts.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to find a good split point
        split_at = max_len
        
        # Prefer splitting at double newline (paragraph break)
        double_nl = remaining.rfind("\n\n", 0, max_len)
        if double_nl > max_len // 2:
            split_at = double_nl + 1
        else:
            # Try single newline
            single_nl = remaining.rfind("\n", 0, max_len)
            if single_nl > max_len // 2:
                split_at = single_nl + 1
            else:
                # Try space
                space = remaining.rfind(" ", 0, max_len)
                if space > max_len // 2:
                    split_at = space + 1

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    return chunks


class TeleGhostBridge:
    """Main bridge between Telegram and Mattermost."""

    def __init__(self, config: Config):
        self.config = config
        self.mm = MattermostClient(config.mm_url)
        self._last_post_ids: dict[str, str] = {}  # channel_id → last seen post_id
        self._our_post_ids: deque[str] = deque(maxlen=1000)
        self._running = False
        self._tg_bot = None
        self._ws: MattermostWebSocket | None = None
        # Track DM channels we're interested in → user mapping
        self._dm_to_user: dict[str, tuple[UserMapping, object]] = {}
        # Bidirectional message ID mapping for edit/delete sync
        self._tg_to_mm: dict[int, str] = {}   # TG message_id → MM post_id
        self._mm_to_tg: dict[str, int] = {}   # MM post_id → TG message_id
        self._map_maxlen = 2000
        # Synthetic typing: track pending bot responses per user
        self._typing_tasks: dict[int, asyncio.Task] = {}  # tg_user_id → typing task
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
            logger.info("Voice-to-text enabled: %s (model=%s)", config.whisper_url, config.whisper_model)

    async def start(self):
        """Start the bridge."""
        logger.info("TeleGhost v0.5.0 starting (WebSocket + multi-bot + startup resilience)...")

        # Phase 1: Pre-validate all user tokens before anything else
        for user in self.config.users:
            logger.info("Validating token for %s...", user.telegram_name)
            user_info = await self.mm.validate_token(user.mm_token)
            if not user_info:
                logger.critical(
                    "FATAL: Token validation FAILED for %s — check mm_token in config",
                    user.telegram_name,
                )
                raise SystemExit(1)
            logger.info(
                "Token OK for %s (MM user: %s)",
                user.telegram_name, user_info.get("username", "?"),
            )

        # Phase 2: Auto-discover DM channels with retry
        max_retries = 3
        retry_delay = 2.0

        for user in self.config.users:
            for bot in user.bots:
                if bot.mm_dm_channel:
                    # Pre-configured — validate format
                    if len(bot.mm_dm_channel) != 26 or not bot.mm_dm_channel.isalnum():
                        logger.error(
                            "Invalid pre-configured DM channel for %s→%s: %r — will re-discover",
                            user.telegram_name, bot.name, bot.mm_dm_channel,
                        )
                        bot.mm_dm_channel = ""

                if not bot.mm_dm_channel:
                    # Discover with retry
                    for attempt in range(1, max_retries + 1):
                        channel = await self.mm.get_dm_channel(
                            user.mm_token, user.mm_user_id, bot.mm_bot_id
                        )
                        if channel:
                            bot.mm_dm_channel = channel
                            logger.info(
                                "DM discovered for %s→%s: %s (attempt %d)",
                                user.telegram_name, bot.name, channel, attempt,
                            )
                            break
                        logger.warning(
                            "DM discovery failed for %s→%s (attempt %d/%d), retrying in %.0fs...",
                            user.telegram_name, bot.name, attempt, max_retries, retry_delay,
                        )
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 10.0)

                    if not bot.mm_dm_channel:
                        logger.error(
                            "FAILED to discover DM for %s→%s after %d attempts",
                            user.telegram_name, bot.name, max_retries,
                        )

                # Build reverse lookup: DM channel → (user, bot)
                if bot.mm_dm_channel:
                    self._dm_to_user[bot.mm_dm_channel] = (user, bot)

            # Legacy compatibility
            if not user.mm_dm_channel and user.mm_target_bot and not user.bots:
                channel = await self.mm.get_dm_channel(
                    user.mm_token, user.mm_user_id, user.mm_target_bot
                )
                if channel:
                    user.mm_dm_channel = channel

        # Phase 3: Abort if zero channels discovered
        if not self._dm_to_user:
            logger.critical(
                "FATAL: Zero DM channels discovered — bridge has nothing to relay. "
                "Check MM connectivity, tokens, and bot user IDs."
            )
            raise SystemExit(1)

        total_bots = sum(len(u.bots) for u in self.config.users)
        ok_channels = len(self._dm_to_user)
        if ok_channels < total_bots:
            logger.warning(
                "Partial discovery: %d/%d bot channels OK — some bots will be unreachable",
                ok_channels, total_bots,
            )
        else:
            logger.info("All %d bot channels discovered successfully", ok_channels)

        # Build Telegram application
        app = ApplicationBuilder().token(self.config.tg_bot_token).build()

        # FIX #2: Store bot reference for reuse in MM→TG relay
        self._tg_bot = app.bot

        # /bot command for switching active bot
        app.add_handler(CommandHandler("bot", self._handle_bot_command))

        # Handle ALL messages (text, photos, documents, etc.)
        app.add_handler(MessageHandler(
            filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            self._handle_telegram_message,
        ))

        # Handle edited messages
        app.add_handler(MessageHandler(
            filters.UpdateType.EDITED_MESSAGE,
            self._handle_telegram_edit,
        ))

        # Handle reactions (TG sends MessageReactionUpdated)
        from telegram.ext import MessageReactionHandler
        app.add_handler(MessageReactionHandler(self._handle_telegram_reaction))

        # Start TG polling in background
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=0.5,
        )

        # Start health endpoint
        await self.health.start()

        # Derive WebSocket URL from MM URL (http→ws, https→wss)
        ws_url = self.config.mm_url.replace("https://", "wss://").replace("http://", "ws://")

        # Use user's personal token for WS (bot tokens get rejected on WS connect)
        ws_token = self.config.users[0].mm_token if self.config.users else self.config.mm_bot_token

        # Start WebSocket listener (replaces polling)
        self._ws = MattermostWebSocket(
            ws_url=ws_url,
            token=ws_token,
            on_post=self._handle_ws_post,
            on_post_edited=self._handle_ws_edit,
            on_post_deleted=self._handle_ws_delete,
            on_reaction_added=self._handle_ws_reaction_added,
            on_reaction_removed=self._handle_ws_reaction_removed,
            on_typing=self._handle_ws_typing,
        )
        await self._ws.start()

        logger.info("TeleGhost bridge active — WebSocket + Telegram listening")

        # Keep running until interrupted
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            self._running = False
            if self._ws:
                await self._ws.stop()
            await self.health.stop()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            await self.mm.close()

    def _link_messages(self, tg_msg_id: int, mm_post_id: str):
        """Track a TG↔MM message pair for edit/delete sync."""
        self._tg_to_mm[tg_msg_id] = mm_post_id
        self._mm_to_tg[mm_post_id] = tg_msg_id
        # Evict oldest if over limit
        while len(self._tg_to_mm) > self._map_maxlen:
            oldest_tg = next(iter(self._tg_to_mm))
            old_mm = self._tg_to_mm.pop(oldest_tg)
            self._mm_to_tg.pop(old_mm, None)

    def _get_active_bot(self, user: UserMapping):
        """Get the active bot route for a user."""
        for bot in user.bots:
            if bot.name == user.active_bot:
                return bot
        return user.bots[0] if user.bots else None

    async def _typing_loop(self, tg_user_id: int):
        """Send 'typing' chat action every 4s until cancelled.

        Telegram typing indicator lasts ~5s, so 4s refresh keeps it alive.
        This runs after we post a user message to MM, and stops when the
        bot's response arrives via WebSocket.
        """
        try:
            while True:
                await self._tg_bot.send_chat_action(
                    chat_id=tg_user_id,
                    action="typing",
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing loop error for %d: %s", tg_user_id, e)

    def _start_typing(self, user: UserMapping):
        """Start synthetic typing indicator for a user."""
        # Cancel any existing typing task
        self._stop_typing(user)
        task = asyncio.create_task(self._typing_loop(user.telegram_id))
        self._typing_tasks[user.telegram_id] = task

    def _stop_typing(self, user: UserMapping):
        """Stop synthetic typing indicator for a user."""
        task = self._typing_tasks.pop(user.telegram_id, None)
        if task and not task.done():
            task.cancel()

    async def _handle_bot_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle /bot command to list or switch active bot."""
        if not update.effective_user or not update.effective_message:
            return

        tg_id = update.effective_user.id
        user = self.config.get_user_by_tg_id(tg_id)
        if not user:
            return

        args = context.args or []

        if not args:
            # List available bots
            lines = ["🤖 *Bots disponibles:*\n"]
            for bot in user.bots:
                marker = "→ " if bot.name == user.active_bot else "  "
                lines.append(f"{marker}`{bot.name}`")
            lines.append(f"\nActivo: *{user.active_bot}*")
            lines.append("Usa `/bot nombre` para cambiar")
            await update.effective_message.reply_text(
                "\n".join(lines), parse_mode="Markdown"
            )
        else:
            target = args[0].lower()
            matched = None
            for bot in user.bots:
                if bot.name.lower() == target:
                    matched = bot
                    break

            if matched:
                # Stop any pending typing from previous bot
                self._stop_typing(user)
                user.active_bot = matched.name
                await update.effective_message.reply_text(
                    f"── Ahora hablando con *{matched.name}* ──",
                    parse_mode="Markdown",
                )
                logger.info("Bot switched to %s for %s", matched.name, user.telegram_name)
            else:
                names = ", ".join(b.name for b in user.bots)
                await update.effective_message.reply_text(
                    f"❌ Bot '{target}' no encontrado. Disponibles: {names}"
                )

    async def _handle_telegram_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle incoming Telegram message → post to MM as user."""
        msg = update.effective_message
        if not msg or not update.effective_user:
            return

        tg_id = update.effective_user.id
        user = self.config.get_user_by_tg_id(tg_id)

        if not user:
            logger.warning("Unknown TG user %d, ignoring", tg_id)
            return

        # Get active bot's DM channel
        active_bot = self._get_active_bot(user)
        if not active_bot or not active_bot.mm_dm_channel:
            logger.error("No active bot/DM channel for %s", user.telegram_name)
            return

        # Use active bot's DM channel
        dm_channel = active_bot.mm_dm_channel

        logger.info("TG→MM [%s→%s]: %s", user.telegram_name, active_bot.name, (msg.text or "<media>")[:80])
        self.health.record_tg_to_mm()

        file_ids = []

        # Handle media (photo, document, audio, video, voice, sticker)
        local_file = None
        try:
            if msg.photo:
                photo = msg.photo[-1]
                tg_file = await context.bot.get_file(photo.file_id)
                local_file = tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False
                )
                await tg_file.download_to_drive(local_file.name)
                fid = await self.mm.upload_file(
                    user.mm_token, dm_channel,
                    local_file.name, f"photo_{photo.file_unique_id}.jpg"
                )
                if fid:
                    file_ids.append(fid)

            elif msg.document:
                tg_file = await context.bot.get_file(msg.document.file_id)
                fname = msg.document.file_name or "file"
                local_file = tempfile.NamedTemporaryFile(
                    suffix=Path(fname).suffix or ".bin", delete=False
                )
                await tg_file.download_to_drive(local_file.name)
                fid = await self.mm.upload_file(
                    user.mm_token, dm_channel,
                    local_file.name, fname
                )
                if fid:
                    file_ids.append(fid)

            elif msg.audio or msg.voice:
                audio = msg.audio or msg.voice
                tg_file = await context.bot.get_file(audio.file_id)
                suffix = ".ogg" if msg.voice else ".mp3"
                local_file = tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False
                )
                await tg_file.download_to_drive(local_file.name)
                fname = getattr(audio, "file_name", None) or f"audio{suffix}"

                # Voice-to-text: transcribe if Whisper is configured
                if self.whisper and msg.voice:
                    transcript = await self.whisper.transcribe(local_file.name)
                    if transcript:
                        # Prepend transcription to message text
                        voice_text = f"🎤 {transcript}"
                        text = f"{voice_text}\n{text}" if text else voice_text
                        logger.info("Voice transcribed: %s", transcript[:80])

                # Upload audio file (always if keep_audio, or if no transcript)
                if self.config.whisper_keep_audio or not self.whisper or not msg.voice:
                    fid = await self.mm.upload_file(
                        user.mm_token, dm_channel,
                        local_file.name, fname
                    )
                    if fid:
                        file_ids.append(fid)
                elif self.whisper and msg.voice and not self.config.whisper_keep_audio:
                    # Whisper active + keep_audio=false: skip audio upload
                    pass

            elif msg.video or msg.video_note:
                video = msg.video or msg.video_note
                tg_file = await context.bot.get_file(video.file_id)
                local_file = tempfile.NamedTemporaryFile(
                    suffix=".mp4", delete=False
                )
                await tg_file.download_to_drive(local_file.name)
                fname = getattr(video, "file_name", None) or "video.mp4"
                fid = await self.mm.upload_file(
                    user.mm_token, dm_channel,
                    local_file.name, fname
                )
                if fid:
                    file_ids.append(fid)

        finally:
            if local_file:
                try:
                    Path(local_file.name).unlink(missing_ok=True)
                except Exception:
                    pass

        # Build message text
        text = msg.text or msg.caption or ""

        # Post to MM as the real user
        if text or file_ids:
            result = await self._retry_mm_post(user, dm_channel, text, file_ids or None)
            post_id = result.get("id")
            if post_id:
                self._our_post_ids.append(post_id)
                # Track TG↔MM message pair for edit/delete sync
                if msg.message_id:
                    self._link_messages(msg.message_id, post_id)
                # Start synthetic typing — bot is now processing
                self._start_typing(user)

    async def _handle_ws_post(self, post: dict):
        """Handle a new post event from Mattermost WebSocket."""
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        # Only process posts in tracked DM channels
        if channel_id not in self._dm_to_user:
            return

        # Skip our own posts (echo prevention)
        if post_id in self._our_post_ids:
            return

        user, bot = self._dm_to_user[channel_id]

        # Skip posts from the mapped user (their own messages)
        if user_id == user.mm_user_id:
            return

        # Bot responded — stop synthetic typing
        self._stop_typing(user)

        # This is a bot response — relay to Telegram
        text = post.get("message", "")

        # Always add bot prefix for multi-bot clarity
        if text and len(user.bots) > 1:
            text = f"🤖 {bot.name}: {text}"

        if text:
            logger.info("WS→TG [%s←%s]: %s", user.telegram_name, bot.name, text[:80])
            self.health.record_mm_to_tg()

            chunks = split_message(text)
            for i, chunk in enumerate(chunks):
                try:
                    tg_text = mm_to_telegram(chunk)
                    try:
                        sent = await self._tg_bot.send_message(
                            chat_id=user.telegram_id,
                            text=tg_text,
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        sent = await self._tg_bot.send_message(
                            chat_id=user.telegram_id,
                            text=chunk,
                            parse_mode=None,
                        )
                    # Track first chunk for edit/delete sync
                    if i == 0 and sent and post_id:
                        self._link_messages(sent.message_id, post_id)
                except Exception as e:
                    logger.error("TG send error: %s", e)

        # Handle file attachments
        file_ids_list = post.get("file_ids") or []
        for fid in file_ids_list:
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                    dl_path = await self.mm.download_file(
                        self.config.mm_bot_token, fid, tmp.name
                    )
                    if dl_path:
                        with open(dl_path, "rb") as doc:
                            await self._tg_bot.send_document(
                                chat_id=user.telegram_id,
                                document=doc,
                            )
                    Path(tmp.name).unlink(missing_ok=True)
            except Exception as e:
                logger.error("TG file send error: %s", e)

    async def _handle_telegram_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle edited Telegram message → edit corresponding MM post."""
        msg = update.edited_message
        if not msg or not update.effective_user:
            return

        tg_id = update.effective_user.id
        user = self.config.get_user_by_tg_id(tg_id)
        if not user:
            return

        mm_post_id = self._tg_to_mm.get(msg.message_id)
        if not mm_post_id:
            logger.debug("TG edit for unmapped msg %d, ignoring", msg.message_id)
            return

        new_text = msg.text or msg.caption or ""
        if not new_text:
            return

        logger.info("TG→MM edit [%s]: msg %d → post %s", user.telegram_name, msg.message_id, mm_post_id[:8])
        result = await self.mm.edit_post(user.mm_token, mm_post_id, new_text)
        if result.get("id"):
            self._our_post_ids.append(result["id"])  # Prevent echo of edit event

    async def _handle_ws_edit(self, post: dict):
        """Handle an edited post event from Mattermost WebSocket."""
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return

        # Skip edits we triggered ourselves
        if post_id in self._our_post_ids:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        tg_msg_id = self._mm_to_tg.get(post_id)
        if not tg_msg_id:
            logger.debug("MM edit for unmapped post %s, ignoring", post_id[:8])
            return

        new_text = post.get("message", "")
        if not new_text:
            return

        logger.info("WS→TG edit [%s←%s]: post %s → msg %d", user.telegram_name, bot.name, post_id[:8], tg_msg_id)
        try:
            tg_text = mm_to_telegram(new_text)
            try:
                await self._tg_bot.edit_message_text(
                    chat_id=user.telegram_id,
                    message_id=tg_msg_id,
                    text=tg_text,
                    parse_mode="MarkdownV2",
                )
            except Exception:
                await self._tg_bot.edit_message_text(
                    chat_id=user.telegram_id,
                    message_id=tg_msg_id,
                    text=new_text,
                )
        except Exception as e:
            logger.error("TG edit_message error: %s", e)

    async def _handle_ws_delete(self, post: dict):
        """Handle a deleted post event from Mattermost WebSocket."""
        channel_id = post.get("channel_id", "")
        post_id = post.get("id", "")
        user_id = post.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return

        user, bot = self._dm_to_user[channel_id]
        if user_id == user.mm_user_id:
            return

        tg_msg_id = self._mm_to_tg.get(post_id)
        if not tg_msg_id:
            return

        logger.info("WS→TG delete [%s←%s]: post %s → msg %d", user.telegram_name, bot.name, post_id[:8], tg_msg_id)
        try:
            await self._tg_bot.delete_message(
                chat_id=user.telegram_id,
                message_id=tg_msg_id,
            )
        except Exception as e:
            logger.error("TG delete_message error: %s", e)

        # Cleanup mapping
        self._mm_to_tg.pop(post_id, None)
        self._tg_to_mm.pop(tg_msg_id, None)

    async def _handle_telegram_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """Handle TG reaction → add/remove reaction on MM post."""
        reaction_update = update.message_reaction
        if not reaction_update:
            return

        tg_user_id = reaction_update.user.id if reaction_update.user else None
        if not tg_user_id:
            return

        user = self.config.get_user_by_tg_id(tg_user_id)
        if not user:
            return

        msg_id = reaction_update.message_id
        mm_post_id = self._tg_to_mm.get(msg_id)
        if not mm_post_id:
            logger.debug("TG reaction on unmapped msg %d, ignoring", msg_id)
            return

        # Determine added/removed reactions by comparing old vs new
        old_emojis = set()
        for r in (reaction_update.old_reaction or []):
            if hasattr(r, "emoji") and r.emoji:
                old_emojis.add(r.emoji)

        new_emojis = set()
        for r in (reaction_update.new_reaction or []):
            if hasattr(r, "emoji") and r.emoji:
                new_emojis.add(r.emoji)

        added = new_emojis - old_emojis
        removed = old_emojis - new_emojis

        for emoji in added:
            mm_name = tg_emoji_to_mm(emoji)
            if mm_name:
                logger.info("TG→MM reaction [%s]: %s on %s", user.telegram_name, mm_name, mm_post_id[:8])
                await self.mm.add_reaction(user.mm_token, user.mm_user_id, mm_post_id, mm_name)

        for emoji in removed:
            mm_name = tg_emoji_to_mm(emoji)
            if mm_name:
                logger.info("TG→MM unreaction [%s]: %s on %s", user.telegram_name, mm_name, mm_post_id[:8])
                await self.mm.remove_reaction(user.mm_token, user.mm_user_id, mm_post_id, mm_name)

    async def _handle_ws_reaction_added(self, reaction: dict):
        """Handle MM reaction_added → set reaction on TG message."""
        post_id = reaction.get("post_id", "")
        user_id = reaction.get("user_id", "")
        emoji_name = reaction.get("emoji_name", "")

        if not post_id or not emoji_name:
            return

        tg_msg_id = self._mm_to_tg.get(post_id)
        if not tg_msg_id:
            return

        # Find user by checking all DM channels for the post
        target_user = None
        for ch_id, (usr, bot) in self._dm_to_user.items():
            if user_id != usr.mm_user_id:
                target_user = usr
                break

        if not target_user:
            return

        tg_emoji = mm_emoji_to_tg(emoji_name)
        if not tg_emoji:
            logger.debug("No TG emoji for MM :%s:, skipping", emoji_name)
            return

        logger.info("WS→TG reaction [%s]: %s on msg %d", target_user.telegram_name, tg_emoji, tg_msg_id)
        try:
            await self._tg_bot.set_message_reaction(
                chat_id=target_user.telegram_id,
                message_id=tg_msg_id,
                reaction=[{"type": "emoji", "emoji": tg_emoji}],
            )
        except Exception as e:
            logger.error("TG set_reaction error: %s", e)

    async def _handle_ws_reaction_removed(self, reaction: dict):
        """Handle MM reaction_removed → clear reaction on TG message."""
        post_id = reaction.get("post_id", "")
        user_id = reaction.get("user_id", "")

        if not post_id:
            return

        tg_msg_id = self._mm_to_tg.get(post_id)
        if not tg_msg_id:
            return

        target_user = None
        for ch_id, (usr, bot) in self._dm_to_user.items():
            if user_id != usr.mm_user_id:
                target_user = usr
                break

        if not target_user:
            return

        logger.info("WS→TG unreaction [%s]: on msg %d", target_user.telegram_name, tg_msg_id)
        try:
            # Empty reaction list clears all reactions
            await self._tg_bot.set_message_reaction(
                chat_id=target_user.telegram_id,
                message_id=tg_msg_id,
                reaction=[],
            )
        except Exception as e:
            logger.error("TG clear_reaction error: %s", e)

    async def _handle_ws_typing(self, typing_info: dict):
        """Handle MM typing event → extend synthetic typing.

        Most OpenClaw bots don't emit typing events, so this serves as
        a supplementary signal. The main typing comes from _typing_loop
        started when we post the user's message.
        """
        channel_id = typing_info.get("channel_id", "")
        user_id = typing_info.get("user_id", "")

        if channel_id not in self._dm_to_user:
            return

        user, bot = self._dm_to_user[channel_id]

        # Only relay typing from bots, not from the user themselves
        if user_id == user.mm_user_id:
            return

        # If we don't already have a synthetic typing loop running, start one
        if user.telegram_id not in self._typing_tasks or self._typing_tasks[user.telegram_id].done():
            self._start_typing(user)
            logger.debug("WS typing extended [%s←%s]", user.telegram_name, bot.name)

    async def _retry_mm_post(
        self, user: UserMapping, channel_id: str, text: str,
        file_ids: list[str] | None, max_retries: int = 3
    ) -> dict:
        """Post to MM with exponential backoff retry."""
        delay = 1.0
        last_error = {}
        for attempt in range(max_retries):
            result = await self.mm.post_message(
                user.mm_token, channel_id, text, file_ids
            )
            if result.get("id"):
                return result
            last_error = result
            
            # FIX #10: Notify user on persistent failure (last attempt)
            if attempt == max_retries - 1:
                logger.error(
                    "MM post failed after %d retries for %s: %s",
                    max_retries, user.telegram_name, last_error
                )
                try:
                    if self._tg_bot:
                        await self._tg_bot.send_message(
                            chat_id=user.telegram_id,
                            text=f"⚠️ Mensaje no entregado a Mattermost: {last_error.get('message', 'error desconocido')}",
                        )
                except Exception as e:
                    logger.error("Could not notify user of failure: %s", e)
                return last_error

            logger.warning(
                "MM post attempt %d/%d failed, retrying in %.1fs...",
                attempt + 1, max_retries, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)

        return last_error

    # Legacy polling loop removed in v0.1.0 — replaced by WebSocket
