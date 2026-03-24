"""Tests for MM → Telegram MarkdownV2 conversion."""

import pytest
from bridgemost.markdown import mm_to_telegram, _escape_telegram


class TestEscapeTelegram:
    """Test Telegram special character escaping."""

    def test_no_special_chars(self):
        assert _escape_telegram("hello world") == "hello world"

    def test_escapes_dots(self):
        assert _escape_telegram("end.") == "end\\."

    def test_escapes_parens(self):
        assert _escape_telegram("(test)") == "\\(test\\)"

    def test_escapes_hash(self):
        assert _escape_telegram("#channel") == "\\#channel"

    def test_escapes_plus_minus_equals(self):
        assert _escape_telegram("a+b-c=d") == "a\\+b\\-c\\=d"

    def test_escapes_pipe(self):
        assert _escape_telegram("a|b") == "a\\|b"

    def test_escapes_exclamation(self):
        assert _escape_telegram("wow!") == "wow\\!"

    def test_escapes_curly_braces(self):
        assert _escape_telegram("{a}") == "\\{a\\}"

    def test_all_special_chars(self):
        result = _escape_telegram("_*[]()~`>#+-=|{}.!")
        # Every char should be escaped
        assert result == "\\_\\*\\[\\]\\(\\)\\~\\`\\>\\#\\+\\-\\=\\|\\{\\}\\.\\!"


class TestBold:
    """Test bold conversion: **text** → *text*."""

    def test_simple_bold(self):
        result = mm_to_telegram("**hello**")
        assert "*hello*" in result
        assert "**" not in result

    def test_bold_with_surrounding_text(self):
        result = mm_to_telegram("this is **bold** text")
        assert "*bold*" in result


class TestItalic:
    """Test italic conversion: _text_ → _text_."""

    def test_simple_italic(self):
        result = mm_to_telegram("_hello_")
        assert "_hello_" in result


class TestStrikethrough:
    """Test strikethrough: ~~text~~ → ~text~."""

    def test_simple_strike(self):
        result = mm_to_telegram("~~deleted~~")
        assert "~deleted~" in result
        assert "~~" not in result


class TestCodeBlocks:
    """Test code block preservation."""

    def test_inline_code(self):
        result = mm_to_telegram("use `pip install` here")
        assert "`pip install`" in result

    def test_code_block(self):
        result = mm_to_telegram("```python\nprint('hi')\n```")
        assert "```python" in result
        assert "print('hi')" in result

    def test_code_not_escaped(self):
        # Special chars inside code should NOT be escaped
        result = mm_to_telegram("`a+b=c`")
        assert "`a+b=c`" in result


class TestLinks:
    """Test link conversion."""

    def test_simple_link(self):
        result = mm_to_telegram("[Google](https://google.com)")
        assert "[Google](https://google.com)" in result

    def test_link_text_escaped(self):
        result = mm_to_telegram("[a+b](https://example.com)")
        assert "[a\\+b](https://example.com)" in result


class TestMixed:
    """Test combinations."""

    def test_plain_text_escaped(self):
        result = mm_to_telegram("Price: $10.99!")
        assert "\\." in result
        assert "\\!" in result

    def test_fallback_on_error(self):
        # Plain text should still work even if conversion has issues
        result = mm_to_telegram("simple text")
        assert "simple" in result

    def test_empty_string(self):
        result = mm_to_telegram("")
        assert result == ""
