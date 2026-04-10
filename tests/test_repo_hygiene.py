"""Repository hygiene tests for secrets/backups safety."""

from pathlib import Path


def test_gitignore_ignores_config_backups():
    content = Path(".gitignore").read_text(encoding="utf-8")
    assert "config.yaml.bak*" in content
