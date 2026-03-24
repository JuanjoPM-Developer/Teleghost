"""Tests for emoji mapping between Telegram and Mattermost."""

from bridgemost.emoji import tg_emoji_to_mm, mm_emoji_to_tg, UNICODE_TO_MM, MM_TO_UNICODE


class TestTgToMm:
    """Test Telegram Unicode → MM emoji name."""

    def test_thumbs_up(self):
        assert tg_emoji_to_mm("👍") == "+1"

    def test_heart(self):
        assert tg_emoji_to_mm("❤️") == "heart"

    def test_fire(self):
        assert tg_emoji_to_mm("🔥") == "fire"

    def test_rocket(self):
        assert tg_emoji_to_mm("🚀") == "rocket"

    def test_unknown_returns_none(self):
        assert tg_emoji_to_mm("🦑") is None

    def test_variation_selector_stripped(self):
        # Heart with variation selector should still match
        assert tg_emoji_to_mm("❤\ufe0f") == "heart"


class TestMmToTg:
    """Test MM emoji name → Telegram Unicode."""

    def test_plus_one(self):
        assert mm_emoji_to_tg("+1") == "👍"

    def test_heart(self):
        assert mm_emoji_to_tg("heart") == "❤️"

    def test_tada(self):
        assert mm_emoji_to_tg("tada") == "🎉"

    def test_unknown_returns_none(self):
        assert mm_emoji_to_tg("nonexistent_emoji") is None


class TestBidirectional:
    """Test that mappings are consistent."""

    def test_all_unicode_map_back(self):
        """Every Unicode→MM entry should reverse-map back."""
        for emoji, name in UNICODE_TO_MM.items():
            assert MM_TO_UNICODE[name] == emoji, f"Reverse mismatch: {name} → {MM_TO_UNICODE.get(name)} != {emoji}"

    def test_mapping_count(self):
        """Both maps should have the same number of entries."""
        assert len(UNICODE_TO_MM) == len(MM_TO_UNICODE)

    def test_roundtrip(self):
        """TG→MM→TG should return the original emoji."""
        for emoji in ["👍", "🔥", "🚀", "😂", "💀"]:
            mm_name = tg_emoji_to_mm(emoji)
            assert mm_name is not None
            back = mm_emoji_to_tg(mm_name)
            assert back == emoji
