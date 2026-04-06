"""Tests for BASTION SQLite persistence layer."""

from __future__ import annotations

import pytest

# Skip entire module if aiosqlite not installed
aiosqlite = pytest.importorskip("aiosqlite")


class TestPersistenceConfig:
    def test_defaults(self):
        from bastion.models import PersistenceConfig

        cfg = PersistenceConfig()
        assert cfg.enabled is False
        assert cfg.database_path == ""
        assert cfg.persist_audit is True
        assert cfg.persist_tasks is True
        assert cfg.persist_queue is False
        assert cfg.queue_recovery_ttl == 300

    def test_broker_config_has_persistence(self):
        from bastion.models import BrokerConfig

        config = BrokerConfig()
        assert hasattr(config, "persistence")
        assert config.persistence.enabled is False

    def test_database_path_function(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))
        from bastion.paths import database_path

        result = database_path()
        assert result == tmp_path / "bastion.db"
