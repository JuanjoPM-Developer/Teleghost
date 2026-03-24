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

from .config import Config, UserMapping
from .health import HealthServer
from .markdown import mm_to_telegram
from .mattermost import MattermostClient
from .websocket import MattermostWebSocket

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
        # Health server
        self.health = HealthServer(port=config.health_port)

    async def start(self):
        """Start the bridge."""
        logger.info("TeleGhost v0.1.0 starting (WebSocket mode)...")

        # Auto-discover DM channels for all bot routes
        for user in self.config.users:
            for bot in user.bots:
                if not bot.mm_dm_channel:
                    channel = await self.mm.get_dm_channel(
                        user.mm_token, user.mm_user_id, bot.mm_bot_id
                    )
                    if channel:
                        bot.mm_dm_channel = channel
                        logger.info(
                            "Auto-discovered DM for %s→%s: %s",
                            user.telegram_name, bot.name, channel
                        )
                    else:
                        logger.error(
                            "Could not discover DM for %s→%s",
                            user.telegram_name, bot.name
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

    def _get_active_bot(self, user: UserMapping):
        """Get the active bot route for a user."""
        for bot in user.bots:
            if bot.name == user.active_bot:
                return bot
        return user.bots[0] if user.bots else None

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
                user.active_bot = matched.name
                await update.effective_message.reply_text(
                    f"✅ Bot cambiado a *{matched.name}*", parse_mode="Markdown"
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
                fid = await self.mm.upload_file(
                    user.mm_token, dm_channel,
                    local_file.name, fname
                )
                if fid:
                    file_ids.append(fid)

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

        # This is a bot response — relay to Telegram
        text = post.get("message", "")

        # Add bot prefix if not the active bot (multi-bot clarity)
        if text and len(user.bots) > 1 and bot.name != user.active_bot:
            text = f"[{bot.name}] {text}"

        if text:
            logger.info("WS→TG [%s←%s]: %s", user.telegram_name, bot.name, text[:80])
            self.health.record_mm_to_tg()

            chunks = split_message(text)
            for chunk in chunks:
                try:
                    tg_text = mm_to_telegram(chunk)
                    try:
                        await self._tg_bot.send_message(
                            chat_id=user.telegram_id,
                            text=tg_text,
                            parse_mode="MarkdownV2",
                        )
                    except Exception:
                        await self._tg_bot.send_message(
                            chat_id=user.telegram_id,
                            text=chunk,
                            parse_mode=None,
                        )
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
