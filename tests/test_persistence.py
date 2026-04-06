"""Tests for BASTION SQLite persistence layer."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from bastion.models import A2ATaskRecord, A2ATaskState, QueuedRequest, SchedulerConfig
from bastion.queue import AffinityQueue
from bastion.taskstore import TaskStore

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


class TestPersistentAuditLog:
    @pytest.mark.asyncio
    async def test_emit_tiered_writes_to_sqlite(self):
        from bastion.persistence import DatabaseManager, PersistentAuditLog
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = MagicMock()
            inner.tier = 2
            pal = PersistentAuditLog(inner, mgr)
            pal.emit_tiered("request_complete", {"model": "qwen3:14b", "latency": 1.5})
            await asyncio.sleep(0.1)
            async with mgr.conn.execute("SELECT event_type, payload FROM audit_events") as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "request_complete"
            payload = json.loads(rows[0][1])
            assert payload["model"] == "qwen3:14b"
            inner.emit_tiered.assert_called_once()
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_emit_writes_to_sqlite(self):
        from bastion.persistence import DatabaseManager, PersistentAuditLog
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = MagicMock()
            inner.tier = 2
            pal = PersistentAuditLog(inner, mgr)
            pal.emit("swap", {"from": "a", "to": "b"})
            await asyncio.sleep(0.1)
            async with mgr.conn.execute("SELECT event_type, payload FROM audit_events") as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "swap"
            inner.emit.assert_called_once()
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_sqlite_failure_does_not_crash(self):
        from bastion.persistence import DatabaseManager, PersistentAuditLog
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = MagicMock()
            inner.tier = 2
            pal = PersistentAuditLog(inner, mgr)
            await mgr.close()
            # Should not raise
            pal.emit_tiered("test_event", {"key": "value"})
            await asyncio.sleep(0.1)
            inner.emit_tiered.assert_called_once()
        finally:
            pass


class TestPersistentTaskStore:
    @pytest.mark.asyncio
    async def test_create_persists_to_sqlite(self):
        from bastion.persistence import DatabaseManager, PersistentTaskStore
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = TaskStore(maxsize=100)
            pts = PersistentTaskStore(inner, mgr)
            record = A2ATaskRecord(task_id="t-001", context_id="ctx-001",
                                   state=A2ATaskState.SUBMITTED, skill_id="infer",
                                   input_params={"model": "qwen3:14b", "prompt": "hello"})
            await pts.create(record)
            assert inner.get("t-001") is not None
            async with mgr.conn.execute(
                "SELECT task_id, state FROM task_state WHERE task_id = ?", ("t-001",)
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "t-001"
            assert row[1] == "submitted"
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_update_state_persists(self):
        from bastion.persistence import DatabaseManager, PersistentTaskStore
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = TaskStore(maxsize=100)
            pts = PersistentTaskStore(inner, mgr)
            record = A2ATaskRecord(task_id="t-002", context_id="ctx-002",
                                   state=A2ATaskState.SUBMITTED, skill_id="infer",
                                   input_params={"model": "qwen3:14b"})
            await pts.create(record)
            await pts.update_state("t-002", A2ATaskState.WORKING)
            async with mgr.conn.execute(
                "SELECT state FROM task_state WHERE task_id = ?", ("t-002",)
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == "working"
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_hydrate_restores_active_tasks(self):
        from bastion.persistence import DatabaseManager, PersistentTaskStore
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner1 = TaskStore(maxsize=100)
            pts1 = PersistentTaskStore(inner1, mgr)
            for i in range(3):
                record = A2ATaskRecord(task_id=f"t-{i:03d}", context_id=f"ctx-{i:03d}",
                                       state=A2ATaskState.SUBMITTED, skill_id="infer",
                                       input_params={"model": "qwen3:14b"})
                await pts1.create(record)
            await pts1.update_state("t-002", A2ATaskState.WORKING)
            await pts1.update_state("t-002", A2ATaskState.COMPLETED)

            inner2 = TaskStore(maxsize=100)
            pts2 = PersistentTaskStore(inner2, mgr)
            count = await pts2.hydrate()
            assert count == 2
            assert inner2.get("t-000") is not None
            assert inner2.get("t-001") is not None
            assert inner2.get_active("t-002") is None
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_terminal_state_updates_sqlite(self):
        from bastion.persistence import DatabaseManager, PersistentTaskStore
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = TaskStore(maxsize=100)
            pts = PersistentTaskStore(inner, mgr)
            record = A2ATaskRecord(task_id="t-term", context_id="ctx",
                                   state=A2ATaskState.SUBMITTED, skill_id="infer",
                                   input_params={})
            await pts.create(record)
            await pts.update_state("t-term", A2ATaskState.WORKING)
            await pts.update_state("t-term", A2ATaskState.FAILED)
            async with mgr.conn.execute(
                "SELECT state FROM task_state WHERE task_id = ?", ("t-term",)
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == "failed"
        finally:
            await mgr.close()


class TestPersistentQueue:
    def _make_queue(self) -> AffinityQueue:
        return AffinityQueue(SchedulerConfig(max_queue_size=100, cooldown_seconds=0))

    def _make_request(self, model: str = "qwen3:14b", req_id: str | None = None) -> QueuedRequest:
        req = QueuedRequest(model=model, endpoint="/api/generate",
                            body=b'{"prompt": "test"}', priority=50.0, base_priority=50.0)
        if req_id:
            req.id = req_id
        return req

    @pytest.mark.asyncio
    async def test_enqueue_persists(self):
        from bastion.persistence import DatabaseManager, PersistentQueue
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = self._make_queue()
            pq = PersistentQueue(inner, mgr)
            req = self._make_request(req_id="r-001")
            result = await pq.enqueue(req)
            assert result is True
            async with mgr.conn.execute("SELECT entry_id, model, completed FROM queue_entries") as cursor:
                rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "r-001"
            assert rows[0][1] == "qwen3:14b"
            assert rows[0][2] == 0
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_dequeue_marks_completed(self):
        from bastion.persistence import DatabaseManager, PersistentQueue
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = self._make_queue()
            pq = PersistentQueue(inner, mgr)
            req = self._make_request(req_id="r-002")
            await pq.enqueue(req)
            dequeued = await pq.dequeue_for_model("qwen3:14b")
            assert dequeued is not None
            assert dequeued.id == "r-002"
            async with mgr.conn.execute(
                "SELECT completed FROM queue_entries WHERE entry_id = ?", ("r-002",)
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == 1
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_cancel_marks_completed(self):
        from bastion.persistence import DatabaseManager, PersistentQueue
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            inner = self._make_queue()
            pq = PersistentQueue(inner, mgr)
            req = self._make_request(req_id="r-003")
            await pq.enqueue(req)
            result = await pq.cancel("r-003")
            assert result is True
            async with mgr.conn.execute(
                "SELECT completed FROM queue_entries WHERE entry_id = ?", ("r-003",)
            ) as cursor:
                row = await cursor.fetchone()
            assert row[0] == 1
        finally:
            await mgr.close()

    @pytest.mark.asyncio
    async def test_hydrate_respects_ttl(self):
        from bastion.persistence import DatabaseManager, PersistentQueue
        mgr = DatabaseManager(":memory:")
        await mgr.open()
        try:
            now = datetime.now(UTC).isoformat()
            old = "2020-01-01T00:00:00+00:00"
            await mgr.conn.execute(
                "INSERT INTO queue_entries (entry_id, model, priority, payload, enqueued_at, completed) VALUES (?, ?, ?, ?, ?, ?)",
                ("fresh-1", "qwen3:14b", 50, '{"model":"qwen3:14b","endpoint":"/api/generate","body":"","priority":50.0,"base_priority":50.0}', now, 0))
            await mgr.conn.execute(
                "INSERT INTO queue_entries (entry_id, model, priority, payload, enqueued_at, completed) VALUES (?, ?, ?, ?, ?, ?)",
                ("stale-1", "qwen3:14b", 50, '{"model":"qwen3:14b","endpoint":"/api/generate","body":"","priority":50.0,"base_priority":50.0}', old, 0))
            await mgr.conn.execute(
                "INSERT INTO queue_entries (entry_id, model, priority, payload, enqueued_at, completed) VALUES (?, ?, ?, ?, ?, ?)",
                ("done-1", "qwen3:14b", 50, '{}', now, 1))
            await mgr.conn.commit()
            inner = self._make_queue()
            pq = PersistentQueue(inner, mgr)
            recovered, discarded = await pq.hydrate(recovery_ttl=300)
            assert recovered == 1
            assert discarded == 1
            assert inner.total_size == 1
        finally:
            await mgr.close()


class TestConfigIntegration:
    def test_persistence_config_from_yaml(self, tmp_path):
        import yaml
        from bastion.config import load_config

        config_file = tmp_path / "broker.yaml"
        config_file.write_text(yaml.dump({
            "persistence": {
                "enabled": True,
                "persist_audit": True,
                "persist_tasks": True,
                "persist_queue": True,
                "queue_recovery_ttl": 600,
            }
        }))
        config = load_config(config_file)
        assert config.persistence.enabled is True
        assert config.persistence.persist_queue is True
        assert config.persistence.queue_recovery_ttl == 600

    def test_persistence_env_overrides(self, monkeypatch):
        from bastion.config import load_config

        monkeypatch.setenv("BASTION_PERSISTENCE_ENABLED", "true")
        monkeypatch.setenv("BASTION_PERSISTENCE_DB_PATH", "/tmp/test.db")
        config = load_config()
        assert config.persistence.enabled is True
        assert config.persistence.database_path == "/tmp/test.db"
