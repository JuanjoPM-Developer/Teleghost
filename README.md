# BridgeMost 👻

**Telegram ↔ Mattermost Transparent Bridge**

BridgeMost makes your Telegram messages appear **natively** in Mattermost — as your real user, with your avatar and name. Bot responses relay back to Telegram instantly via WebSocket.

Unlike Matterbridge or webhooks that post with `[User]` prefixes, BridgeMost posts as **your actual Mattermost account**. Nobody in Mattermost can tell you're writing from Telegram.

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🪪 Transparent identity | Posts as your real MM user (avatar, name, everything) |
| 📁 Full media | Photos, documents, audio, video, voice — bidirectional |
| 🎤 Voice-to-text | Voice messages auto-transcribed via Whisper API |
| 🤖 Multi-bot routing | Talk to multiple bots; switch with `/bot name` |
| ⚡ Real-time WebSocket | Responses arrive instantly (no polling) |
| ✏️ Edit & delete sync | Edits and deletes stay in sync both ways |
| 😀 Reactions | Emoji reactions synced between TG and MM |
| ⌨️ Typing indicator | "Bot is typing..." shown in Telegram |
| 📝 Markdown | MM markdown → Telegram MarkdownV2 auto-conversion |
| 🔒 Startup checks | Validates tokens + discovers channels before starting |
| 💾 Persistent mapping | SQLite store for message IDs (survives restarts) |
| 🩺 Health endpoint | HTTP `/health` on configurable port |

| 👥 Multi-user | Multiple Telegram users, each with their own MM identity |

~55 MB RAM · ~250 ms latency · asyncio-based · Python 3.11+

> **Multi-user ready:** Multiple people can use the same BridgeMost instance — each with their own Telegram account, Mattermost identity, and bot routing. Add users to `config.yaml` and they appear as themselves in Mattermost. No shared accounts, no impersonation.

---

## 🚀 Installation — Step by Step

### What you need before starting

