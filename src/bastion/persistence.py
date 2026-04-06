"""Optional SQLite persistence for BASTION.

Provides DatabaseManager (connection lifecycle + migrations) and persistent
wrappers for AuditLogger, TaskStore, and AffinityQueue. All wrappers use
composition: reads stay in-memory, writes dual-write to SQLite.

Requires: pip install bastion[persistence]  (aiosqlite)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

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
