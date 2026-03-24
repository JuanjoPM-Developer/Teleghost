"""Markdown converter: Mattermost → Telegram MarkdownV2.

Mattermost uses standard Markdown. Telegram MarkdownV2 requires escaping
special characters and has slightly different syntax.
"""

import re
import logging

logger = logging.getLogger("bridgemost.markdown")

# Markers for preserving formatting across escaping
_BOLD = "\x02B\x03"
_ITALIC = "\x02I\x03"
_STRIKE = "\x02S\x03"
_BLOCK_PFX = "\x02BLK"
_BLOCK_SFX = "\x03"
_CODE_PFX = "\x02COD"
_CODE_SFX = "\x03"
_LINK_PFX = "\x02LNK"
_LINK_SFX = "\x03"


def mm_to_telegram(text: str) -> str:
    """Convert Mattermost Markdown to Telegram MarkdownV2.
    
    Handles: bold, italic, strikethrough, code, code blocks, links.
    Falls back to plain text if conversion fails.
    """
    try:
        return _convert(text)
    except Exception as e:
        logger.warning("Markdown conversion failed, sending plain: %s", e)
        return _escape_telegram(text)


def _convert(text: str) -> str:
    """Core conversion logic."""
    result = text

    # 1. Preserve code blocks (don't process inside them)
    blocks: list[str] = []

    def save_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        idx = len(blocks)
        blocks.append(f"```{lang}\n{code}```")
        return f"{_BLOCK_PFX}{idx}{_BLOCK_SFX}"

    result = re.sub(r"```(\w*)\n(.*?)```", save_block, result, flags=re.DOTALL)

    # 2. Preserve inline code
    codes: list[str] = []

    def save_code(m: re.Match) -> str:
        idx = len(codes)
        codes.append(f"`{m.group(1)}`")
        return f"{_CODE_PFX}{idx}{_CODE_SFX}"

    result = re.sub(r"`([^`]+)`", save_code, result)

    # 3. Preserve links
    links: list[tuple[str, str]] = []

    def save_link(m: re.Match) -> str:
        idx = len(links)
        links.append((m.group(1), m.group(2)))
        return f"{_LINK_PFX}{idx}{_LINK_SFX}"

    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", save_link, result)

    # 4. Convert formatting to markers
    # Bold: **text** → *text* in TG
    result = re.sub(r"\*\*(.+?)\*\*", lambda m: f"{_BOLD}{m.group(1)}{_BOLD}", result)

    # Strikethrough: ~~text~~ → ~text~ in TG
    result = re.sub(r"~~(.+?)~~", lambda m: f"{_STRIKE}{m.group(1)}{_STRIKE}", result)

    # Italic: _text_ → _text_ in TG (but must not conflict with bold markers)
    result = re.sub(
        r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",
        lambda m: f"{_ITALIC}{m.group(1)}{_ITALIC}",
        result
    )

    # 5. Escape special chars in remaining text
    result = _escape_telegram(result)

    # 6. Restore formatting markers → real TG MarkdownV2 chars
    escaped_bold = _escape_telegram(_BOLD)
    escaped_italic = _escape_telegram(_ITALIC)
    escaped_strike = _escape_telegram(_STRIKE)

    result = result.replace(escaped_bold, "*")
    result = result.replace(escaped_italic, "_")
    result = result.replace(escaped_strike, "~")

    # 7. Restore links
    for i, (link_text, url) in enumerate(links):
        escaped_text = _escape_telegram(link_text)
        placeholder = _escape_telegram(f"{_LINK_PFX}{i}{_LINK_SFX}")
        result = result.replace(placeholder, f"[{escaped_text}]({url})")

    # 8. Restore code blocks and inline code (verbatim, not escaped)
    for i, block in enumerate(blocks):
        placeholder = _escape_telegram(f"{_BLOCK_PFX}{i}{_BLOCK_SFX}")
        result = result.replace(placeholder, block)

    for i, code in enumerate(codes):
        placeholder = _escape_telegram(f"{_CODE_PFX}{i}{_CODE_SFX}")
        result = result.replace(placeholder, code)

    return result


def _escape_telegram(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    result = []
    for char in text:
        if char in special:
            result.append(f"\\{char}")
        else:
            result.append(char)
    return "".join(result)
