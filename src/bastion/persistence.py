"""Optional SQLite persistence for BASTION.

Provides DatabaseManager (connection lifecycle + migrations) and persistent
wrappers for AuditLogger, TaskStore, and AffinityQueue. All wrappers use
composition: reads stay in-memory, writes dual-write to SQLite.

Requires: pip install bastion-broker[persistence]  (aiosqlite)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from bastion.models import A2ATaskRecord, A2ATaskState

logger = logging.getLogger(__name__)

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tier INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            model TEXT,
            client_ip TEXT,
            content_hash TEXT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_events(event_type)",
        """CREATE TABLE IF NOT EXISTS task_state (
            task_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            model TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_task_state ON task_state(state)",
        """CREATE TABLE IF NOT EXISTS queue_entries (
            entry_id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            priority INTEGER NOT NULL,
            payload TEXT NOT NULL,
            enqueued_at TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_queue_pending ON queue_entries(completed, enqueued_at)",
    ],
}


class DatabaseManager:
    """Owns the aiosqlite connection lifecycle and runs migrations."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Open connection, enable WAL mode, run migrations."""
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._ensure_schema_version_table()
        await self._run_migrations()

    async def close(self) -> None:
        """Close connection gracefully. Safe to call if not opened."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the active connection. Raises RuntimeError if not opened."""
        if self._conn is None:
            raise RuntimeError("DatabaseManager not opened")
        return self._conn

    async def _ensure_schema_version_table(self) -> None:
        """Create the schema_version table if it does not exist."""
        await self.conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )"""
        )
        await self.conn.commit()

    async def _run_migrations(self) -> None:
        """Apply any unapplied migrations in ascending version order."""
        async with self.conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ) as cursor:
            applied = {row[0] async for row in cursor}

        for version in sorted(MIGRATIONS.keys()):
            if version in applied:
                continue
            logger.info("Applying migration v%d", version)
            for sql in MIGRATIONS[version]:
                await self.conn.execute(sql)
            await self.conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(UTC).isoformat()),
            )
            await self.conn.commit()
            logger.info("Migration v%d applied", version)


class PersistentAuditLog:
    """Wraps an AuditLogger to dual-write audit events to SQLite."""

    def __init__(self, inner: Any, db: DatabaseManager) -> None:
        self._inner = inner
        self._db = db

    @property
    def tier(self) -> int:
        return self._inner.tier

    @property
    def logger(self) -> Any:
        return self._inner.logger

    def emit(self, event: str, details: dict[str, Any]) -> None:
        """Emit audit event: JSONL first, then fire-and-forget SQLite insert."""
        self._inner.emit(event, details)
        self._schedule_insert(
            event_type=event, tier=self.tier, payload=details,
            model=details.get("model"), client_ip=details.get("client_ip"),
            content_hash=details.get("content_hash"),
        )

    def emit_tiered(self, event_type: str, data: dict[str, Any],
                    tier_override: int | None = None, auth_token: str | None = None,
                    a2a_identity: dict[str, Any] | None = None, source_ip: str | None = None,
                    prompt: str | None = None, response: str | None = None) -> None:
        """Emit tiered audit event: JSONL first, then fire-and-forget SQLite."""
        self._inner.emit_tiered(event_type=event_type, data=data, tier_override=tier_override,
                                auth_token=auth_token, a2a_identity=a2a_identity,
                                source_ip=source_ip, prompt=prompt, response=response)
        effective_tier = tier_override if tier_override is not None else self.tier
        self._schedule_insert(event_type=event_type, tier=effective_tier, payload=data,
                              model=data.get("model"), client_ip=source_ip,
                              content_hash=data.get("content_hash"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _schedule_insert(self, event_type: str, tier: int, payload: dict[str, Any],
                         model: str | None = None, client_ip: str | None = None,
                         content_hash: str | None = None) -> None:
        """Fire-and-forget async insert into audit_events."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._insert(event_type, tier, payload, model, client_ip, content_hash))

    async def _insert(self, event_type: str, tier: int, payload: dict[str, Any],
                      model: str | None, client_ip: str | None, content_hash: str | None) -> None:
        try:
            await self._db.conn.execute(
                """INSERT INTO audit_events
                   (timestamp, tier, event_type, model, client_ip, content_hash, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(UTC).isoformat(), tier, event_type, model,
                 client_ip, content_hash, json.dumps(payload)),
            )
            await self._db.conn.commit()
        except Exception as e:
            logger.warning("SQLite audit write failed (JSONL is safety net): %s", e)


class PersistentTaskStore:
    """Wraps TaskStore to dual-write task state to SQLite."""

    def __init__(self, inner: Any, db: DatabaseManager) -> None:
        self._inner = inner
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # Delegated read methods
    def get(self, task_id: str) -> Any:
        return self._inner.get(task_id)
    def get_active(self, task_id: str) -> A2ATaskRecord | None:
        return self._inner.get_active(task_id)
    def has_task(self, task_id: str) -> bool:
        return self._inner.has_task(task_id)
    def active_count(self) -> int:
        return self._inner.active_count()
    def count_by_state(self, state: str) -> int:
        return self._inner.count_by_state(state)
    def stats(self) -> dict:
        return self._inner.stats()
    def subscribe(self, task_id: str) -> Any:
        return self._inner.subscribe(task_id)
    def unsubscribe(self, task_id: str, queue: Any) -> None:
        return self._inner.unsubscribe(task_id, queue)
    async def notify_subscribers(self, task_id: str, event: dict) -> None:
        return await self._inner.notify_subscribers(task_id, event)
    def start_cleanup(self) -> None:
        self._inner.start_cleanup()
    def stop_cleanup(self) -> None:
        self._inner.stop_cleanup()

    # Write methods (dual-write)
    async def create(self, record: A2ATaskRecord) -> str:
        task_id = self._inner.create(record)
        try:
            now = datetime.now(UTC).isoformat()
            await self._db.conn.execute(
                """INSERT OR REPLACE INTO task_state
                   (task_id, state, model, priority, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record.task_id, record.state.value, record.input_params.get("model"),
                 0, json.dumps(record.model_dump(), default=str), now, now),
            )
            await self._db.conn.commit()
        except Exception as e:
            logger.warning("SQLite task persist failed for %s: %s", record.task_id, e)
        return task_id

    async def update_state(self, task_id: str, new_state: A2ATaskState) -> A2ATaskRecord:
        record = self._inner.update_state(task_id, new_state)
        try:
            now = datetime.now(UTC).isoformat()
            await self._db.conn.execute(
                "UPDATE task_state SET state = ?, payload = ?, updated_at = ? WHERE task_id = ?",
                (new_state.value, json.dumps(record.model_dump(), default=str), now, task_id),
            )
            await self._db.conn.commit()
        except Exception as e:
            logger.warning("SQLite task state update failed for %s: %s", task_id, e)
        return record

    async def hydrate(self) -> int:
        """Load active (non-terminal) tasks from SQLite into in-memory store. Returns count."""
        terminal = {
            A2ATaskState.COMPLETED.value,
            A2ATaskState.FAILED.value,
            A2ATaskState.CANCELED.value,
        }
        count = 0
        query = "SELECT task_id, state, payload FROM task_state"
        async with self._db.conn.execute(query) as cursor:
            async for row in cursor:
                task_id, state, payload_json = row
                if state in terminal:
                    continue
                try:
                    data = json.loads(payload_json)
                    record = A2ATaskRecord(**data)
                    self._inner.create(record)
                    count += 1
                except Exception as e:
                    logger.warning("Failed to hydrate task %s: %s", task_id, e)
        if count:
            logger.info("Hydrated %d active tasks from SQLite", count)
        return count


