"""Abstract base class for chat adapters.

Every adapter implements these methods to bridge a chat platform
(Telegram, Google Chat, Slack, Matrix, etc.) with Mattermost.
The core relay engine calls these methods without knowing which
platform is on the other end.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Awaitable


@dataclass
class InboundMessage:
    """A message received from the chat platform, normalized for the core."""
    platform_msg_id: Any          # Platform-specific message ID (int for TG, str for others)
    user_id: Any                  # Platform-specific user ID
    text: str = ""                # Message text
    file_path: str | None = None  # Local path to downloaded media file
    file_name: str = ""           # Original filename
    file_mime: str = ""           # MIME type
    is_edit: bool = False         # True if this is an edit of an existing message
    is_voice: bool = False        # True if this is a voice message (for Whisper)
    reply_to_msg_id: Any = None   # Platform message ID this message replies to
    location: tuple[float, float] | None = None  # (lat, lon)
    venue_name: str = ""          # Venue name (if location is a venue)
    venue_address: str = ""       # Venue address
    poll_question: str = ""       # Poll question
    poll_options: list[str] | None = None  # Poll choices
    poll_anonymous: bool = False
    poll_multiple: bool = False
    sticker_emoji: str = ""       # Sticker emoji hint
    reaction_added: list[str] | None = None   # Emojis added
    reaction_removed: list[str] | None = None # Emojis removed
    reaction_msg_id: Any = None   # Message the reaction is on


@dataclass
class OutboundMessage:
    """A message to send TO the chat platform (from MM bot response)."""
    text: str = ""
    file_path: str | None = None
    file_name: str = ""
    file_mime: str = ""
    file_size: int = 0
    is_edit: bool = False
    edit_platform_msg_id: Any = None  # Which message to edit
    is_delete: bool = False
    delete_platform_msg_id: Any = None
    reply_to_platform_msg_id: Any = None  # Which platform message this should reply to
    reaction_emoji: str = ""          # Emoji to set
    reaction_msg_id: Any = None
    reaction_clear: bool = False      # Clear all reactions


class BaseAdapter(ABC):
    """Abstract adapter interface.

    Subclasses must implement all abstract methods.
    The core sets the callbacks after construction via set_callbacks().
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the adapter (connect, authenticate, begin listening)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the adapter."""
        ...

    @abstractmethod
    async def send_message(self, user_id: Any, msg: OutboundMessage) -> Any:
        """Send a message to a user. Returns platform message ID or None."""
        ...

    @abstractmethod
    async def send_typing(self, user_id: Any) -> None:
        """Send a typing indicator to a user."""
        ...

    @abstractmethod
    async def edit_message(self, user_id: Any, platform_msg_id: Any, new_text: str) -> bool:
        """Edit an existing message. Returns True on success."""
        ...

    @abstractmethod
    async def delete_message(self, user_id: Any, platform_msg_id: Any) -> bool:
        """Delete a message. Returns True on success."""
        ...

    @abstractmethod
    async def set_reaction(self, user_id: Any, platform_msg_id: Any, emoji: str) -> bool:
        """Set a reaction on a message."""
        ...

    @abstractmethod
    async def clear_reactions(self, user_id: Any, platform_msg_id: Any) -> bool:
        """Clear all reactions from a message."""
        ...

    # --- Callbacks set by the core ---

    _on_message: Callable[[InboundMessage], Awaitable[None]] | None = None
    _on_edit: Callable[[InboundMessage], Awaitable[None]] | None = None
    _on_reaction: Callable[[InboundMessage], Awaitable[None]] | None = None
    _on_command: Callable[[str, list[str], Any], Awaitable[str | None]] | None = None

    def set_callbacks(
        self,
        on_message: Callable[[InboundMessage], Awaitable[None]],
        on_edit: Callable[[InboundMessage], Awaitable[None]],
        on_reaction: Callable[[InboundMessage], Awaitable[None]],
        on_command: Callable[[str, list[str], Any], Awaitable[str | None]],
    ) -> None:
        """Register callbacks from the core relay engine."""
        self._on_message = on_message
        self._on_edit = on_edit
        self._on_reaction = on_reaction
        self._on_command = on_command
