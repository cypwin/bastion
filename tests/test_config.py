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


class TestObservabilityConfigLoading:
    """``observability:`` block wiring through ``load_config`` (spec 4.8, T2-config).

    These exercise the YAML-file -> ``load_config`` -> ``BrokerConfig`` path
    (not direct ``BrokerConfig(**)`` construction, which is covered in
    ``test_observability_models.py``).  The contract: a present block populates
    ``ObservabilityConfig`` + nested ``CorrelationConfig`` and is NOT silently
    dropped; an absent block yields defaults via the default factory; an unknown
    sub-key behaves per the existing config strictness (Pydantic ``extra=ignore``,
    consistent with every other ``BrokerConfig`` sub-model).
    """

    def test_present_block_parses_into_model(self, tmp_path):
        """A YAML observability block populates ObservabilityConfig fields."""
        cfg = {
            "observability": {
                "churn_threshold": 9,
                "process_watchlist": ["ollama", "pid:1234"],
                "ecc_enabled": True,
                "cpu_sensor_name": "zenpower",
                "psi_io_full_warn_pct": 7.5,
                "psi_io_full_crit_pct": 40.0,
            }
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        obs = config.observability
        assert obs.churn_threshold == 9
        assert obs.process_watchlist == ["ollama", "pid:1234"]
        assert obs.ecc_enabled is True
        assert obs.cpu_sensor_name == "zenpower"
        assert obs.psi_io_full_warn_pct == 7.5
        assert obs.psi_io_full_crit_pct == 40.0

    def test_present_nested_correlation_block_parses(self, tmp_path):
        """A nested observability.correlation block populates CorrelationConfig."""
        cfg = {
            "observability": {
                "correlation": {
                    "ring_maxlen": 1024,
                    "ring_tail_in_snapshot": 16,
                    "contention_block_write_mb_s_threshold": 2000.0,
                    "cpu_safe_ceiling_c": 90.0,
                    "gpu_safe_ceiling_c": 93.0,
                    "risk_weights": {
                        "vram_headroom": 0.30,
                        "thermal_headroom": 0.20,
                        "swap_rate": 0.20,
                        "thrashing": 0.20,
                        "memory_psi": 0.10,
                    },
                }
            }
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        corr = config.observability.correlation
        assert corr.ring_maxlen == 1024
        assert corr.ring_tail_in_snapshot == 16
        assert corr.contention_block_write_mb_s_threshold == 2000.0
        assert corr.cpu_safe_ceiling_c == 90.0
        assert corr.gpu_safe_ceiling_c == 93.0
        assert corr.risk_weights["vram_headroom"] == 0.30

    def test_absent_block_yields_defaults(self, tmp_path):
        """A YAML without an observability block falls back to defaults."""
        cfg = {"server": {"port": 11434}}
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        obs = config.observability
        # Documented defaults from spec 4.8.
        assert obs.churn_threshold == 5
        assert obs.process_watchlist == []
        assert obs.ecc_enabled is False
        assert obs.cpu_sensor_name is None
        assert obs.psi_io_full_warn_pct == 5.0
        assert obs.psi_io_full_crit_pct == 25.0
        # Nested correlation defaults too.
        assert obs.correlation.ring_maxlen == 512
        assert obs.correlation.contention_block_write_mb_s_threshold == 200.0
        assert obs.correlation.gpu_safe_ceiling_c is None

    def test_absent_observability_key_entirely(self, tmp_path, monkeypatch):
        """No config file at all still yields a populated observability default."""
        monkeypatch.chdir(tmp_path)
        config = load_config(None)
        assert config.observability.churn_threshold == 5
        assert config.observability.correlation.ring_maxlen == 512

    def test_unknown_subkey_is_ignored(self, tmp_path):
        """An unknown observability sub-key behaves per existing config strictness.

        Every BrokerConfig sub-model uses Pydantic v2's default ``extra=ignore``;
        an unrecognized key is dropped rather than raising, and known siblings in
        the same block still parse.
        """
        cfg = {
            "observability": {
                "made_up_future_key": 123,
                "churn_threshold": 4,
            }
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        # Known sibling still parsed; unknown key silently dropped.
        assert config.observability.churn_threshold == 4
        assert not hasattr(config.observability, "made_up_future_key")

    def test_unknown_correlation_subkey_is_ignored(self, tmp_path):
        """Unknown keys under observability.correlation are also ignored."""
        cfg = {
            "observability": {
                "correlation": {
                    "made_up_corr_key": "x",
                    "ring_maxlen": 256,
                }
            }
        }
        path = tmp_path / "test.yaml"
        path.write_text(yaml.dump(cfg))

        config = load_config(path)
        assert config.observability.correlation.ring_maxlen == 256
        assert not hasattr(config.observability.correlation, "made_up_corr_key")


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
