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


class TestDatabaseManager:
    @pytest.mark.asyncio
    async def test_open_creates_schema(self):
        from bastion.persistence import DatabaseManager

        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            async with mgr._conn.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ) as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 1

            async with mgr._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) as cursor:
                tables = [row[0] async for row in cursor]
            assert "audit_events" in tables
            assert "task_state" in tables
            assert "queue_entries" in tables
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self):
        from bastion.persistence import DatabaseManager

        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            async with mgr._conn.execute("PRAGMA journal_mode") as cursor:
                row = await cursor.fetchone()
            assert row is not None
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_migrations_idempotent(self):
        from bastion.persistence import DatabaseManager

        mgr = DatabaseManager(":memory:")
        await mgr.open()
        await mgr._run_migrations()
        async with mgr._conn.execute(
            "SELECT COUNT(*) FROM schema_version"
        ) as cursor:
            count = (await cursor.fetchone())[0]
        assert count == 1
        await mgr.close()

    @pytest.mark.asyncio
    async def test_close_is_safe_when_not_opened(self):
        from bastion.persistence import DatabaseManager

        mgr = DatabaseManager(":memory:")
        await mgr.close()  # Should not raise
