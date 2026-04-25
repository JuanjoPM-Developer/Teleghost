"""Telegram-specific presentation helpers for BridgeMost."""

import asyncio
import re
from dataclasses import dataclass

from .adapters.base import OutboundMessage


@dataclass
class TelegramPresentationConfig:
    """How BridgeMost should present MM bot output back into Telegram."""

    enabled: bool = True
    suppress_internal_progress: bool = True
    show_placeholder: bool = True
    placeholder_text: str = "🧠⚡ Conectando a la red neuronal..."
    placeholder_delay_seconds: float = 1.2
    stream_final_response: bool = True
    stream_chunk_chars: int = 180
    stream_edit_interval: float = 0.18


@dataclass
class PendingTelegramPresentation:
    """Placeholder / streaming state for one MM DM channel."""

    placeholder_task: asyncio.Task | None = None
    placeholder_msg_id: int | None = None


_INTERNAL_STATUS_PREFIXES = (
    "⚠️ Context:",
    "⏳ Still working...",
    "💾 Memory updated",
)
_INTERNAL_STATUS_SNIPPETS = (
    "to compaction",
    "Auto-compaction is disabled",
)
_INTERNAL_TOOL_NAMES = {
    "browser_back",
    "browser_click",
    "browser_console",
    "browser_get_images",
    "browser_navigate",
    "browser_press",
    "browser_scroll",
    "browser_snapshot",
    "browser_type",
    "browser_vision",
    "clarify",
    "cronjob",
    "delegate_task",
    "execute_code",
    "memory",
    "patch",
    "process",
    "read_file",
    "search_files",
    "session_search",
    "skill_manage",
    "skill_view",
    "terminal",
    "text_to_speech",
    "todo",
    "vision_analyze",
    "write_file",
}
_INTERNAL_LINE_RE = re.compile(r"^[^A-Za-z0-9_/.-]*([A-Za-z_][A-Za-z0-9_.-]*)\s*:")


def _line_is_internal(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped:
        return True
    if stripped == "Editado":
        return True
    if any(stripped.startswith(prefix) for prefix in _INTERNAL_STATUS_PREFIXES):
        return True
    if any(snippet in stripped for snippet in _INTERNAL_STATUS_SNIPPETS):
        return True

    match = _INTERNAL_LINE_RE.match(stripped)
    if not match:
        return False
    token = match.group(1).lower()
    return token in _INTERNAL_TOOL_NAMES or token.startswith("browser_")


def is_internal_progress_text(text: str) -> bool:
    """Return True when the MM post is tool chatter / status noise for Telegram."""

    normalized = (text or "").strip()
    if not normalized:
        return False
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return False
    return all(_line_is_internal(line) for line in lines)


class TelegramPresentationMixin:
    """Shared clean-mode behavior for main relay + dedicated DM bridges."""

    def _init_telegram_presentation(self) -> None:
        self._presentation: dict[str, PendingTelegramPresentation] = {}

    def _tp_config(self) -> TelegramPresentationConfig | None:
        return getattr(self.config, "telegram_presentation", None)

    def _telegram_clean_mode_enabled(self) -> bool:
        cfg = self._tp_config()
        return bool(
            cfg
            and getattr(cfg, "enabled", False)
            and getattr(self.config, "adapter", "telegram") == "telegram"
        )

    def _should_suppress_mm_text(self, text: str) -> bool:
        cfg = self._tp_config()
        return bool(
            self._telegram_clean_mode_enabled()
            and getattr(cfg, "suppress_internal_progress", True)
            and is_internal_progress_text(text)
        )

    async def _cancel_placeholder_task(self, channel_id: str) -> None:
        state = self._presentation.get(channel_id)
        if not state or not state.placeholder_task:
            return
        task = state.placeholder_task
        state.placeholder_task = None
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _clear_pending_presentation(self, channel_id: str, user_id: int, delete_placeholder: bool = False) -> None:
        if not self._telegram_clean_mode_enabled():
            return
        state = self._presentation.get(channel_id)
        if not state:
            return
        await self._cancel_placeholder_task(channel_id)
        if delete_placeholder and state.placeholder_msg_id:
            try:
                await self.adapter.delete_message(user_id, state.placeholder_msg_id)
            except Exception:
                pass
            state.placeholder_msg_id = None

    async def _schedule_placeholder(
        self,
        channel_id: str,
        user_id: int,
        reply_to_platform_msg_id: int | None = None,
    ) -> None:
        cfg = self._tp_config()
        if not self._telegram_clean_mode_enabled() or not getattr(cfg, "show_placeholder", True):
            return

        state = self._presentation.setdefault(channel_id, PendingTelegramPresentation())
        await self._cancel_placeholder_task(channel_id)
        if state.placeholder_msg_id:
            try:
                await self.adapter.delete_message(user_id, state.placeholder_msg_id)
            except Exception:
                pass
            state.placeholder_msg_id = None

        delay = max(0.0, float(getattr(cfg, "placeholder_delay_seconds", 1.2)))
        placeholder_text = getattr(cfg, "placeholder_text", "🧠⚡ Conectando a la red neuronal...")
        placeholder_msg = OutboundMessage(
            text=placeholder_text,
            reply_to_platform_msg_id=reply_to_platform_msg_id,
        )

        if delay == 0:
            state.placeholder_msg_id = await self.adapter.send_message(user_id, placeholder_msg)
            return

        async def _show_placeholder() -> None:
            try:
                await asyncio.sleep(delay)
                state.placeholder_msg_id = await self.adapter.send_message(user_id, placeholder_msg)
            except asyncio.CancelledError:
                return

        state.placeholder_task = asyncio.create_task(_show_placeholder())

    async def _present_visible_text(
        self,
        channel_id: str,
        user_id: int,
        mm_post_id: str,
        text: str,
        reply_to_platform_msg_id: int | None = None,
    ):
        cfg = self._tp_config()
        if not self._telegram_clean_mode_enabled():
            sent_id = await self.adapter.send_message(
                user_id,
                OutboundMessage(
                    text=text,
                    reply_to_platform_msg_id=reply_to_platform_msg_id,
                ),
            )
            if sent_id:
                self._track_pair(sent_id, mm_post_id)
            return sent_id

        state = self._presentation.setdefault(channel_id, PendingTelegramPresentation())
        await self._cancel_placeholder_task(channel_id)
        placeholder_id = state.placeholder_msg_id
        state.placeholder_msg_id = None

        if placeholder_id:
            edit_ok = False
            stream_fn = getattr(self.adapter, "stream_edit_message", None)
            if callable(stream_fn) and getattr(cfg, "stream_final_response", True):
                edit_ok = bool(await stream_fn(
                    user_id,
                    placeholder_id,
                    text,
                    chunk_size=int(getattr(cfg, "stream_chunk_chars", 180)),
                    interval=float(getattr(cfg, "stream_edit_interval", 0.18)),
                ))
            else:
                edit_ok = bool(await self.adapter.edit_message(user_id, placeholder_id, text))
            if edit_ok:
                self._track_pair(placeholder_id, mm_post_id)
                return placeholder_id

            sent_id = await self.adapter.send_message(user_id, OutboundMessage(text=text))
            if sent_id:
                self._track_pair(sent_id, mm_post_id)
            return sent_id

        sent_id = await self.adapter.send_message(
            user_id,
            OutboundMessage(
                text=text,
                reply_to_platform_msg_id=reply_to_platform_msg_id,
            ),
        )
        if sent_id:
            self._track_pair(sent_id, mm_post_id)
        return sent_id
