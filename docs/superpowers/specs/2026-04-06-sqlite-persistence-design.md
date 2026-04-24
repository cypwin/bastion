# BASTION SQLite Persistence — Design Spec

> Phase 3.2 of the production roadmap. Optional SQLite persistence for audit,
> task state, and queue recovery.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| SQLite library | `aiosqlite` (optional dep) | Async-native, fits existing async lifespan; optional via `pip install bastion[persistence]` |
| Store pattern | Composition/wrapper | Zero changes to existing in-memory stores; reads stay fast from memory, writes dual-write to SQLite |
| Schema approach | Versioned with migration runner | Simple `schema_version` table + migration list; ~30 lines, prevents upgrade pain |
| Queue recovery | TTL-gated replay | Only recover entries younger than configurable TTL (default 5 min); stale entries discarded with audit log |
| Dependency model | Optional with graceful fallback | Consistent with prometheus-client, opentelemetry pattern; `try: import aiosqlite` |

## 1. Configuration

### PersistenceConfig (new model in `models.py`)

```python
class PersistenceConfig(BaseModel):
    enabled: bool = False
    database_path: str = ""        # empty = auto (XDG data_dir / "bastion.db")
    persist_audit: bool = True
    persist_tasks: bool = True
    persist_queue: bool = False     # opt-in, most users don't need this
    queue_recovery_ttl: int = 300   # seconds, entries older than this discarded on startup
```

Added as `persistence: PersistenceConfig` field on `BrokerConfig` (default: disabled).

### New path in `paths.py`

- `database_path()` returns `data_dir() / "bastion.db"`

### Dependency

- `aiosqlite` as optional extra: `pip install bastion[persistence]`
- `pyproject.toml`: `persistence = ["aiosqlite>=0.20"]`
- Env overrides: `BASTION_PERSISTENCE_ENABLED`, `BASTION_PERSISTENCE_DB_PATH`

### Config YAML example

```yaml
persistence:
  enabled: true
  # database_path: ~/.local/share/bastion/bastion.db  # auto-detected
  persist_audit: true
  persist_tasks: true
  persist_queue: false
  queue_recovery_ttl: 300
```

## 2. SQLite Schema & Migrations

### Schema version table

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL  -- ISO 8601
);
```

### Version 1 — three core tables

```sql
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    tier INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    model TEXT,
    client_ip TEXT,
    content_hash TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_audit_timestamp ON audit_events(timestamp);
CREATE INDEX idx_audit_event_type ON audit_events(event_type);

