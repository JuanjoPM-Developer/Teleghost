# Changelog

All notable changes to BridgeMost are documented here.

## v2.2.1 (2026-03-30)

### Fixed
- **Duplicate relay** — main relay now skips DM channels owned by DM bridges
  - All WS handlers (post, edit, delete, typing) filter `_dm_bridge_channels`
  - Prevents messages from being relayed by both the relay bot and the DM bridge bot
- **Event loop crash** — wrapped `asyncio.gather` in async function to fix
  "future belongs to a different loop" error on Python 3.13

## v2.2.0 (2026-03-30)

### Added
- **DM Bridge mode** — dedicated TG bot per MM bot, direct 1:1 bridging
  - New `DmBridge` dataclass in `config.py` with `tg_bot_token`, `mm_bot_id`, `name`
  - New `dm_bridges:` config section (optional; inherits user mappings from `users:`)
  - New `DmBridgeRelay` class in `core.py` — one instance per `dm_bridge` entry
    - Each relay polls its own TG bot token (separate `TelegramAdapter` instance)
    - Discovers DM channel(s) between configured users and the target MM bot
    - Bidirectional relay: TG DM → MM DM channel, MM bot response → TG DM
    - Full feature parity: media, edits, deletes, reactions, voice-to-text, typing indicators
    - Per-relay SQLite store (`dm_<name>.db`) to avoid ID conflicts with main relay
  - `__main__.py` instantiates `DmBridgeRelay` instances and runs them via `asyncio.gather`
  - Graceful shutdown propagates `_running = False` to all relays
  - Health endpoint (`/health`) now includes `dm_bridges` array with per-relay stats
  - `config.example.yaml`: documented `dm_bridges:` section with inline comments

### Changed
- `__init__.py`: version bumped to `2.2.0`
- Startup log line includes DM bridge count

## v2.1.0 (2026-03-25)

### Added
- **Google Chat adapter** — `adapters/googlechat.py` (310 lines)
  - Service Account with domain-wide delegation (ghost mode)
  - Posts as the real Workspace user in Google Chat Spaces
  - Message polling, edit, delete, emoji reactions
  - `/bot`, `/bots`, `/status` commands
  - Auto-split long messages
- Config: `googlechat:` section with `credentials_file`, `delegated_user`, `space`, `poll_interval`
- Auto-detect adapter from config sections (telegram vs googlechat)

### Changed
- `__main__.py` routes to correct adapter at startup
- `pyproject.toml`: added `google-auth` + `google-api-python-client` dependencies
- `config.example.yaml`: full Google Chat section with inline documentation

## v2.0.2 (2026-03-25)

### Changed
- README rewritten for multi-platform architecture (reduced Telegram-specific references)
- GitHub repo description updated to "Multi-platform ↔ Mattermost transparent bridge"
- `pyproject.toml` keywords updated

## v2.0.1 (2026-03-25)

### Fixed
- Removed unused `Path` import from `base.py`
- Renamed TG-specific emoji function names in core to `unicode_to_mm`/`mm_to_unicode`
- Replaced private adapter attribute access in `core.py` with `hasattr` check

## v2.0.0 (2026-03-25)

### Changed — **Plugin Adapter Architecture**
- Extracted Telegram logic into `adapters/telegram.py` (461 lines)
- Created `adapters/base.py` — abstract interface (8 methods + 2 dataclasses + 4 callbacks)
- Created `core.py` — platform-agnostic relay engine (595 lines)
- `bridge.py` reduced to 31-line backward-compatibility wrapper
- Adding new platforms = one Python file implementing `BaseAdapter`, zero core changes

## v1.0.0 (2026-03-25)

### Added
- Published to **PyPI** (`pip install bridgemost`)
- GitHub Actions CI/CD pipeline (Python 3.11/3.12/3.13 + lint)

## v0.9.8 (2026-03-25)

### Fixed
- `_relay_mm_file_to_tg` used wrong token for DM file downloads (403 silent fail)
- `file_ids` from WebSocket could be string instead of list — added type guard
- Empty `config.users` caused crash on WS fallback — fail-fast with clear message

## v0.9.7 (2026-03-25)

### Added
- Dockerfile + docker-compose.yml for containerized deployment
- .dockerignore for clean builds
- Extended test suite: 71 tests (was 56) covering store prefs, health, whisper, mattermost client
- CHANGELOG.md (this file)

