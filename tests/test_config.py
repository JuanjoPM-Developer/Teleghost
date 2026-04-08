"""Tests for config parsing."""

import tempfile
from pathlib import Path

import pytest
import yaml
from bridgemost.config import Config, DmBridge, load_config


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

    def test_telegram_presentation_defaults(self, minimal_config):
        cfg = load_config(str(minimal_config))
        assert cfg.telegram_presentation.enabled is True
        assert cfg.telegram_presentation.suppress_internal_progress is True
        assert cfg.telegram_presentation.show_placeholder is True
        assert cfg.telegram_presentation.placeholder_text == "🧠⚡ Conectando a la red neuronal..."

    def test_telegram_presentation_custom_values(self, tmp_path):
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
            "telegram_presentation": {
                "enabled": True,
                "suppress_internal_progress": True,
                "show_placeholder": True,
                "placeholder_text": "🧠⚡ Enlace sináptico en curso...",
                "placeholder_delay_seconds": 0.4,
                "stream_final_response": True,
                "stream_chunk_chars": 90,
                "stream_edit_interval": 0.05,
            },
        }
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(data, allow_unicode=True))

        cfg = load_config(str(cfg_file))

        assert cfg.telegram_presentation.placeholder_text == "🧠⚡ Enlace sináptico en curso..."
        assert cfg.telegram_presentation.placeholder_delay_seconds == 0.4
        assert cfg.telegram_presentation.stream_chunk_chars == 90
        assert cfg.telegram_presentation.stream_edit_interval == 0.05


class TestDmBridgesConfig:
    """Test dm_bridges section parsing."""

    def _write_config(self, tmp_path, extra: dict) -> Path:
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
                    "bots": [{"name": "mybot", "mm_bot_id": "bot12345678901234567890ab", "default": True}],
                }
            ],
        }
        data.update(extra)
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump(data))
        return cfg_file

    def test_no_dm_bridges_defaults_to_empty(self, tmp_path):
        cfg_file = self._write_config(tmp_path, {})
        cfg = load_config(str(cfg_file))
        assert cfg.dm_bridges == []

    def test_parses_single_dm_bridge(self, tmp_path):
        cfg_file = self._write_config(tmp_path, {
            "dm_bridges": [
                {
                    "name": "apex",
                    "tg_bot_token": "111:APEX_TOKEN",
                    "mm_bot_id": "apexbot12345678901234567",
                }
            ]
        })
        cfg = load_config(str(cfg_file))
        assert len(cfg.dm_bridges) == 1
        bridge = cfg.dm_bridges[0]
        assert bridge.name == "apex"
        assert bridge.tg_bot_token == "111:APEX_TOKEN"
        assert bridge.mm_bot_id == "apexbot12345678901234567"

    def test_parses_multiple_dm_bridges(self, tmp_path):
        cfg_file = self._write_config(tmp_path, {
            "dm_bridges": [
                {"name": "alpha", "tg_bot_token": "111:AAA", "mm_bot_id": "botaaa"},
                {"name": "beta",  "tg_bot_token": "222:BBB", "mm_bot_id": "botbbb"},
            ]
        })
        cfg = load_config(str(cfg_file))
        assert len(cfg.dm_bridges) == 2
        names = [b.name for b in cfg.dm_bridges]
        assert "alpha" in names
        assert "beta" in names

    def test_dm_bridge_name_defaults_to_bot_id_prefix(self, tmp_path):
        cfg_file = self._write_config(tmp_path, {
            "dm_bridges": [
                {"tg_bot_token": "333:CCC", "mm_bot_id": "abcdefgh12345678"}
            ]
        })
        cfg = load_config(str(cfg_file))
        assert len(cfg.dm_bridges) == 1
        # name should default to first 8 chars of mm_bot_id
        assert cfg.dm_bridges[0].name == "abcdefgh"

    def test_dm_bridge_dataclass_fields(self):
        bridge = DmBridge(
            tg_bot_token="999:TOKEN",
            mm_bot_id="botid123",
            name="mybridge",
        )
        assert bridge.tg_bot_token == "999:TOKEN"
        assert bridge.mm_bot_id == "botid123"
        assert bridge.name == "mybridge"
