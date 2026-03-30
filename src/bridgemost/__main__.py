"""BridgeMost entry point."""

import asyncio
import logging
import signal
import sys

from .config import load_config
from .core import BridgeMostCore, DmBridgeRelay


def setup_logging(level: str, log_file: str = ""):
    """Configure logging."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def main():
    """Main entry point."""
    # Handle `bridgemost setup` command
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from .setup import main as setup_main
        setup_main()
        return

    config_path = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        config = load_config(config_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("Tip: Run 'python -m bridgemost setup' to create config.yaml interactively.")
        sys.exit(1)

    setup_logging(config.log_level, config.log_file)
    logger = logging.getLogger("bridgemost")

    from . import __version__
    logger.info("BridgeMost v%s — Multi-Platform ↔ Mattermost Bridge", __version__)
    logger.info(
        "Adapter: %s | Users: %d | DM bridges: %d",
        config.adapter, len(config.users), len(config.dm_bridges),
    )

    # Build the appropriate adapter
    if config.adapter == "googlechat":
        from .adapters.googlechat import GoogleChatAdapter
        user_id = config.users[0].telegram_id if config.users else config.gchat_delegated_user
        adapter = GoogleChatAdapter(
            credentials_file=config.gchat_credentials_file,
            delegated_user=config.gchat_delegated_user,
            space=config.gchat_space,
            poll_interval=config.gchat_poll_interval,
            user_id=user_id,
        )
    elif config.adapter == "telegram":
        from .adapters.telegram import TelegramAdapter
        allowed_ids = [u.telegram_id for u in config.users] if config.users else None
        adapter = TelegramAdapter(
            bot_token=config.tg_bot_token,
            allowed_user_ids=allowed_ids,
        )
    else:
        logger.critical("Unknown adapter: %s", config.adapter)
        sys.exit(1)

    core = BridgeMostCore(config, adapter)

    # Build DM bridge relays
    dm_relays = [DmBridgeRelay(config, bridge) for bridge in config.dm_bridges]

    # Wire DM relay stats into the health endpoint
    if dm_relays:
        core.health.dm_bridges_fn = lambda: [r.stats_snapshot() for r in dm_relays]

    # Graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown_handler(sig, frame):
        logger.info("Received %s, shutting down...", signal.Signals(sig).name)
        core._running = False
        for relay in dm_relays:
            relay._running = False

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    async def _run_all():
        coros = [core.start()] + [r.start() for r in dm_relays]
        await asyncio.gather(*coros)

    try:
        loop.run_until_complete(_run_all())
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down...")
    finally:
        loop.close()
        logger.info("BridgeMost stopped.")


if __name__ == "__main__":
    main()