| # | Item | Where to get it |
|---|------|----------------|
| 1 | **Mattermost server** (self-hosted) | You must be admin or have an admin enable PAT support |
| 2 | **Telegram bot token** | Create via [@BotFather](https://t.me/BotFather) → `/newbot` |
| 3 | **Your Telegram user ID** | Send any message to [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID |
| 4 | **Python 3.11+** | `python3 --version` to check |
| 5 | **Git** | `git --version` to check |

### Step 1 — Enable Personal Access Tokens on Mattermost

> ⚠️ **This step is REQUIRED.** Without it, BridgeMost cannot post as your user.

**Option A — Via Mattermost UI (admin):**
1. Go to **System Console → Authentication → Token Access**
2. Set **Enable Personal Access Tokens** to `true`
3. Save

**Option B — Via command line (requires access to the server):**
```bash
# If mmctl is available:
mmctl --local config set ServiceSettings.EnableUserAccessTokens true

# Or via REST API with admin token:
curl -X PUT http://localhost:8065/api/v4/config/patch \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ServiceSettings": {"EnableUserAccessTokens": true}}'
```

### Step 2 — Clone and install

```bash
git clone https://github.com/JuanjoPM-Developer/BridgeMost.git
cd BridgeMost
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Step 3 — Configure

**Option A — Interactive wizard (recommended):**

```bash
python3 -m bridgemost setup
```

The wizard will:
1. Connect to your Mattermost server
2. Log you in (password is NOT stored)
3. Auto-create a Personal Access Token for BridgeMost
4. List all bots on the server — you pick which ones to bridge
5. Ask for your Telegram bot token and user ID
6. Generate `config.yaml` automatically

**Option B — Manual configuration:**

```bash
cp config.example.yaml config.yaml
```

Then edit `config.yaml` — see the [Configuration Reference](#-configuration-reference) below for each field.

### Step 4 — Run

```bash
# Foreground (for testing):
python3 -m bridgemost

# Or as a systemd service (recommended for production):
sudo cp bridgemost.service.example /etc/systemd/system/bridgemost.service
# Edit the service file — update paths to match your installation
sudo systemctl daemon-reload
sudo systemctl enable --now bridgemost
```

### Step 5 — Test

1. Open Telegram and send a message to your BridgeMost bot
2. The message should appear in Mattermost as **your real user**
3. When the MM bot responds, the response should appear in your Telegram chat

---

## ⚙️ Configuration Reference

### Minimal config.yaml

```yaml
telegram:
  bot_token: "123456:ABC-DEF..."        # From @BotFather

mattermost:
  url: "http://localhost:8065"           # Your MM server URL (http or https)
  bot_token: "abc123..."                 # Any bot's access token (for WebSocket)
  bot_user_id: "a1b2c3d4..."            # User ID of that bot

users:
  - telegram_id: 123456789              # Your numeric Telegram user ID
    telegram_name: "Your Name"           # Display name (for logs only)
    mm_user_id: "x1y2z3..."             # Your Mattermost user ID
    mm_token: "your-pat-here"           # Your Personal Access Token
    bots:
      - name: "mybot"                   # Friendly name (used with /bot command)
        mm_bot_id: "bot-user-id-here"   # The bot's Mattermost user ID
        mm_dm_channel: ""               # Leave empty — auto-discovered at startup
        default: true                   # First bot to talk to when bridge starts
```

### How to find each value

| Field | How to get it |
|-------|--------------|
| `telegram.bot_token` | [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token |
| `telegram_id` | Send any message to [@userinfobot](https://t.me/userinfobot) |
| `mattermost.url` | The URL you use to open Mattermost in your browser |
| `mattermost.bot_token` | MM → Integrations → Bot Accounts → pick any bot → copy token. Or ask your admin. |
| `mattermost.bot_user_id` | Run: `mmctl user search <botname>` → copy the `id` field. Or: `curl http://YOUR_MM/api/v4/users/username/<botname> -H "Authorization: Bearer TOKEN"` → `"id"` field |
| `mm_user_id` | Same as above but with your own username instead of the bot's |
| `mm_token` (PAT) | MM → click your avatar → Profile → Security → Personal Access Tokens → Create. Or the setup wizard creates it for you. |
| `mm_bot_id` | Same method as `bot_user_id` — the Mattermost user ID of the bot you want to talk to |
| `mm_dm_channel` | **Leave empty** — BridgeMost discovers it automatically. Only fill in if auto-discovery fails (check logs). |

### Optional sections

```yaml
# Voice-to-text transcription (requires a Whisper-compatible API)
voice_to_text:
  url: "http://localhost:9000"          # Whisper endpoint
  api_key: ""                           # For OpenAI/Groq; empty for local Whisper
  model: "large-v3"                     # large-v3, whisper-1, whisper-large-v3-turbo
  language: ""                          # "es", "en", or "" for auto-detect
  keep_audio: true                      # Also attach audio file alongside transcript

# Health monitoring endpoint
health:
  port: 9191                            # HTTP health check on this port

# Message persistence
storage:
  data_dir: ""                          # SQLite DB location; empty = working directory

# Logging
logging:
  level: "INFO"                         # DEBUG, INFO, WARNING, ERROR
  file: ""                              # Log file path, or "" for stdout only
```

---

## 🤖 Telegram Commands

| Command | Description |
|---------|-------------|
| `/bot` | List all available bots and show which one is active |
| `/bot name` | Switch to a different bot |
| `/bots` | Show all bots with live 🟢/⚫ online status |
| `/status` | Detailed info about the active bot (state, last message, store stats) |

Just type normally to send messages — commands are only for bot management.

---

## 🎤 Voice-to-Text

When `voice_to_text` is configured, voice messages are transcribed before posting:

> 🎤 Hello, this is what I said in the voice message

If `keep_audio: true`, the original audio file is also attached.

Compatible APIs:
- [OpenAI Whisper API](https://platform.openai.com/docs/guides/speech-to-text) (`whisper-1`)
- [Groq Whisper](https://console.groq.com/) (`whisper-large-v3-turbo` — free tier)
- [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) (self-hosted, any model)
- Any endpoint accepting `POST` with `multipart/form-data` and returning `{"text": "..."}`

---

## 📊 Health Endpoint

```bash
curl http://localhost:9191/health
```

```json
{
  "status": "ok",
  "version": "0.9.5",
  "transport": "websocket",
  "uptime": "2h15m30s",
  "messages": { "tg_to_mm": 42, "mm_to_tg": 38, "errors": 0 },
  "store": { "persistent_mappings": 156 }
}
```

---

## 🏗️ Architecture

```
┌──────────┐              ┌──────────────┐              ┌──────────────┐
│ Telegram │◄────────────►│  BridgeMost  │◄────────────►│  Mattermost  │
│  (User)  │  Bot API     │   (Bridge)   │  REST + WS   │  (Bots)      │
└──────────┘              └──────────────┘              └──────────────┘
```

1. **TG → MM**: User sends via Telegram → BridgeMost posts to MM using the user's PAT (appears as real user)
2. **MM → TG**: Bot responds → WebSocket delivers event instantly → BridgeMost relays to Telegram
3. **Edits/Deletes/Reactions**: Tracked via bidirectional message ID mapping (SQLite-backed, 30-day TTL)

---

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| `FATAL: Token validation FAILED` | Your PAT is invalid or expired. Create a new one in MM → Profile → Security → Personal Access Tokens. Also verify `EnableUserAccessTokens` is `true` in System Console. |
| `⚠️ Tu token de Mattermost ha expirado` (Telegram alert) | Same as above — renew the PAT and update `mm_token` in config.yaml, then restart. |
| `Zero DM channels discovered` | BridgeMost couldn't find any DM channel with the configured bots. Make sure you have sent at least one DM to each bot in Mattermost before starting. The `mm_bot_id` values must be correct user IDs (26 alphanumeric characters). |
| `WS auth rejected (CLOSE on connect)` | The `mattermost.bot_token` is invalid. Get a valid bot token from Integrations → Bot Accounts. |
| `OSError: [Errno 98] address already in use` (health port) | Another process is using the health port (default 9191). Change `health.port` in config or stop the conflicting service. |
| Messages arrive but with `[BotName]` prefix | This is normal in multi-bot mode — it identifies which bot sent the response. With a single bot, no prefix is added. |
| Voice messages not transcribed | Check that `voice_to_text.url` is reachable: `curl http://YOUR_WHISPER_URL/asr`. For OpenAI/Groq, verify `api_key` is set. |
| `EnableUserAccessTokens` keeps turning off | Something (another admin, a script, or a bot) is toggling it. Set it permanently and check who has access to System Console. |
| Bridge starts but no messages relay | Check logs (`journalctl -u bridgemost -f`). Common causes: wrong `telegram_id` (messages from unknown users are silently ignored), bot not yet DM'd in MM, or firewall blocking MM API. |

---

## 🛡️ Security Notes

- **`config.yaml` contains secrets** — it's in `.gitignore`, never commit it
- Personal Access Tokens have your full user permissions — use a dedicated account if concerned
- Health endpoint binds to `127.0.0.1` by default (not exposed externally)
- Only Telegram users whose `telegram_id` is in config can use the bridge
- Message ID mappings stored in SQLite (local disk only, 30-day auto-prune)

---

## 📋 Changelog

| Version | Date | Feature |
|---------|------|---------|
| v0.9.5 | 2026-03-25 | Fix NameError in whisper.py finally block (GitHub #1), version alignment |
| v0.9.4 | 2026-03-25 | Smart MM→TG file relay with MIME dispatch (photo/gif/audio/voice/video) |
| v0.9.3 | 2026-03-24 | Audit fix: 3 bugs in sticker/location/poll handlers |
| v0.9.2 | 2026-03-24 | Stickers, locations, venues, polls — TG→MM |
| v0.9.1 | 2026-03-25 | Multi-user highlighted in README, unit tests added |
| v0.9.0 | 2026-03-24 | README rewrite, improved config.example.yaml with inline docs |
| v0.8.3 | 2026-03-24 | Code audit cleanup (dead code, unused imports, version alignment) |
| v0.8.2 | 2026-03-24 | Remove `/ping` (ghost typing fix), typing timeout 300→60s |
| v0.8.1 | 2026-03-24 | `/bots` and `/status` Telegram commands |
| v0.8.0 | 2026-03-24 | SQLite persistent store, WS reconnect jitter, TG rate limiter |
| v0.7.0 | 2026-03-24 | 7-bug audit fix (voice handler, file leak, typing timeout, PAT expiry) |
| v0.6.0 | 2026-03-24 | Interactive setup wizard (`bridgemost setup`) |
| v0.5.0 | 2026-03-24 | Startup resilience — token validation, DM retry, zero-channel abort |
| v0.4.0 | 2026-03-24 | Voice-to-text via Whisper API |
| v0.3.1 | 2026-03-24 | Synthetic typing indicator |
| v0.3.0 | 2026-03-24 | Multi-bot routing with `/bot` command |
| v0.2.0 | 2026-03-24 | Emoji/reaction relay |
| v0.1.1 | 2026-03-24 | Edit and delete sync (bidirectional) |
| v0.1.0 | 2026-03-24 | WebSocket transport (replaced polling) |
| v0.0.5 | 2026-03-24 | First public release |

---

## 📄 License

MIT — see [LICENSE](LICENSE)

## 🙏 Built with

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [aiohttp](https://github.com/aio-libs/aiohttp)
