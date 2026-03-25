# Changelog

All notable changes to BridgeMost are documented here.

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
