"""Configuration loader for BridgeMost."""

import os
import yaml
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bridgemost")


@dataclass
class BotRoute:
    """A bot that the user can talk to via the relay."""
    name: str          # Display name (e.g. "MyBot", "Assistant")
    mm_bot_id: str     # MM user ID of the bot
    mm_dm_channel: str = ""  # Will be auto-discovered if empty
    is_default: bool = False


@dataclass
class UserMapping:
    """Maps a Telegram user to a Mattermost account."""
    telegram_id: int
    telegram_name: str
    mm_user_id: str
    mm_token: str
    mm_dm_channel: str = ""       # Legacy: single-bot DM channel
    mm_target_bot: str = ""       # Legacy: single bot target
    bots: list[BotRoute] = field(default_factory=list)
    active_bot: str = ""          # Currently active bot name


@dataclass
class Config:
    """BridgeMost configuration."""
    # Adapter selection (auto-detected from config sections)
    adapter: str = ""  # "telegram", "googlechat", etc. Empty = auto-detect

    # Telegram
    tg_bot_token: str = ""

    # Google Chat
    gchat_credentials_file: str = ""   # Path to service-account.json
    gchat_delegated_user: str = ""     # user@company.com (ghost mode)
    gchat_space: str = ""              # spaces/AAAAxyz...
    gchat_poll_interval: float = 2.0   # Seconds between polls

    # Mattermost
    mm_url: str = "http://127.0.0.1:8065"
    mm_bot_token: str = ""
    mm_bot_user_id: str = ""

    # User mappings
    users: list[UserMapping] = field(default_factory=list)

    # Logging
    log_level: str = "INFO"
    log_file: str = ""

    # Polling
    tg_timeout: int = 30
    mm_poll_interval: float = 0.5

    # Health
    health_port: int = 9191

    # Data persistence
    data_dir: str = ""  # Directory for SQLite DB; empty = working directory

    # Voice-to-text (Whisper)
    whisper_url: str = ""          # e.g. http://localhost:9000
    whisper_api_key: str = ""      # Required for OpenAI/Groq, empty for local
    whisper_model: str = "large-v3"  # Model name (large-v3, whisper-1, etc.)
    whisper_language: str = ""     # ISO 639-1 code (es, en) or empty for auto
    whisper_keep_audio: bool = True  # Also send original audio alongside text

    def get_user_by_tg_id(self, tg_id: int) -> UserMapping | None:
        """Find user mapping by Telegram ID."""
        for u in self.users:
            if u.telegram_id == tg_id:
                return u
        return None

    def get_user_by_mm_id(self, mm_id: str) -> UserMapping | None:
        """Find user mapping by Mattermost user ID."""
        for u in self.users:
            if u.mm_user_id == mm_id:
                return u
        return None


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from YAML file.

    Search order:
    1. Explicit path argument
    2. BRIDGEMOST_CONFIG env var
    3. ./config.yaml
    4. /etc/bridgemost/config.yaml
    """
    candidates = []
    if path:
        candidates.append(Path(path))
    if env := os.environ.get("BRIDGEMOST_CONFIG"):
        candidates.append(Path(env))
    candidates.append(Path("config.yaml"))
    candidates.append(Path("/etc/bridgemost/config.yaml"))

    config_path = None
    for c in candidates:
        if c.exists():
            config_path = c
            break

    if not config_path:
        raise FileNotFoundError(
            f"No config file found. Searched: {[str(c) for c in candidates]}"
        )

    logger.info("Loading config from %s", config_path)
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    cfg = Config()

    # Adapter (auto-detect if not specified)
    cfg.adapter = raw.get("adapter", "")

    # Telegram
    tg = raw.get("telegram", {})
    cfg.tg_bot_token = tg.get("bot_token", "")

    # Google Chat
    gc = raw.get("googlechat", {})
    cfg.gchat_credentials_file = gc.get("credentials_file", "")
    cfg.gchat_delegated_user = gc.get("delegated_user", "")
    cfg.gchat_space = gc.get("space", "")
    cfg.gchat_poll_interval = gc.get("poll_interval", 2.0)

    # Auto-detect adapter
    if not cfg.adapter:
        if cfg.tg_bot_token:
            cfg.adapter = "telegram"
        elif cfg.gchat_credentials_file:
            cfg.adapter = "googlechat"
        else:
            cfg.adapter = "telegram"  # Default fallback

    # Mattermost
    mm = raw.get("mattermost", {})
    cfg.mm_url = mm.get("url", cfg.mm_url).rstrip("/")
    cfg.mm_bot_token = mm.get("bot_token", "")
    cfg.mm_bot_user_id = mm.get("bot_user_id", "")

    # Users
    for u in raw.get("users", []):
        bots = []
        for b in u.get("bots", []):
            bots.append(BotRoute(
                name=b["name"],
                mm_bot_id=b["mm_bot_id"],
                mm_dm_channel=b.get("mm_dm_channel", ""),
                is_default=b.get("default", False),
            ))

        mapping = UserMapping(
            telegram_id=int(u["telegram_id"]),
            telegram_name=u.get("telegram_name", "Unknown"),
            mm_user_id=u["mm_user_id"],
            mm_token=u["mm_token"],
            mm_dm_channel=u.get("mm_dm_channel", ""),
            mm_target_bot=u.get("mm_target_bot", ""),
            bots=bots,
        )

        # Set active bot to default or first
        default_bots = [b for b in bots if b.is_default]
        if default_bots:
            mapping.active_bot = default_bots[0].name
        elif bots:
            mapping.active_bot = bots[0].name

        # Legacy compatibility: if no bots list but mm_target_bot exists,
        # create a single bot route
        if not bots and u.get("mm_target_bot"):
            mapping.bots = [BotRoute(
                name="default",
                mm_bot_id=u["mm_target_bot"],
                mm_dm_channel=u.get("mm_dm_channel", ""),
                is_default=True,
            )]
            mapping.active_bot = "default"

        cfg.users.append(mapping)

    # Logging
    log = raw.get("logging", {})
    cfg.log_level = log.get("level", "INFO")
    cfg.log_file = log.get("file", "")

    # Polling
    poll = raw.get("polling", {})
    cfg.tg_timeout = poll.get("telegram_timeout", 30)
    cfg.mm_poll_interval = poll.get("mm_poll_interval", 0.5)

    # Health
    health = raw.get("health", {})
    cfg.health_port = health.get("port", 9191)

    # Data persistence
    storage = raw.get("storage", {})
    cfg.data_dir = storage.get("data_dir", "")

    # Voice-to-text
    vtt = raw.get("voice_to_text", {})
    cfg.whisper_url = vtt.get("url", "")
    cfg.whisper_api_key = vtt.get("api_key", "")
    cfg.whisper_model = vtt.get("model", "large-v3")
    cfg.whisper_language = vtt.get("language", "")
    cfg.whisper_keep_audio = vtt.get("keep_audio", True)

    return cfg