### Fixed
- Active bot persistence across restarts (v0.9.6 fix included)

## v0.9.6 (2026-03-25)

### Fixed
- **Active bot lost on restart** — `/bot` selection now persisted in SQLite `user_prefs` table
- Bot switch survives service restarts, reboots, and crashes

## v0.9.5 (2026-03-24)

### Fixed
- Version alignment across all files (README, pyproject.toml, health endpoint)
- Dynamic version sourcing from single `__init__.__version__`
- Production hardening: logrotate config, HEARTBEAT.md monitoring

## v0.9.4 (2026-03-24)

### Added
- **Smart MM→TG file relay** — photos, audio, video, GIFs, documents sent by bots in MM arrive as native Telegram media (not generic file downloads)
- `get_file_info()` API method for MIME-based dispatch

### Fixed
- GitHub Issue #1: NameError in whisper.py `finally` block when file open fails

## v0.9.3 (2026-03-24)

### Fixed
- `text` variable used before assignment in sticker/location/poll handlers
- Venue captured by location handler (reordered elif chain)
- Poll metadata formatting (clean separators)

## v0.9.2 (2026-03-24)

### Added
- **Stickers** — .webp/.tgs/.webm uploaded to MM with emoji fallback
- **Locations** — 📍 coordinates + Google Maps link
- **Venues** — 📍 name + address + map link
- **Polls** — 📊 question + numbered options

## v0.9.1 (2026-03-24)

### Added
- 56 unit tests across 5 test files (markdown, emoji, store, bridge, config)
- Multi-user support highlighted in README

## v0.9.0 (2026-03-24)

### Changed
- Complete README rewrite with prerequisites, value lookup table, 10 troubleshooting items
- config.example.yaml rewritten with inline documentation for every field

## v0.8.3 (2026-03-24)

### Fixed
- Removed dead `_handle_ping_command` method (57 lines)
- Cleaned unused imports (ParseMode, TGMessage)
- Moved inline imports to top-level (datetime, tempfile)

## v0.8.2 (2026-03-24)

### Fixed
- Removed `/ping` command (caused infinite typing indicator)
- Typing timeout reduced 300s → 60s
- Version alignment (pyproject, README)
- Removed TeleGhost residuals (egg-info, stale tags)

## v0.8.1 (2026-03-24)

### Added
- `/bots` — list all bots with live 🟢/⚫ status
- `/status` — detailed info on active bot
- `get_user_status()` MM API method

## v0.8.0 (2026-03-24)

### Added
- **SQLite persistent message mapping** — survives restarts (30-day TTL)
- WebSocket reconnect jitter (±25%) to prevent thundering herd
- Telegram rate limiter (25 msg/s sliding window)

## v0.7.0 (2026-03-24)

### Fixed
- 7-bug audit: voice `text` before assignment, whisper file handle leak, env var name, typing timeout, PAT expiry detection, version centralization, reaction user lookup

## v0.6.0 (2026-03-24)

### Added
- Interactive setup wizard (`bridgemost setup`)
- Guided config generation with MM login, bot discovery, PAT creation

## v0.5.0 (2026-03-24)

### Added
- Startup resilience: token pre-validation, DM discovery retry, graceful abort

## v0.4.0 (2026-03-24)

### Added
- **Voice-to-text** via Whisper API (transcribes voice messages before posting)

## v0.3.1 (2026-03-24)

### Added
- Synthetic typing indicator — works regardless of bot typing support

## v0.3.0 (2026-03-24)

### Added
- **Multi-bot routing** with `/bot` command — talk to any MM bot from single TG chat

## v0.2.0 (2026-03-24)

### Added
- Emoji/reaction sync (bidirectional TG ↔ MM)

## v0.1.1 (2026-03-24)

### Added
- Edit/delete sync (bidirectional)

## v0.1.0 (2026-03-24)

### Changed
- **WebSocket transport** replacing MM polling — real-time with <100ms latency

## v0.0.5 (2026-03-24)

### Added
- First public release
- Transparent user identity (posts as real MM user via PAT)
- Full media support (photos, documents, audio, video)
- Markdown MM→TG conversion
- Health endpoint
- Error notifications via Telegram
