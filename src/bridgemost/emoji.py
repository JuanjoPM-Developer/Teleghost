"""Emoji mapping between Telegram Unicode and Mattermost emoji names."""

# Telegram sends Unicode emoji, Mattermost uses :name: format
# This maps common Unicode → MM name and vice versa

UNICODE_TO_MM: dict[str, str] = {
    "👍": "+1",
    "👎": "-1",
    "❤️": "heart",
    "🔥": "fire",
    "🥰": "smiling_face_with_hearts",
    "👏": "clap",
    "😁": "grin",
    "🤔": "thinking",
    "🤯": "exploding_head",
    "😱": "scream",
    "🤬": "face_with_symbols_on_mouth",
    "😢": "cry",
    "🎉": "tada",
    "🤩": "star_struck",
    "🤮": "face_vomiting",
    "💩": "hankey",
    "🙏": "pray",
    "👌": "ok_hand",
    "🕊": "dove",
    "🤡": "clown_face",
    "🥱": "yawning_face",
    "🥴": "woozy_face",
    "😍": "heart_eyes",
    "🐳": "whale",
    "🌚": "new_moon_with_face",
    "🌭": "hotdog",
    "💯": "100",
    "🤣": "rofl",
    "⚡": "zap",
    "🍌": "banana",
    "🏆": "trophy",
    "💔": "broken_heart",
    "🤨": "face_with_raised_eyebrow",
    "😐": "neutral_face",
    "🍓": "strawberry",
    "🍾": "champagne",
    "💋": "kiss",
    "🖕": "fu",
    "😈": "smiling_imp",
    "😴": "sleeping",
    "😭": "sob",
    "🤓": "nerd_face",
    "👻": "ghost",
    "👨‍💻": "technologist",
    "👀": "eyes",
    "🎃": "jack_o_lantern",
    "🙈": "see_no_evil",
    "😇": "innocent",
    "😂": "joy",
    "🤝": "handshake",
    "✍️": "writing_hand",
    "🤗": "hugs",
    "🫡": "saluting_face",
    "🎅": "santa",
    "🎄": "christmas_tree",
    "☃️": "snowman",
    "💅": "nail_care",
    "🤪": "zany_face",
    "🗿": "moyai",
    "🆒": "cool",
    "💘": "cupid",
    "🙉": "hear_no_evil",
    "🦄": "unicorn",
    "😘": "kissing_heart",
    "💊": "pill",
    "🙊": "speak_no_evil",
    "😎": "sunglasses",
    "👾": "space_invader",
    "🤷": "shrug",
    "😡": "rage",
    "🤑": "money_mouth_face",
    "🎁": "gift",
    "😏": "smirk",
    "✅": "white_check_mark",
    "❌": "x",
    "⭐": "star",
    "🚀": "rocket",
    "😀": "grinning",
    "😊": "blush",
    "😉": "wink",
    "😋": "yum",
    "😜": "stuck_out_tongue_winking_eye",
    "😄": "smile",
    "😃": "smiley",
    "😆": "laughing",
    "🙂": "slightly_smiling_face",
    "🤙": "call_me_hand",
    "💪": "muscle",
    "✨": "sparkles",
    "🎵": "musical_note",
    "💀": "skull",
    "🤖": "robot_face",
    "😳": "flushed",
    "😤": "triumph",
    "😑": "expressionless",
    "😶": "no_mouth",
    "🙄": "rolling_eyes",
    "😮": "open_mouth",
    "😧": "anguished",
    "😲": "astonished",
}

# Reverse mapping: MM name → Unicode
MM_TO_UNICODE: dict[str, str] = {v: k for k, v in UNICODE_TO_MM.items()}


def tg_emoji_to_mm(emoji: str) -> str | None:
    """Convert a TG Unicode emoji to MM emoji name. Returns None if unknown."""
    # Strip variation selectors
    clean = emoji.replace("\ufe0f", "").strip()
    return UNICODE_TO_MM.get(emoji) or UNICODE_TO_MM.get(clean)


def mm_emoji_to_tg(name: str) -> str | None:
    """Convert an MM emoji name to TG Unicode emoji. Returns None if unknown."""
    return MM_TO_UNICODE.get(name)


# Platform-agnostic aliases (preferred in core.py)
unicode_to_mm = tg_emoji_to_mm
mm_to_unicode = mm_emoji_to_tg