CREATE TABLE IF NOT EXISTS task_state (
    task_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    model TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_task_state ON task_state(state);

CREATE TABLE IF NOT EXISTS queue_entries (
    entry_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    priority INTEGER NOT NULL,
    payload TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_queue_pending ON queue_entries(completed, enqueued_at);
```

### Migration runner

- `MIGRATIONS` dict: `int -> list[str]` (version number to SQL statements)
- On startup: check current version, apply unapplied migrations in order
- Each migration wrapped in a transaction
- Version 1 is the initial schema above
- ~30 lines of code

### Design choice: JSON payload columns

Tables store serialized Pydantic models as JSON in `payload` columns. Indexed
columns (`timestamp`, `state`, `model`, `event_type`) are extracted for
querying. This keeps the schema simple and forward-compatible — adding fields
to Pydantic models doesn't require a migration.

## 3. Persistence Module Architecture

New file: `src/bastion/persistence.py`

### DatabaseManager

Owns the connection lifecycle:

- `async open()` — opens aiosqlite connection, runs migrations, enables WAL mode
- `async close()` — closes connection gracefully
- Shared by all persistent stores (single DB file, single connection)

### PersistentAuditLog

Wraps existing `AuditLogger` class instance:

- Subclasses or wraps `AuditLogger` — replaces the global `_audit_logger` in `audit.py`
- Module-level `emit()` and `emit_tiered()` functions continue to work unchanged (they delegate to the global)
- `emit_tiered()` override — calls original JSONL emit via `super()`, then INSERT into audit_events
- Dual-write: JSONL remains primary (human-readable, grep-able), SQLite is queryable archive
- If SQLite write fails: log warning, don't crash — JSONL is the safety net
- Note: `emit_tiered()` is sync in the current codebase; SQLite write uses `asyncio.get_event_loop().create_task()` to fire-and-forget the async insert, or the wrapper stores a reference to the DB manager and schedules writes via a small internal queue

### PersistentTaskStore

Wraps existing `TaskStore`:

- Takes existing TaskStore instance + DatabaseManager
- All reads delegate to in-memory TaskStore (fast, no change)
- `update_state()`, `create_task()` — dual-write: update in-memory, then persist to SQLite
- `async hydrate()` — on startup, loads active tasks from SQLite into fresh in-memory store
- Backpressure levels stay in-memory only

### PersistentQueue

Wraps existing `AffinityQueue`:

- Takes existing AffinityQueue instance + DatabaseManager
- `enqueue()` — delegates to in-memory, then persists entry
- `dequeue_for_model()` / `cancel()` — delegates to in-memory, marks entry completed=1
- `async hydrate(recovery_ttl)` — replays pending entries younger than TTL, discards stale with audit log
- Queue persistence is opt-in (persist_queue: false by default)

### Key principle

All three wrappers expose the same interface as the thing they wrap. The rest
of BASTION doesn't know or care whether persistence is active.

## 4. Wiring in `server.py`

### Startup sequence (new steps marked with *)

1. Load config, init audit logger (existing)
2. \* If `persistence.enabled`:
   - a. Import aiosqlite (fail fast with clear error if missing)
   - b. Create DatabaseManager, open connection, run migrations
   - c. Wrap audit logger with PersistentAuditLog
   - d. Create TaskStore, wrap with PersistentTaskStore, hydrate from SQLite
   - e. Create AffinityQueue; if persist_queue: wrap with PersistentQueue, hydrate (TTL-gated)
   - f. Log: "Persistence enabled: {db_path}, recovered {n} tasks, {m} queue entries"
3. If persistence NOT enabled: create plain TaskStore + AffinityQueue (unchanged)
4. Rest of lifespan continues as-is

### Shutdown sequence

1. Existing graceful shutdown (stop scheduler, cancel tasks, etc.)
2. \* If persistence.enabled: close DatabaseManager

### Error handling

- `persistence.enabled: true` + aiosqlite missing: clear error —
  `"Persistence requires aiosqlite. Install with: pip install bastion[persistence]"`
- SQLite file locked/corrupt: log error, fall back to in-memory with warning (don't crash)

## 5. Testing Strategy

Test file: `tests/test_persistence.py`

All tests use in-memory SQLite (`:memory:`) — no disk I/O, no cleanup.

### Test groups

1. **DatabaseManager** — migrations apply cleanly, schema version tracked, re-run idempotent
2. **PersistentAuditLog** — dual-writes JSONL + SQLite, SQLite failure falls back gracefully
3. **PersistentTaskStore** — create/update persists, hydrate restores into fresh in-memory store
4. **PersistentQueue** — enqueue persists, dequeue/cancel marks completed, hydrate respects TTL
5. **Config integration** — PersistenceConfig defaults, YAML loading, env var overrides

### Approach

- aiosqlite connects to `:memory:` (real DB, no mocks on DB layer)
- Wrapped in-memory stores are real instances
- Only mock: JSONL file handler in audit tests
- Tests skip with `pytest.skipif` if aiosqlite not installed

## 6. File Impact Summary

### Modified

| File | Change |
|------|--------|
| `src/bastion/models.py` | Add PersistenceConfig, add field to BrokerConfig |
| `src/bastion/paths.py` | Add `database_path()` |
| `src/bastion/config.py` | Add BASTION_PERSISTENCE_* env var overrides |
| `src/bastion/server.py` | Conditional persistence wiring in lifespan() |
| `pyproject.toml` | Add `persistence` optional extra |
| `config/broker.example.yaml` | Add commented persistence section |

### Created

| File | Purpose |
|------|---------|
| `src/bastion/persistence.py` | DatabaseManager, PersistentAuditLog, PersistentTaskStore, PersistentQueue, migrations |
| `tests/test_persistence.py` | Full test suite (~15-20 tests) |

### Not touched

- `audit.py`, `taskstore.py`, `queue.py` — existing stores untouched
- `proxy.py`, `scheduler.py`, `a2a.py` — no persistence awareness
- `dashboard/` — no changes

### Estimated size

- `persistence.py`: ~300-400 lines
- `test_persistence.py`: ~200-300 lines
