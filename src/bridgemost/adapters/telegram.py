"""Telegram adapter for BridgeMost.

Handles all Telegram-specific logic: bot polling, message sending,
media upload/download, reactions, typing indicators, and slash-command passthrough.
"""

import asyncio
import logging
import re
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    MessageReactionHandler,
    filters,
    ContextTypes,
)

from .base import BaseAdapter, InboundMessage, OutboundMessage

logger = logging.getLogger("bridgemost.adapter.telegram")

# Telegram message length limit
TG_MAX_LENGTH = 4096

# BridgeMost local commands live under their own namespace so generic
# slash commands can pass through transparently to Hermes upstream.
BRIDGEMOST_CONTROL_COMMAND = "bridge"


def split_message(text: str, max_len: int = TG_MAX_LENGTH) -> list[str]:
    """Split a long message into chunks that fit Telegram's limit."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = max_len
        double_nl = remaining.rfind("\n\n", 0, max_len)
        if double_nl > max_len // 2:
            split_at = double_nl + 1
        else:
            single_nl = remaining.rfind("\n", 0, max_len)
            if single_nl > max_len // 2:
                split_at = single_nl + 1
            else:
                space = remaining.rfind(" ", 0, max_len)
                if space > max_len // 2:
                    split_at = space + 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


class TelegramAdapter(BaseAdapter):
    """Telegram adapter using python-telegram-bot."""

    def __init__(self, bot_token: str, allowed_user_ids: list[int] | None = None):
        self._bot_token = bot_token
        self._allowed_users = set(allowed_user_ids) if allowed_user_ids else None
        self._app = None
        self._bot = None
        # Typing tasks per user
        self._typing_tasks: dict[int, asyncio.Task] = {}
        # Streaming edit tasks per (user, message)
        self._stream_tasks: dict[tuple[int, int], asyncio.Task] = {}
        # Rate limiter: max 25 msgs/sec
        from collections import deque
        import time
        self._send_times: deque[float] = deque(maxlen=25)
        self._rate_limit = 25

    async def start(self) -> None:
        """Start Telegram bot polling."""
        self._app = ApplicationBuilder().token(self._bot_token).build()
        self._bot = self._app.bot

        # Local BridgeMost commands
        self._app.add_handler(CommandHandler(BRIDGEMOST_CONTROL_COMMAND, self._cmd_bridge))
        self._app.add_handler(CommandHandler("bot", self._cmd_bot))
        self._app.add_handler(CommandHandler("bots", self._cmd_bots))

        # Unknown slash commands must pass through to Mattermost/Hermes.
        self._app.add_handler(MessageHandler(
            filters.COMMAND & ~filters.StatusUpdate.ALL,
            self._on_tg_passthrough_command,
        ))

        # Messages
        self._app.add_handler(MessageHandler(
            filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL,
            self._on_tg_message,
        ))

        # Edits
        self._app.add_handler(MessageHandler(
            filters.UpdateType.EDITED_MESSAGE,
            self._on_tg_edit,
        ))

        # Reactions
        self._app.add_handler(MessageReactionHandler(self._on_tg_reaction))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True, poll_interval=0.5)
        logger.info("Telegram adapter started")

    async def stop(self) -> None:
        """Stop Telegram bot."""
        # Cancel all typing tasks
        for task in self._typing_tasks.values():
            if not task.done():
                task.cancel()
        self._typing_tasks.clear()

        for task in self._stream_tasks.values():
            if not task.done():
                task.cancel()
        self._stream_tasks.clear()

        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        logger.info("Telegram adapter stopped")

    def _is_allowed(self, tg_user_id: int) -> bool:
        if not self._allowed_users:
            return True
        return tg_user_id in self._allowed_users

    @staticmethod
    def _is_secure_update(update: Update, msg) -> bool:
        """Reject Telegram updates that violate the owner-only DM security model."""
        chat = getattr(update, "effective_chat", None)
        if chat and getattr(chat, "type", None) != "private":
            return False
        if getattr(msg, "sender_chat", None):
            return False
        if getattr(msg, "forward_origin", None):
            return False
        if getattr(msg, "forward_from", None) or getattr(msg, "forward_from_chat", None):
            return False
        if getattr(msg, "forward_date", None):
            return False
        return True

    @staticmethod
    def _reply_kwargs(msg: OutboundMessage) -> dict:
        reply_to = getattr(msg, "reply_to_platform_msg_id", None)
        if reply_to is None:
            return {}
        try:
            reply_to = int(reply_to)
        except (TypeError, ValueError):
            pass
        return {"reply_to_message_id": reply_to}

    async def _rate_wait(self):
        import time
        now = time.monotonic()
        if len(self._send_times) >= self._rate_limit:
            oldest = self._send_times[0]
            elapsed = now - oldest
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed + 0.05)
        self._send_times.append(time.monotonic())

    # --- Inbound handlers ---

    async def _on_tg_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming TG message → convert to InboundMessage → call core."""
        msg = update.effective_message
        if not msg or not update.effective_user:
            return
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._is_secure_update(update, msg):
            return
        if not self._on_message:
            return

        inbound = InboundMessage(
            platform_msg_id=msg.message_id,
            user_id=update.effective_user.id,
            reply_to_msg_id=getattr(getattr(msg, "reply_to_message", None), "message_id", None),
        )

        # Download media to temp file
        local_file = None
        try:
            if msg.photo:
                photo = msg.photo[-1]
                tg_file = await context.bot.get_file(photo.file_id)
                local_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                await tg_file.download_to_drive(local_file.name)
                inbound.file_path = local_file.name
                inbound.file_name = f"photo_{photo.file_unique_id}.jpg"
                inbound.file_mime = "image/jpeg"

            elif msg.document:
                tg_file = await context.bot.get_file(msg.document.file_id)
                fname = msg.document.file_name or "file"
                local_file = tempfile.NamedTemporaryFile(suffix=Path(fname).suffix or ".bin", delete=False)
                await tg_file.download_to_drive(local_file.name)
                inbound.file_path = local_file.name
                inbound.file_name = fname
                inbound.file_mime = msg.document.mime_type or ""

            elif msg.audio or msg.voice:
                audio = msg.audio or msg.voice
                tg_file = await context.bot.get_file(audio.file_id)
                suffix = ".ogg" if msg.voice else ".mp3"
                local_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                await tg_file.download_to_drive(local_file.name)
                inbound.file_path = local_file.name
                inbound.file_name = getattr(audio, "file_name", None) or f"audio{suffix}"
                inbound.file_mime = "audio/ogg" if msg.voice else "audio/mpeg"
                inbound.is_voice = bool(msg.voice)

            elif msg.video or msg.video_note:
                video = msg.video or msg.video_note
                tg_file = await context.bot.get_file(video.file_id)
                local_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                await tg_file.download_to_drive(local_file.name)
                inbound.file_path = local_file.name
                inbound.file_name = getattr(video, "file_name", None) or "video.mp4"
                inbound.file_mime = "video/mp4"

            elif msg.sticker:
                sticker = msg.sticker
                inbound.sticker_emoji = sticker.emoji or ""
                try:
                    tg_file = await context.bot.get_file(sticker.file_id)
                    if sticker.is_animated:
                        suffix, fname = ".tgs", "sticker.tgs"
                    elif sticker.is_video:
                        suffix, fname = ".webm", "sticker.webm"
                    else:
                        suffix, fname = ".webp", "sticker.webp"
                    local_file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                    await tg_file.download_to_drive(local_file.name)
                    inbound.file_path = local_file.name
                    inbound.file_name = fname
                except Exception:
                    pass  # Fallback: emoji only

            elif msg.venue:
                inbound.location = (msg.venue.location.latitude, msg.venue.location.longitude)
                inbound.venue_name = msg.venue.title or ""
                inbound.venue_address = msg.venue.address or ""

            elif msg.location:
                inbound.location = (msg.location.latitude, msg.location.longitude)

            elif msg.poll:
                inbound.poll_question = msg.poll.question
                inbound.poll_options = [opt.text for opt in msg.poll.options]
                inbound.poll_anonymous = msg.poll.is_anonymous
                inbound.poll_multiple = msg.poll.allows_multiple_answers

        except Exception as e:
            logger.error("TG media download error: %s", e)

        inbound.text = msg.text or msg.caption or ""
        await self._on_message(inbound)

    @staticmethod
    def _normalize_command_text(text: str) -> str:
        """Normalize Telegram command syntax before forwarding upstream."""
        if not text or not text.startswith("/"):
            return text
        parts = text.split(maxsplit=1)
        command = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if "@" in command:
            command = command.split("@", 1)[0]
        return f"{command} {rest}".strip()

    async def _on_tg_passthrough_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Forward unknown slash commands verbatim to Mattermost/Hermes."""
        msg = update.effective_message
        if not msg or not update.effective_user:
            return
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._is_secure_update(update, msg):
            return
        if not self._on_message:
            return

        text = self._normalize_command_text(msg.text or msg.caption or "")
        if not text:
            return

        command_name = text.split(maxsplit=1)[0].lstrip("/").lower()
        if command_name in {BRIDGEMOST_CONTROL_COMMAND, "bot", "bots"}:
            return

        inbound = InboundMessage(
            platform_msg_id=msg.message_id,
            user_id=update.effective_user.id,
            text=text,
        )
        await self._on_message(inbound)

    async def _on_tg_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle edited TG message."""
        msg = update.edited_message
        if not msg or not update.effective_user:
            return
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._is_secure_update(update, msg):
            return
        if not self._on_edit:
            return

        inbound = InboundMessage(
            platform_msg_id=msg.message_id,
            user_id=update.effective_user.id,
            text=msg.text or msg.caption or "",
            is_edit=True,
        )
        await self._on_edit(inbound)

    async def _on_tg_reaction(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle TG reaction changes."""
        reaction_update = update.message_reaction
        if not reaction_update or not reaction_update.user:
            return
        if not self._is_allowed(reaction_update.user.id):
            return
        if not self._on_reaction:
            return

        old_emojis = set()
        for r in (reaction_update.old_reaction or []):
            if hasattr(r, "emoji") and r.emoji:
                old_emojis.add(r.emoji)

        new_emojis = set()
        for r in (reaction_update.new_reaction or []):
            if hasattr(r, "emoji") and r.emoji:
                new_emojis.add(r.emoji)

        inbound = InboundMessage(
            platform_msg_id=0,
            user_id=reaction_update.user.id,
            reaction_added=list(new_emojis - old_emojis) or None,
            reaction_removed=list(old_emojis - new_emojis) or None,
            reaction_msg_id=reaction_update.message_id,
        )
        await self._on_reaction(inbound)

    # --- Commands ---

    @staticmethod
    def _bridge_help_text() -> str:
        return (
            "🌉 *BridgeMost*\n\n"
            "Comandos locales del bridge:\n"
            "- `/bridge bot` — listar bot activo y disponibles\n"
            "- `/bridge bot <nombre>` — cambiar el bot activo\n"
            "- `/bridge bots` — ver estado de bots\n"
            "- `/bridge status` — ver estado local del bridge\n\n"
            "Todas las demás slash commands (`/new`, `/model`, `/help`, `/status`, ...) "
            "se reenvían a Hermes."
        )

    async def _cmd_bridge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._on_command or not update.effective_user or not update.effective_message:
            return
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._is_secure_update(update, update.effective_message):
            return

        args = context.args or []
        if not args:
            reply = self._bridge_help_text()
        else:
            subcmd = args[0].lower()
            subargs = args[1:]
            if subcmd in {"bot", "bots", "status"}:
                reply = await self._on_command(subcmd, subargs, update.effective_user.id)
            elif subcmd in {"help", "ayuda"}:
                reply = self._bridge_help_text()
            else:
                reply = (
                    f"❌ Subcomando desconocido: `{subcmd}`\n\n"
                    f"{self._bridge_help_text()}"
                )

        if reply:
            await update.effective_message.reply_text(reply, parse_mode="Markdown")

    async def _cmd_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._on_command or not update.effective_user or not update.effective_message:
            return
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._is_secure_update(update, update.effective_message):
            return
        reply = await self._on_command("bot", context.args or [], update.effective_user.id)
        if reply:
            await update.effective_message.reply_text(reply, parse_mode="Markdown")

    async def _cmd_bots(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._on_command or not update.effective_user or not update.effective_message:
            return
        if not self._is_allowed(update.effective_user.id):
            return
        if not self._is_secure_update(update, update.effective_message):
            return
        reply = await self._on_command("bots", [], update.effective_user.id)
        if reply:
            await update.effective_message.reply_text(reply, parse_mode="Markdown")

    # --- Outbound methods (called by core) ---

    async def send_message(self, user_id, msg: OutboundMessage) -> int | None:
        """Send message/file to TG user. Returns TG message_id."""
        if not self._bot:
            return None

        sent_id = None
        reply_kwargs = self._reply_kwargs(msg)

        # Handle file sending with smart dispatch
        if msg.file_path:
            await self._rate_wait()
            try:
                mime = msg.file_mime or ""
                ext = Path(msg.file_name).suffix.lower() if msg.file_name else ""

                if mime.startswith("image/"):
                    if mime == "image/gif" or ext == ".gif":
                        with open(msg.file_path, "rb") as f:
                            sent = await self._bot.send_animation(
                                chat_id=user_id,
                                animation=f,
                                filename=msg.file_name,
                                **reply_kwargs,
                            )
                    elif msg.file_size <= 10*1024*1024 and ext in (".jpg", ".jpeg", ".png", ".webp"):
                        with open(msg.file_path, "rb") as f:
                            sent = await self._bot.send_photo(
                                chat_id=user_id,
                                photo=f,
                                filename=msg.file_name,
                                **reply_kwargs,
                            )
                    else:
                        with open(msg.file_path, "rb") as f:
                            sent = await self._bot.send_document(
                                chat_id=user_id,
                                document=f,
                                filename=msg.file_name,
                                **reply_kwargs,
                            )
                elif mime.startswith("audio/"):
                    if ext == ".ogg" or mime == "audio/ogg":
                        with open(msg.file_path, "rb") as f:
                            sent = await self._bot.send_voice(
                                chat_id=user_id,
                                voice=f,
                                filename=msg.file_name,
                                **reply_kwargs,
                            )
                    else:
                        with open(msg.file_path, "rb") as f:
                            sent = await self._bot.send_audio(
                                chat_id=user_id,
                                audio=f,
                                filename=msg.file_name,
                                **reply_kwargs,
                            )
                elif mime.startswith("video/"):
                    with open(msg.file_path, "rb") as f:
                        sent = await self._bot.send_video(
                            chat_id=user_id,
                            video=f,
                            filename=msg.file_name,
                            **reply_kwargs,
                        )
                else:
                    with open(msg.file_path, "rb") as f:
                        sent = await self._bot.send_document(
                            chat_id=user_id,
                            document=f,
                            filename=msg.file_name,
                            **reply_kwargs,
                        )

                sent_id = sent.message_id if sent else None
            except Exception as e:
                logger.error("TG file send error: %s", e)

        # Handle text
        if msg.text:
            from ..markdown import mm_to_telegram

            chunks = split_message(msg.text)
            for i, chunk in enumerate(chunks):
                await self._rate_wait()
                try:
                    tg_text = mm_to_telegram(chunk)
                    chunk_reply_kwargs = reply_kwargs if i == 0 and sent_id is None else {}
                    try:
                        sent = await self._bot.send_message(
                            chat_id=user_id,
                            text=tg_text,
                            parse_mode="MarkdownV2",
                            **chunk_reply_kwargs,
                        )
                    except Exception:
                        sent = await self._bot.send_message(
                            chat_id=user_id,
                            text=chunk,
                            parse_mode=None,
                            **chunk_reply_kwargs,
                        )
                    if i == 0 and sent:
                        sent_id = sent.message_id
                except Exception as e:
                    logger.error("TG send error: %s", e)

        return sent_id

    async def send_typing(self, user_id) -> None:
        """Send/refresh typing indicator."""
        if self._bot:
            try:
                await self._bot.send_chat_action(chat_id=user_id, action="typing")
            except Exception:
                pass

    async def _cancel_stream_task(self, user_id: int, platform_msg_id: int) -> None:
        key = (int(user_id), int(platform_msg_id))
        task = self._stream_tasks.get(key)
        current = asyncio.current_task()
        if not task or task.done() or task is current:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._stream_tasks.get(key) is task:
                self._stream_tasks.pop(key, None)

    @staticmethod
    def _stream_snapshots(text: str, chunk_size: int) -> list[str]:
        cleaned = text or ""
        if chunk_size <= 0 or len(cleaned) <= chunk_size:
            return [cleaned]

        tokens = re.findall(r"\S+\s*", cleaned)
        if not tokens:
            return [cleaned]

        snapshots = []
        acc = ""
        next_cut = chunk_size
        for token in tokens:
            acc += token
            if len(acc) >= next_cut:
                snapshots.append(acc.rstrip())
                next_cut += chunk_size

        final_text = cleaned.rstrip() if cleaned.rstrip() else cleaned
        if not snapshots or snapshots[-1] != final_text:
            snapshots.append(final_text)
        return snapshots

    async def _edit_message_text(self, user_id, platform_msg_id, new_text: str, parse_markdown: bool = True) -> bool:
        if not self._bot:
            return False
        try:
            if parse_markdown:
                from ..markdown import mm_to_telegram
                tg_text = mm_to_telegram(new_text)
                try:
                    await self._bot.edit_message_text(
                        chat_id=user_id,
                        message_id=platform_msg_id,
                        text=tg_text,
                        parse_mode="MarkdownV2",
                    )
                except Exception:
                    await self._bot.edit_message_text(
                        chat_id=user_id,
                        message_id=platform_msg_id,
                        text=new_text,
                    )
            else:
                await self._bot.edit_message_text(
                    chat_id=user_id,
                    message_id=platform_msg_id,
                    text=new_text,
                )
            return True
        except Exception as e:
            logger.error("TG edit error: %s", e)
            return False

    async def edit_message(self, user_id, platform_msg_id, new_text: str) -> bool:
        await self._cancel_stream_task(user_id, platform_msg_id)
        return await self._edit_message_text(user_id, platform_msg_id, new_text, parse_markdown=True)

    async def stream_edit_message(self, user_id, platform_msg_id, new_text: str, chunk_size: int = 180, interval: float = 0.18) -> bool:
        if not self._bot:
            return False

        await self._cancel_stream_task(user_id, platform_msg_id)
        key = (int(user_id), int(platform_msg_id))
        current = asyncio.current_task()
        if current:
            self._stream_tasks[key] = current

        try:
            snapshots = self._stream_snapshots(new_text, chunk_size)
            if len(snapshots) == 1:
                return await self._edit_message_text(user_id, platform_msg_id, snapshots[0], parse_markdown=True)

            for partial in snapshots[:-1]:
                await self._rate_wait()
                ok = await self._edit_message_text(user_id, platform_msg_id, partial, parse_markdown=False)
                if not ok:
                    return False
                if interval > 0:
                    await asyncio.sleep(interval)

            await self._rate_wait()
            return await self._edit_message_text(user_id, platform_msg_id, snapshots[-1], parse_markdown=True)
        except asyncio.CancelledError:
            return False
        finally:
            if current and self._stream_tasks.get(key) is current:
                self._stream_tasks.pop(key, None)

    async def delete_message(self, user_id, platform_msg_id) -> bool:
        if not self._bot:
            return False
        await self._cancel_stream_task(user_id, platform_msg_id)
        try:
            await self._bot.delete_message(chat_id=user_id, message_id=platform_msg_id)
            return True
        except Exception as e:
            logger.error("TG delete error: %s", e)
            return False

    async def set_reaction(self, user_id, platform_msg_id, emoji: str) -> bool:
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=user_id, message_id=platform_msg_id,
                reaction=[{"type": "emoji", "emoji": emoji}],
            )
            return True
        except Exception as e:
            logger.error("TG set_reaction error: %s", e)
            return False

    async def clear_reactions(self, user_id, platform_msg_id) -> bool:
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=user_id, message_id=platform_msg_id, reaction=[],
            )
            return True
        except Exception as e:
            logger.error("TG clear_reaction error: %s", e)
            return False

    async def send_raw_text(self, user_id: int, text: str) -> None:
        """Send a plain text message (for system notifications like PAT expiry)."""
        if self._bot:
            try:
                await self._bot.send_message(chat_id=user_id, text=text)
            except Exception:
                pass

    # --- Typing management ---

    def start_typing_loop(self, user_id: int, timeout: float = 60.0):
        """Start a persistent typing indicator loop."""
        self.stop_typing_loop(user_id)
        task = asyncio.create_task(self._typing_loop(user_id, timeout))
        self._typing_tasks[user_id] = task

    def stop_typing_loop(self, user_id: int):
        """Cancel the typing loop for a user."""
        task = self._typing_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, user_id: int, max_duration: float = 60.0):
        try:
            elapsed = 0.0
            while elapsed < max_duration:
                await self.send_typing(user_id)
                await asyncio.sleep(4.0)
                elapsed += 4.0
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
