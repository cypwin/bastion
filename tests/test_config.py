"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
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

    def test_missing_explicit_path_raises(self, tmp_path, monkeypatch):
        """If an explicit path is given but doesn't exist, raise FileNotFoundError."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="nonexistent.yaml"):
            load_config(tmp_path / "nonexistent.yaml")

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
        # total_vram_gb is auto-detected; falls back to 8.0 when nvidia-smi absent
        assert config.gpu.total_vram_gb > 0  # resolved by auto-detect or fallback

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
        assert config.gpu.max_vram_gb > 0  # total - headroom must be positive
        assert config.request_overrides.use_mmap is False


class TestLoadedFrom:
    def test_load_config_records_resolved_source_path(self, tmp_path):
        """/broker/catalog's registry_source depends on this being populated."""
        path = tmp_path / "broker.yaml"
        path.write_text(yaml.dump({"models": {"m:7b": {"vram_gb": 5.0}}}))
        config = load_config(path)
        assert config.loaded_from == path.resolve()

    def test_default_config_has_no_source_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)  # ensure no broker.yaml is discovered
        monkeypatch.delenv("BASTION_CONFIG", raising=False)
        config = load_config(None)
        assert config.loaded_from is None
