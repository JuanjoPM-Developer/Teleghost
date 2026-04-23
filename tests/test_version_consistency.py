"""Version consistency tests."""

from pathlib import Path

from bridgemost import __version__


def test_version_is_2_2_6():
    assert __version__ == "2.2.6"


def test_changelog_mentions_2_2_6():
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    assert "## v2.2.6" in changelog
