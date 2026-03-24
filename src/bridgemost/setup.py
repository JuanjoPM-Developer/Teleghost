"""Interactive setup wizard — generates config.yaml from scratch."""

import asyncio
import getpass
import json
import sys
from pathlib import Path

import aiohttp

# ANSI colors
G = "\033[92m"  # green
Y = "\033[93m"  # yellow
R = "\033[91m"  # red
B = "\033[1m"   # bold
N = "\033[0m"   # reset


def ask(prompt: str, default: str = "") -> str:
    """Prompt user with optional default."""
    suffix = f" [{default}]" if default else ""
    val = input(f"{B}{prompt}{suffix}:{N} ").strip()
    return val or default


def ask_secret(prompt: str) -> str:
    """Prompt for a secret (masked input)."""
    return getpass.getpass(f"{B}{prompt}:{N} ").strip()


def ask_yes(prompt: str, default: bool = True) -> bool:
    """Yes/no prompt."""
    hint = "Y/n" if default else "y/N"
    val = input(f"{B}{prompt} [{hint}]:{N} ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "si", "sí")


async def validate_mm_url(url: str) -> bool:
    """Check if Mattermost is reachable."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{url}/api/v4/system/ping", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    return data.get("status") == "OK"
    except Exception:
        pass
    return False


async def mm_login(url: str, username: str, password: str) -> dict | None:
    """Login to Mattermost, return session data with token."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/api/v4/users/login",
                json={"login_id": username, "password": password},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    token = r.headers.get("Token", "")
                    return {"user": data, "token": token}
                else:
                    body = await r.json()
                    print(f"{R}Login failed ({r.status}): {body.get('message', 'Unknown error')}{N}")
    except Exception as e:
        print(f"{R}Connection error: {e}{N}")
    return None


async def create_pat(url: str, session_token: str, user_id: str, description: str) -> str | None:
    """Create a Personal Access Token for the user."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/api/v4/users/{user_id}/tokens",
                json={"description": description},
                headers={"Authorization": f"Bearer {session_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status in (200, 201):
                    data = await r.json()
                    return data.get("token")
                else:
                    body = await r.json()
                    print(f"{R}PAT creation failed ({r.status}): {body.get('message', '')}{N}")
    except Exception as e:
        print(f"{R}Error creating PAT: {e}{N}")
    return None


async def list_bots(url: str, token: str) -> list[dict]:
    """List all bots on the MM server."""
    bots = []
    page = 0
    try:
        async with aiohttp.ClientSession() as s:
            while True:
                async with s.get(
                    f"{url}/api/v4/bots?page={page}&per_page=100",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        break
                    batch = await r.json()
                    if not batch:
                        break
                    bots.extend(batch)
                    if len(batch) < 100:
                        break
                    page += 1
    except Exception as e:
        print(f"{R}Error listing bots: {e}{N}")
    return bots


async def get_user_by_username(url: str, token: str, username: str) -> dict | None:
    """Look up a user by username."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{url}/api/v4/users/username/{username}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    return await r.json()
    except Exception:
        pass
    return None


def select_bots(bots: list[dict]) -> list[dict]:
    """Interactive bot selection with numbered list."""
    if not bots:
        print(f"{Y}No bots found on this server.{N}")
        return []

    print(f"\n{B}Available bots:{N}\n")
    active_bots = [b for b in bots if not b.get("delete_at", 0)]
    for i, bot in enumerate(active_bots, 1):
        name = bot.get("username", "?")
        desc = bot.get("description", "")[:50]
        display = bot.get("display_name", "")
        label = f"{display} (@{name})" if display else f"@{name}"
        if desc:
            label += f" — {desc}"
        print(f"  {G}{i:3d}{N}. {label}")

    print(f"\n{B}Enter bot numbers separated by commas (e.g., 1,3,5){N}")
    print(f"{B}Or 'all' to select all bots:{N}")

    selection = input("> ").strip().lower()

    if selection == "all":
        return active_bots

    selected = []
    for part in selection.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(active_bots):
                selected.append(active_bots[idx])

    return selected


def generate_config(
    mm_url: str,
    tg_bot_token: str,
    tg_user_id: int,
    tg_name: str,
    mm_user_id: str,
    mm_pat: str,
    ws_bot_token: str,
    ws_bot_user_id: str,
    selected_bots: list[dict],
) -> str:
    """Generate config.yaml content."""
    lines = [
        "# BridgeMost Configuration",
        f"# Generated by bridgemost setup",
        "",
        "telegram:",
        f'  bot_token: "{tg_bot_token}"',
        "",
        "mattermost:",
        f'  url: "{mm_url}"',
        f'  bot_token: "{ws_bot_token}"',
        f'  bot_user_id: "{ws_bot_user_id}"',
        "",
        "users:",
        f"  - telegram_id: {tg_user_id}",
        f'    telegram_name: "{tg_name}"',
        f'    mm_user_id: "{mm_user_id}"',
        f'    mm_token: "{mm_pat}"',
        "    bots:",
    ]

    for i, bot in enumerate(selected_bots):
        name = bot.get("username", "bot")
        user_id = bot.get("user_id", "")
        is_default = "true" if i == 0 else "false"
        lines.append(f'      - name: "{name}"')
        lines.append(f'        mm_bot_id: "{user_id}"')
        lines.append(f'        mm_dm_channel: ""')
        lines.append(f'        default: {is_default}')

    lines.extend([
        "",
        "health:",
        "  port: 9191",
        "",
        "logging:",
        '  level: "INFO"',
        '  file: ""',
    ])

    return "\n".join(lines) + "\n"


async def run_setup():
    """Main setup wizard."""
    print(f"""
{B}╔══════════════════════════════════════╗
║       BridgeMost Setup Wizard 👻      ║
╚══════════════════════════════════════╝{N}
""")

    # Step 1: Mattermost URL
    print(f"{B}Step 1/5 — Mattermost Server{N}")
    mm_url = ask("Mattermost URL", "http://localhost:8065").rstrip("/")

    print(f"  Connecting to {mm_url}...")
    if not await validate_mm_url(mm_url):
        print(f"{R}  ✗ Cannot reach Mattermost at {mm_url}{N}")
        print(f"  Make sure the URL is correct and the server is running.")
        sys.exit(1)
    print(f"  {G}✓ Mattermost is reachable{N}\n")

    # Step 2: Login
    print(f"{B}Step 2/5 — Mattermost Login{N}")
    print(f"  We'll log in to discover your bots and create a Personal Access Token.")
    print(f"  Your password is NOT stored — only used for this session.\n")

    mm_username = ask("MM username")
    mm_password = ask_secret("MM password")

    session = await mm_login(mm_url, mm_username, mm_password)
    if not session:
        print(f"\n{R}  ✗ Login failed. Check your credentials.{N}")
        sys.exit(1)

    mm_user = session["user"]
    mm_user_id = mm_user["id"]
    mm_display = mm_user.get("first_name", mm_username)
    print(f"  {G}✓ Logged in as @{mm_user.get('username', mm_username)} ({mm_display}){N}\n")

    # Create PAT
    print(f"  Creating Personal Access Token...")
    pat = await create_pat(mm_url, session["token"], mm_user_id, "bridgemost-bridge")
    if not pat:
        print(f"{R}  ✗ Could not create PAT.{N}")
        print(f"  Make sure EnableUserAccessTokens is enabled in System Console.")
        print(f"  Or create one manually: Profile → Security → Personal Access Tokens")
        pat = ask_secret("Paste your PAT manually (or Ctrl+C to abort)")
        if not pat:
            sys.exit(1)
    else:
        print(f"  {G}✓ PAT created successfully{N}\n")

    # Step 3: Discover and select bots
    print(f"{B}Step 3/5 — Select Bots{N}")
    print(f"  Discovering bots on your server...")

    bots = await list_bots(mm_url, session["token"])
    if not bots:
        print(f"{Y}  No bots found. You can add them to config.yaml manually later.{N}")
        selected_bots = []
    else:
        print(f"  Found {len(bots)} bot(s).")
        selected_bots = select_bots(bots)

    if selected_bots:
        print(f"\n  {G}✓ Selected {len(selected_bots)} bot(s):{N}")
        for bot in selected_bots:
            print(f"    • @{bot.get('username', '?')}")
        # Use first selected bot for WebSocket auth
        ws_bot_token = ""
        ws_bot_user_id = selected_bots[0].get("user_id", "")
        print(f"\n  {Y}Note: You'll need a bot token for WebSocket auth.{N}")
        print(f"  Find it in Integrations → Bot Accounts → pick a bot → token")
        ws_bot_token = ask_secret("Bot token (for WebSocket reading)")
    else:
        ws_bot_token = ""
        ws_bot_user_id = ""
    print()

    # Step 4: Telegram
    print(f"{B}Step 4/5 — Telegram{N}")
    print(f"  Create a bot via @BotFather if you haven't already.\n")

    tg_bot_token = ask_secret("Telegram bot token (from @BotFather)")
    tg_user_id_str = ask("Your Telegram user ID (from @userinfobot)")
    tg_user_id = int(tg_user_id_str) if tg_user_id_str.isdigit() else 0
    tg_name = ask("Your name (for logs)", mm_display)
    print()

    # Step 5: Generate config
    print(f"{B}Step 5/5 — Generate Config{N}")

    config_content = generate_config(
        mm_url=mm_url,
        tg_bot_token=tg_bot_token,
        tg_user_id=tg_user_id,
        tg_name=tg_name,
        mm_user_id=mm_user_id,
        mm_pat=pat,
        ws_bot_token=ws_bot_token,
        ws_bot_user_id=ws_bot_user_id,
        selected_bots=selected_bots,
    )

    config_path = Path("config.yaml")
    if config_path.exists():
        if not ask_yes(f"config.yaml already exists. Overwrite?", default=False):
            alt_path = Path("config.generated.yaml")
            config_path = alt_path
            print(f"  Saving to {alt_path} instead.")

    config_path.write_text(config_content)
    print(f"\n  {G}✓ Config written to {config_path}{N}")

    print(f"""
{B}╔══════════════════════════════════════╗
║          Setup Complete! 🎉          ║
╚══════════════════════════════════════╝{N}

  Start BridgeMost:
    {G}python3 -m bridgemost{N}

  Or with systemd:
    {G}sudo systemctl start bridgemost{N}

  Send a message via Telegram to your bot —
  it should appear in Mattermost as your real user!
""")


def main():
    """Entry point for `bridgemost setup` or `python -m bridgemost.setup`."""
    asyncio.run(run_setup())


if __name__ == "__main__":
    main()
