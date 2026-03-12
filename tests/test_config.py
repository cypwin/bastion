"""Tests for configuration loading."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from bastion.config import load_config
from bastion.models import BrokerConfig


class TestLoadConfig:
    def test_defaults_when_no_file(self, tmp_path, monkeypatch):
        """With no config file found, returns sensible defaults."""
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert isinstance(config, BrokerConfig)
        assert config.ollama.port == 11435
        assert config.server.port == 11434

    def test_explicit_path(self, tmp_path):
        """Load from an explicit path."""
        cfg = {
            "ollama": {"port": 9999},
            "server": {"port": 8888},
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.ollama.port == 9999
        assert config.server.port == 8888

    def test_missing_explicit_path_falls_back(self, tmp_path, monkeypatch):
        """If explicit path doesn't exist, fall back to defaults."""
        monkeypatch.chdir(tmp_path)
        config = load_config(tmp_path / "nonexistent.yaml")
        assert isinstance(config, BrokerConfig)

    def test_models_section_parsed(self, tmp_path):
        """Models section converts dicts to ModelInfo objects."""
        cfg = {
            "models": {
                "test:1b": {"vram_gb": 1.5, "tags": ["fast"]},
                "embed": {"vram_gb": 0.3, "always_allowed": True},
            }
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.models["test:1b"].vram_gb == 1.5
        assert config.models["test:1b"].tags == ["fast"]
        assert config.models["embed"].always_allowed is True

    def test_partial_config_gets_defaults(self, tmp_path):
        """Config with only some fields fills in defaults for the rest."""
        cfg = {"scheduler": {"cooldown_seconds": 5.0}}
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.scheduler.cooldown_seconds == 5.0
        assert config.scheduler.aging_rate == 2.0  # default
        assert config.gpu.total_vram_gb == 32.0  # default

    def test_admin_port_default_zero(self, tmp_path, monkeypatch):
        """admin_port defaults to 0 (disabled / same port as proxy)."""
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert config.server.admin_port == 0
        assert config.server.two_port_mode is False

    def test_admin_port_from_yaml(self, tmp_path):
        """admin_port can be set via YAML config."""
        cfg = {"server": {"port": 11434, "admin_port": 9999}}
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.server.admin_port == 9999
        assert config.server.two_port_mode is True

    def test_admin_port_same_as_proxy_disables_two_port(self, tmp_path):
        """admin_port == port means single-port mode (backward compatible)."""
        cfg = {"server": {"port": 11434, "admin_port": 11434}}
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.server.admin_port == 11434
        assert config.server.two_port_mode is False

    def test_admin_port_zero_disables_two_port(self, tmp_path):
        """admin_port = 0 means single-port mode."""
        cfg = {"server": {"port": 11434, "admin_port": 0}}
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.server.two_port_mode is False

    def test_real_broker_yaml(self):
        """Smoke test: load the actual config/broker.yaml."""
        path = Path("config/broker.yaml")
        if not path.exists():
            return  # Skip if not running from project root
        config = load_config(path)
        assert len(config.models) >= 10
        assert config.gpu.max_vram_gb == 24.0  # 32 total - 8 headroom
        assert config.request_overrides.use_mmap is False