class PersistentQueue:
    """Wraps AffinityQueue to dual-write queue entries to SQLite."""

    def __init__(self, inner: Any, db: DatabaseManager) -> None:
        self._inner = inner
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    # Delegated read-only properties and methods
    @property
    def total_size(self) -> int:
        return self._inner.total_size
    @property
    def is_empty(self) -> bool:
        return self._inner.is_empty
    @property
    def config(self) -> Any:
        return self._inner.config
    def queue_depth_by_model(self) -> dict[str, int]:
        return self._inner.queue_depth_by_model()
    def pick_next(self, current_model: str | None = None) -> Any:
        return self._inner.pick_next(current_model)
    def get_models_with_requests(self) -> list[str]:
        return self._inner.get_models_with_requests()
    def model_queue_size(self, model: str) -> int:
        return self._inner.model_queue_size(model)
    def sweep_stale(self, max_age_seconds: float) -> list:
        return self._inner.sweep_stale(max_age_seconds)
    def drain_all(self) -> list:
        return self._inner.drain_all()

    # Write methods (dual-write, sync interface matching AffinityQueue)
    def enqueue(self, request: Any) -> bool:
        result = self._inner.enqueue(request)
        if result:
            self._schedule_persist(request)
        return result

    def dequeue_for_model(self, model: str) -> Any:
        request = self._inner.dequeue_for_model(model)
        if request is not None:
            self._schedule_complete(request.id)
        return request

    def cancel(self, request_id: str) -> bool:
        result = self._inner.cancel(request_id)
        if result:
            self._schedule_complete(request_id)
        return result

    def _schedule_persist(self, request: Any) -> None:
        """Fire-and-forget async persist of a queue entry."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._persist_entry(request))

    def _schedule_complete(self, entry_id: str) -> None:
        """Fire-and-forget async mark-completed."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._mark_completed(entry_id))

    async def _persist_entry(self, request: Any) -> None:
        try:
            body_str = (
                request.body.decode("utf-8", errors="replace")
                if isinstance(request.body, bytes)
                else str(request.body)
            )
            payload = json.dumps({
                "model": request.model,
                "endpoint": request.endpoint,
                "body": body_str,
                "priority": request.priority,
                "base_priority": request.base_priority,
            })
            await self._db.conn.execute(
                """INSERT OR REPLACE INTO queue_entries
                   (entry_id, model, priority, payload, enqueued_at, completed)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (
                    request.id,
                    request.model,
                    int(request.base_priority),
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await self._db.conn.commit()
        except Exception as e:
            logger.warning("SQLite queue persist failed for %s: %s", request.id, e)

    async def _mark_completed(self, entry_id: str) -> None:
        try:
            await self._db.conn.execute(
                "UPDATE queue_entries SET completed = 1 WHERE entry_id = ?", (entry_id,))
            await self._db.conn.commit()
        except Exception as e:
            logger.warning("SQLite queue completion update failed for %s: %s", entry_id, e)

    async def hydrate(self, recovery_ttl: int = 300) -> tuple[int, int]:
        """Replay pending queue entries from SQLite, respecting TTL.

        Returns (recovered, discarded).
        """
        from bastion.models import QueuedRequest
        recovered = 0
        discarded = 0
        now = time.time()
        query = (
            "SELECT entry_id, model, priority, payload, enqueued_at "
            "FROM queue_entries WHERE completed = 0"
        )
        async with self._db.conn.execute(query) as cursor:
            async for row in cursor:
                entry_id, model, priority, payload_json, enqueued_at = row
                try:
                    enqueued_dt = datetime.fromisoformat(enqueued_at)
                    age = now - enqueued_dt.timestamp()
                except (ValueError, OSError):
                    age = recovery_ttl + 1
                if age > recovery_ttl:
                    discarded += 1
                    logger.info(
                        "Discarding stale queue entry %s (age=%.0fs, ttl=%ds)",
                        entry_id, age, recovery_ttl,
                    )
                    continue
                try:
                    data = json.loads(payload_json)
                    body_field = data.get("body", "")
                    body_bytes = (
                        body_field.encode("utf-8")
                        if isinstance(body_field, str)
                        else b""
                    )
                    req = QueuedRequest(
                        id=entry_id, model=data.get("model", model),
                        endpoint=data.get("endpoint", "/api/generate"),
                        body=body_bytes,
                        priority=float(data.get("priority", priority)),
                        base_priority=float(data.get("base_priority", priority)),
                    )
                    if self._inner.enqueue(req):
                        recovered += 1
                except Exception as e:
                    logger.warning("Failed to hydrate queue entry %s: %s", entry_id, e)
                    discarded += 1
        if recovered or discarded:
            logger.info("Queue hydration: recovered=%d, discarded=%d", recovered, discarded)
        # Mark all pending entries as completed after hydration
        # (recovered entries are now in-memory; stale entries should not be retried)
        try:
            await self._db.conn.execute(
                "UPDATE queue_entries SET completed = 1 WHERE completed = 0"
            )
            await self._db.conn.commit()
        except Exception as e:
            logger.warning("Failed to mark hydrated queue entries: %s", e)
        return recovered, discarded
