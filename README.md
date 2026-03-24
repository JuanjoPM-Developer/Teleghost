# TeleGhost 👻

**Telegram ↔ Mattermost Transparent Bridge**

TeleGhost makes your Telegram messages appear natively in Mattermost — as your real user, with your avatar and name. Bot responses from Mattermost are relayed back to Telegram instantly.

Unlike Matterbridge, webhooks, or n8n integrations that post with prefixes like `[User]` or from a bot account, TeleGhost posts as **your actual Mattermost user** via Personal Access Token. Nobody in Mattermost can tell you're writing from Telegram.

## ✨ Features

- **Transparent identity** — Messages appear as your real MM user (avatar, name, everything)
- **Full media support** — Photos, documents, audio, video, voice messages — bidirectional
- **Voice-to-text** — Voice messages auto-transcribed via Whisper API, posted as `🎤 text`
- **Multi-bot routing** — Talk to multiple MM bots; switch with `/bot name`
- **Real-time WebSocket** — MM responses arrive instantly via WebSocket (no polling)
- **Edit & delete sync** — Edits and deletes in both directions stay in sync
- **Reactions sync** — Emoji reactions synced between Telegram and Mattermost
- **Synthetic typing** — "Bot is typing..." indicator in Telegram while bots process
- **Markdown conversion** — MM markdown automatically converted to Telegram MarkdownV2
- **Startup resilience** — Token validation, DM discovery retry with backoff, zero-channel abort
- **Retry with backoff** — Exponential backoff on failures, error notifications via Telegram
- **Health endpoint** — HTTP health check at `/health` for monitoring
- **Message splitting** — Long messages automatically split for Telegram's 4096-char limit
- **Lightweight** — ~55MB RAM, ~250ms latency, asyncio-based

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- A Mattermost server (self-hosted) with `EnableUserAccessTokens` enabled
- A Telegram bot token (free from [@BotFather](https://t.me/BotFather))
- A Mattermost Personal Access Token (PAT) for your user account

### Installation

```bash
git clone https://github.com/JuanjoPM-Developer/Teleghost.git
cd Teleghost
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Configuration

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your tokens (see Configuration section below)
```

### Run

```bash
python3 -m teleghost
```

### Systemd Service (recommended)

```bash
sudo cp teleghost.service.example /etc/systemd/system/teleghost.service
# Edit paths in the service file
sudo systemctl enable --now teleghost
```

## ⚙️ Configuration

```yaml
telegram:
  bot_token: "YOUR_TELEGRAM_BOT_TOKEN"

mattermost:
  url: "http://localhost:8065"
  bot_token: "MM_BOT_TOKEN"          # Any bot token (for WebSocket auth)
  bot_user_id: "BOT_USER_ID"         # User ID of the bot above

users:
  - telegram_id: 123456789           # Your Telegram user ID
    telegram_name: "Your Name"
    mm_user_id: "your_mm_user_id"
    mm_token: "YOUR_PERSONAL_ACCESS_TOKEN"
    bots:
      - name: "MyBot"
        mm_bot_id: "bot_user_id"
        mm_dm_channel: ""             # Leave empty for auto-discovery
        default: true
      - name: "AnotherBot"
        mm_bot_id: "another_bot_user_id"

# Optional: Voice-to-text transcription
voice_to_text:
  enabled: false
  url: "http://localhost:9000/asr"    # Whisper API endpoint
  api_key: ""                         # Optional API key
  model: "large-v3"
  language: "auto"                    # Or "es", "en", etc.
  keep_audio: true                    # Also attach audio file in MM

health:
  port: 9191

logging:
  level: "INFO"
  file: ""                            # Path to log file, or "" for stdout
```

### Getting your tokens

1. **Telegram Bot Token**: Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. **Telegram User ID**: Message [@userinfobot](https://t.me/userinfobot)
3. **MM Personal Access Token**: Mattermost → Profile → Security → Personal Access Tokens
4. **MM User/Bot IDs**: `mmctl user search <username>` or Mattermost API `GET /api/v4/users/username/<name>`
5. **MM Bot Token**: Integrations → Bot Accounts → pick any bot → copy token

> **Note:** `EnableUserAccessTokens` must be enabled in your Mattermost System Console (or via `mmctl config set ServiceSettings.EnableUserAccessTokens true`).

### DM Channel Auto-Discovery

You can leave `mm_dm_channel` empty for each bot — TeleGhost will automatically discover the DM channel at startup. If discovery fails (e.g., you've never DM'd that bot), TeleGhost retries 3 times with exponential backoff before giving up on that bot.

## 🤖 Multi-Bot Routing

Configure multiple bots and switch between them from Telegram:

```
/bot           → List available bots and show active one
/bot MyBot     → Switch to MyBot
/bot AnotherBot → Switch to AnotherBot
```

Messages from the active bot go directly to your Telegram chat. Messages from non-active bots are prefixed with `[BotName]` for clarity.

## 🎤 Voice-to-Text

When `voice_to_text.enabled` is `true`, TeleGhost sends voice messages to a Whisper-compatible API for transcription. The transcribed text is posted to Mattermost as:

> 🎤 Hello, this is what I said in the voice message

If `keep_audio` is `true`, the original audio file is also attached.

Compatible APIs: [OpenAI Whisper](https://github.com/openai/whisper), [faster-whisper-server](https://github.com/fedirz/faster-whisper-server), any endpoint accepting `POST /asr` with `multipart/form-data`.

## ✏️ Edit & Delete Sync

- **Edit** a message in Telegram → edit updates in Mattermost
- **Bot edits** a response in Mattermost → edit updates in your Telegram chat
- **Bot deletes** a response in Mattermost → message deleted in your Telegram chat

TeleGhost maintains a bidirectional message ID map (last 2000 messages) for sync.

## 😀 Reactions

- **React** to a message in Telegram → reaction appears in Mattermost
- **Bot reacts** in Mattermost → reaction appears in your Telegram chat

Emoji mapping between Telegram unicode and Mattermost named emojis is handled automatically.

## 📊 Health Endpoint

```bash
curl http://localhost:9191/health
```

```json
{
  "status": "ok",
  "version": "0.5.0",
  "transport": "websocket",
  "uptime": "2h15m30s",
  "uptime_seconds": 8130,
  "messages": {
    "tg_to_mm": 42,
    "mm_to_tg": 38,
    "errors": 0
  },
  "last_activity": {
    "tg_msg_ago": 15,
    "mm_msg_ago": 8
  }
}
```

## 🏗️ Architecture

```
┌──────────┐         ┌──────────────┐         ┌──────────────┐
│ Telegram │◄───────►│  TeleGhost   │◄───────►│  Mattermost  │
│  (User)  │  Bot    │  (Bridge)    │  WS +   │  (Bot + User)│
└──────────┘  API    └──────────────┘  REST   └──────────────┘
```

1. **TG → MM**: User sends message via Telegram → TeleGhost posts to MM as user's real account (PAT)
2. **MM → TG**: Bot responds in MM → WebSocket delivers event instantly → TeleGhost relays to Telegram
3. **Startup**: Validates tokens → discovers DM channels with retry → opens WebSocket → starts Telegram polling

## 🛡️ Startup Resilience (v0.5.0)

TeleGhost validates everything before accepting messages:

1. **Token pre-validation** — all user PATs are tested against MM API at boot. Fails immediately with clear error if invalid.
2. **DM discovery retry** — 3 attempts with exponential backoff (2s → 4s → 8s) per bot channel.
3. **Channel ID validation** — rejects malformed channel IDs (must be exactly 26 alphanumeric chars).
4. **Zero-channel abort** — if no DM channels could be discovered, terminates with `FATAL` instead of running silently broken.

## 📋 Changelog

| Version | Feature |
|---------|---------|
| v0.5.0 | Startup resilience — token validation, DM retry, zero-channel abort |
| v0.4.0 | Voice-to-text transcription via Whisper API |
| v0.3.1 | Synthetic typing indicator |
| v0.3.0 | Multi-bot routing with `/bot` command |
| v0.1.2 | Reactions sync (Telegram ↔ Mattermost) |
| v0.1.1 | Edit and delete sync (bidirectional) |
| v0.1.0 | WebSocket transport (replaced polling) |
| v0.0.5 | First public release |

## 🛡️ Security

- `config.yaml` contains secrets — **never commit it** (it's in `.gitignore`)
- Personal Access Tokens have full user permissions — use dedicated accounts if concerned
- Health endpoint binds to `127.0.0.1` by default
- Telegram bot restricted via `telegram_id` allowlist in config
- No data is stored persistently — message ID maps are in-memory only

## 📄 License

MIT — see [LICENSE](LICENSE)

## 🙏 Credits

Built with:
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [aiohttp](https://github.com/aio-libs/aiohttp)
