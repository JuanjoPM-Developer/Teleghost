"""Tests for config parsing."""

import tempfile
from pathlib import Path

import pytest
import yaml
from bridgemost.config import Config, load_config


@pytest.fixture
def minimal_config(tmp_path):
    """Create a minimal valid config file."""
    data = {
        "telegram": {"bot_token": "123:ABC"},
        "mattermost": {
            "url": "http://localhost:8065",
            "bot_token": "sometoken",
            "bot_user_id": "abcdef1234567890abcdef1234",
        },
        "users": [
            {
                "telegram_id": 12345,
                "telegram_name": "TestUser",
                "mm_user_id": "user1234567890abcdef123456",
                "mm_token": "pat-abc123",
                "bots": [
                    {
                        "name": "mybot",
                        "mm_bot_id": "bot12345678901234567890ab",
                        "default": True,
                    }
                ],
            }
        ],
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(data))
    return cfg_file


class TestConfigLoad:
    """Test config loading from YAML."""

    def test_loads_minimal(self, minimal_config):
        cfg = load_config(str(minimal_config))
        assert cfg.tg_bot_token == "123:ABC"
        assert cfg.mm_url == "http://localhost:8065"
        assert len(cfg.users) == 1
        assert cfg.users[0].telegram_id == 12345
        assert len(cfg.users[0].bots) == 1
        assert cfg.users[0].bots[0].name == "mybot"

    def test_get_user_by_tg_id(self, minimal_config):
        cfg = load_config(str(minimal_config))
        user = cfg.get_user_by_tg_id(12345)
        assert user is not None
        assert user.telegram_name == "TestUser"

    def test_unknown_tg_id_returns_none(self, minimal_config):
        cfg = load_config(str(minimal_config))
        assert cfg.get_user_by_tg_id(99999) is None

    def test_health_port_default(self, minimal_config):
        cfg = load_config(str(minimal_config))
        assert cfg.health_port == 9191

    def test_missing_file_raises(self):
        with pytest.raises(Exception):
            Config.from_file("/nonexistent/config.yaml")
