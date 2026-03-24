"""Tests for bridge utility functions."""

from bridgemost.bridge import split_message


class TestSplitMessage:
    """Test message splitting for Telegram's 4096 char limit."""

    def test_short_message_not_split(self):
        chunks = split_message("hello", max_len=4096)
        assert chunks == ["hello"]

    def test_exact_limit(self):
        text = "a" * 4096
        chunks = split_message(text, max_len=4096)
        assert len(chunks) == 1

    def test_splits_on_newline(self):
        text = "line1\n" * 500  # ~3000 chars
        text += "x" * 2000     # push over 4096
        chunks = split_message(text, max_len=4096)
        assert len(chunks) >= 2
        joined = "".join(chunks)
        assert len(joined) == len(text)

    def test_splits_on_space(self):
        # No newlines — should split on space
        text = ("word " * 900).strip()  # ~4500 chars
        chunks = split_message(text, max_len=4096)
        assert len(chunks) >= 2

    def test_hard_split_no_spaces(self):
        text = "a" * 8192  # No spaces or newlines
        chunks = split_message(text, max_len=4096)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4096
        assert len(chunks[1]) == 4096

    def test_empty_string(self):
        chunks = split_message("")
        assert chunks == [""]

    def test_preserves_all_content(self):
        text = "Hello world! " * 400  # ~5200 chars
        chunks = split_message(text, max_len=4096)
        assert "".join(chunks) == text

    def test_prefers_paragraph_break(self):
        # Double newline should be preferred split point
        part1 = "a" * 3000
        part2 = "b" * 3000
        text = part1 + "\n\n" + part2
        chunks = split_message(text, max_len=4096)
        assert len(chunks) >= 2
        assert chunks[0].startswith("a")
