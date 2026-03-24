"""BridgeMost entry point."""

import asyncio
import logging
import signal
import sys

from .config import load_config
from .bridge import BridgeMostBridge


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

    logger.info("BridgeMost v0.6.0 — Transparent Telegram ↔ Mattermost Bridge")
    logger.info("Users mapped: %d", len(config.users))

    bridge = BridgeMostBridge(config)

    # Graceful shutdown
    loop = asyncio.new_event_loop()

    def shutdown_handler(sig, frame):
        logger.info("Received %s, shutting down...", signal.Signals(sig).name)
        bridge._running = False

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        loop.run_until_complete(bridge.start())
    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down...")
    finally:
        loop.close()
        logger.info("BridgeMost stopped.")


if __name__ == "__main__":
    main()
