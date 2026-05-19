"""End-to-end A2A protocol contract tests.

Pins behaviour not covered by ``tests/test_a2a.py`` (which targets the
skill handlers and agent card) or ``tests/test_taskstore.py`` (which
targets the store in isolation). Focus areas:

  * Full task state machine via the A2AHandler — including the
    invalid-transition guards in ``_safe_transition``.
  * The HTTP surface at ``/a2a/tasks``, ``/a2a/tasks/{id}``,
    DELETE ``/a2a/tasks/{id}``, ``/a2a/stats``, plus lease release.
  * TTL / compaction observable through ``get_task`` (active record
    vs. ``CompactedResult`` shape).
  * Backpressure: ``TaskStoreFullError`` surfaces as a JSON-RPC-ish
    ``{"error": ..., "retry_after": ...}`` from ``create_task``.
  * Lease lifecycle exposed via the public handler API.

Tests intentionally drive the A2AHandler directly where the contract
is internal, and via FastAPI ``TestClient`` where the HTTP route is
the contract.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from bastion.a2a import A2AHandler
from bastion.models import (
    A2AConfig,
    A2ATaskRecord,
    A2ATaskState,
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    OllamaConfig,
    PriorityTier,
    QueuedRequest,
    SchedulerConfig,
    ServerConfig,
)
from bastion.taskstore import CompactedResult, TaskStore, TaskStoreFullError
from bastion.vram import VRAMTracker

# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------

def _make_a2a_config(*, tokens: list[str] | None = None) -> BrokerConfig:
    """BrokerConfig with A2A enabled, no auth tokens by default."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434),
        gpu=GPUConfig(
            total_vram_gb=32.0,
            headroom_gb=6.0,
            max_temperature_c=82,
            max_power_watts=450.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            max_queue_size=16,
        ),
        a2a=A2AConfig(
            enabled=True,
            tokens=tokens or [],
            max_batch_size=4,
            reservation_timeout_seconds=10.0,
            task_ttl_seconds=3600.0,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3, tags=["fast"]),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
        },
    )


@pytest.fixture
def a2a_config() -> BrokerConfig:
    return _make_a2a_config()


@pytest.fixture
def mock_vram() -> MagicMock:
    tracker = MagicMock(spec=VRAMTracker)
    tracker.get_loaded_models = AsyncMock(
        return_value=[LoadedModel(name="qwen3:14b", size_bytes=9_965_000_000, vram_gb=9.3)],
    )
    tracker.get_loaded_vram_gb = AsyncMock(return_value=9.3)
    tracker.can_load_model = AsyncMock(return_value=(True, "ok"))
    return tracker


@pytest.fixture
def mock_scheduler() -> MagicMock:
    sched = MagicMock()
    sched.current_model = "qwen3:14b"
    sched.queue = MagicMock()
    sched.queue.total_size = 0
    sched.queue.queue_depth_by_model = MagicMock(return_value={})
    return sched


@pytest.fixture
async def handler(
    a2a_config: BrokerConfig,
    mock_vram: MagicMock,
    mock_scheduler: MagicMock,
) -> A2AHandler:
    """Fresh handler with a fake_enqueue that grants requests immediately."""
    async def fake_enqueue(_request: QueuedRequest):
        event = asyncio.Event()
        event.set()
        return event, (lambda: None), (lambda: None)

    return A2AHandler(
        config=a2a_config,
        enqueue_fn=fake_enqueue,
        vram_tracker=mock_vram,
        scheduler=mock_scheduler,
    )


@pytest.fixture
def a2a_app_client(
    a2a_config: BrokerConfig,
    mock_vram: MagicMock,
    mock_scheduler: MagicMock,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, A2AHandler], None, None]:
    """TestClient with create_app(...) using the lifespan-created A2A handler.

    The lifespan creates a real A2AHandler when ``a2a.enabled=True``; we
    grab that handler, retarget its enqueue/vram dependencies so skills
    don't depend on a live Ollama backend, and stub the scheduler.
    """
    import bastion.server as server_mod
    from bastion.server import create_app

    monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))

    app = create_app(a2a_config)

    async def fake_enqueue(_request: QueuedRequest):
        event = asyncio.Event()
        event.set()
        return event, (lambda: None), (lambda: None)

    with TestClient(app) as client:
        # Lifespan has already constructed _a2a_handler. Reach in and
        # swap its outward dependencies so we don't need real Ollama.
        h: A2AHandler | None = server_mod._a2a_handler
        assert h is not None, "lifespan should have created A2A handler"
        h._enqueue_fn = fake_enqueue
        h._vram = mock_vram

        orig_scheduler = server_mod._scheduler
        sched_stub: Any = MagicMock()
        sched_stub.is_draining = False
        sched_stub.current_model = "qwen3:14b"
        sched_stub.queue = MagicMock()
        sched_stub.queue.total_size = 0
        sched_stub.queue.queue_depth_by_model = MagicMock(return_value={})
        server_mod._scheduler = sched_stub

        try:
            yield client, h
        finally:
            server_mod._scheduler = orig_scheduler


def _make_record(
    task_id: str = "lifecycle-001",
    state: A2ATaskState = A2ATaskState.SUBMITTED,
    skill_id: str = "status",
) -> A2ATaskRecord:
    return A2ATaskRecord(
        task_id=task_id,
        context_id="ctx-lifecycle",
        state=state,
        skill_id=skill_id,
        input_params={"model": "qwen3:14b"},
    )


# ---------------------------------------------------------------------------
# Task submission + retrieval (handler-level)
# ---------------------------------------------------------------------------

class TestTaskSubmissionContract:
    """Pin the create_task / get_task return contract."""

    @pytest.mark.asyncio
    async def test_submit_status_task_returns_id_and_state(
        self, handler: A2AHandler,
    ) -> None:
        result = await handler.create_task({"skill_id": "status", "params": {}})
        assert "id" in result
        assert isinstance(result["id"], str) and len(result["id"]) > 0
        assert result["status"]["state"] in ("submitted", "working", "completed")
        assert "contextId" in result
        assert result["artifacts"] == []

    @pytest.mark.asyncio
    async def test_get_task_returns_record_dict(self, handler: A2AHandler) -> None:
        created = await handler.create_task({"skill_id": "status", "params": {}})
        # Allow handler to run to completion
        await asyncio.sleep(0.05)
        task = await handler.get_task(created["id"])
        assert task is not None
        assert task["id"] == created["id"]
        assert task["status"]["state"] in ("working", "completed")

    @pytest.mark.asyncio
    async def test_get_unknown_task_returns_none(self, handler: A2AHandler) -> None:
        assert await handler.get_task("not-a-real-task-id") is None

    @pytest.mark.asyncio
    async def test_missing_skill_id_yields_failed_record(
        self, handler: A2AHandler,
    ) -> None:
        result = await handler.create_task({"params": {"model": "qwen3:14b"}})
        assert result["status"]["state"] == "failed"
        assert "Missing skill_id" in result["status"]["message"]


# ---------------------------------------------------------------------------
# State machine — valid transitions (driven via TaskStore through handler)
# ---------------------------------------------------------------------------

class TestStateMachineValidTransitions:
    """Pin every transition the A2A spec considers valid.

    Drives transitions through ``_safe_transition`` (the handler-level
    wrapper) so we exercise the full path: store update + notify.
    """

    @pytest.mark.asyncio
    async def test_submitted_to_working(self, handler: A2AHandler) -> None:
        record = _make_record()
        handler._store.create(record)
        assert handler._safe_transition(record.task_id, A2ATaskState.WORKING) is True
        active = handler._store.get_active(record.task_id)
        assert active is not None
        assert active.state == A2ATaskState.WORKING

    @pytest.mark.asyncio
    async def test_working_to_completed_is_compacted(
        self, handler: A2AHandler,
    ) -> None:
        record = _make_record()
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        assert handler._safe_transition(record.task_id, A2ATaskState.COMPLETED) is True
        # Compacted: no longer in active store
        assert handler._store.get_active(record.task_id) is None
        # Visible via get() as CompactedResult
        result = handler._store.get(record.task_id)
        assert isinstance(result, CompactedResult)
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_working_to_failed_is_compacted(self, handler: A2AHandler) -> None:
        record = _make_record(task_id="lifecycle-002")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        assert handler._safe_transition(record.task_id, A2ATaskState.FAILED) is True
        result = handler._store.get(record.task_id)
        assert isinstance(result, CompactedResult)
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_working_to_canceled_is_compacted(
        self, handler: A2AHandler,
    ) -> None:
        record = _make_record(task_id="lifecycle-003")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        assert handler._safe_transition(record.task_id, A2ATaskState.CANCELED) is True
        result = handler._store.get(record.task_id)
        assert isinstance(result, CompactedResult)
        assert result.status == "canceled"

    @pytest.mark.asyncio
    async def test_submitted_to_canceled_before_dispatch(
        self, handler: A2AHandler,
    ) -> None:
        """A submitted task can be canceled without going through working."""
        record = _make_record(task_id="lifecycle-004")
        handler._store.create(record)
        # Direct submitted -> canceled (handler.cancel_task supports this)
        canceled = await handler.cancel_task(record.task_id)
        assert canceled is True
        result = handler._store.get(record.task_id)
        assert isinstance(result, CompactedResult)
        assert result.status == "canceled"

    @pytest.mark.asyncio
    async def test_submitted_to_failed_is_valid(self, handler: A2AHandler) -> None:
        """SUBMITTED -> FAILED is valid per the spec's terminal-from-any rule
        (the store allows it; the handler uses this when create_task hits
        a missing skill_id, but with that record already created in FAILED)."""
        record = _make_record(task_id="lifecycle-005")
        handler._store.create(record)
        assert handler._safe_transition(record.task_id, A2ATaskState.FAILED) is True
        result = handler._store.get(record.task_id)
        assert isinstance(result, CompactedResult)
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# State machine — invalid transitions
# ---------------------------------------------------------------------------

class TestStateMachineInvalidTransitions:
    """Terminal states reject further transitions; bogus moves are blocked."""

    @pytest.mark.asyncio
    async def test_completed_to_anything_rejected(
        self, handler: A2AHandler,
    ) -> None:
        record = _make_record(task_id="inv-001")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        handler._safe_transition(record.task_id, A2ATaskState.COMPLETED)
        # Task no longer in active store; _safe_transition swallows the KeyError
        assert handler._safe_transition(record.task_id, A2ATaskState.WORKING) is False
        assert handler._safe_transition(record.task_id, A2ATaskState.FAILED) is False

    @pytest.mark.asyncio
    async def test_failed_to_anything_rejected(self, handler: A2AHandler) -> None:
        record = _make_record(task_id="inv-002")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        handler._safe_transition(record.task_id, A2ATaskState.FAILED)
        assert handler._safe_transition(record.task_id, A2ATaskState.COMPLETED) is False
        assert handler._safe_transition(record.task_id, A2ATaskState.CANCELED) is False

    @pytest.mark.asyncio
    async def test_canceled_to_anything_rejected(self, handler: A2AHandler) -> None:
        record = _make_record(task_id="inv-003")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.CANCELED)
        assert handler._safe_transition(record.task_id, A2ATaskState.WORKING) is False
        assert handler._safe_transition(record.task_id, A2ATaskState.COMPLETED) is False

    @pytest.mark.asyncio
    async def test_working_to_submitted_rejected(self, handler: A2AHandler) -> None:
        """No backwards transitions — once working, you cannot un-submit."""
        record = _make_record(task_id="inv-004")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        # Working -> Submitted is not in the valid-transitions table.
        with pytest.raises(ValueError, match="Invalid state transition"):
            handler._store.update_state(record.task_id, A2ATaskState.SUBMITTED)
        # And the safe wrapper turns it into False without raising.
        assert handler._safe_transition(record.task_id, A2ATaskState.SUBMITTED) is False

    @pytest.mark.asyncio
    async def test_submitted_to_completed_rejected(
        self, handler: A2AHandler,
    ) -> None:
        """A task must transition through WORKING before it can complete."""
        record = _make_record(task_id="inv-005")
        handler._store.create(record)
        with pytest.raises(ValueError, match="Invalid state transition"):
            handler._store.update_state(record.task_id, A2ATaskState.COMPLETED)


# ---------------------------------------------------------------------------
# Cancel semantics (the handler API consumers actually use)
# ---------------------------------------------------------------------------

class TestCancelSemantics:
    """cancel_task: True for active tasks; False for terminal / unknown."""

    @pytest.mark.asyncio
    async def test_cancel_unknown_task_returns_false(
        self, handler: A2AHandler,
    ) -> None:
        assert await handler.cancel_task("no-such-task") is False

    @pytest.mark.asyncio
    async def test_cancel_working_task_succeeds(self, handler: A2AHandler) -> None:
        record = _make_record(task_id="cancel-001")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        assert await handler.cancel_task(record.task_id) is True
        compacted = handler._store.get(record.task_id)
        assert isinstance(compacted, CompactedResult)
        assert compacted.status == "canceled"

    @pytest.mark.asyncio
    async def test_cancel_completed_task_returns_false(
        self, handler: A2AHandler,
    ) -> None:
        record = _make_record(task_id="cancel-002")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        handler._safe_transition(record.task_id, A2ATaskState.COMPLETED)
        assert await handler.cancel_task(record.task_id) is False

    @pytest.mark.asyncio
    async def test_cancel_failed_task_returns_false(
        self, handler: A2AHandler,
    ) -> None:
        record = _make_record(task_id="cancel-003")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        handler._safe_transition(record.task_id, A2ATaskState.FAILED)
        assert await handler.cancel_task(record.task_id) is False

    @pytest.mark.asyncio
    async def test_double_cancel_is_idempotent_in_effect(
        self, handler: A2AHandler,
    ) -> None:
        """First cancel wins; second cancel returns False but state is stable."""
        record = _make_record(task_id="cancel-004")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        first = await handler.cancel_task(record.task_id)
        second = await handler.cancel_task(record.task_id)
        assert first is True
        assert second is False
        compacted = handler._store.get(record.task_id)
        assert isinstance(compacted, CompactedResult)
        assert compacted.status == "canceled"


# ---------------------------------------------------------------------------
# Skill routing — unknown / missing
# ---------------------------------------------------------------------------

class TestSkillRouting:
    """The handler accepts only registered skill IDs."""

    @pytest.mark.asyncio
    async def test_unknown_skill_creates_failed_task(
        self, handler: A2AHandler,
    ) -> None:
        result = await handler.create_task(
            {"skill_id": "telekinesis", "params": {}},
        )
        assert result["status"]["state"] == "failed"
        assert "Unknown skill" in result["status"]["message"]

    @pytest.mark.asyncio
    async def test_skill_id_alias_skill_id_camelcase_accepted(
        self, handler: A2AHandler,
    ) -> None:
        """A2A SDK uses ``skillId``; legacy callers use ``skill_id``."""
        result = await handler.create_task({"skillId": "status", "params": {}})
        assert result["status"]["state"] in ("submitted", "working", "completed")

    @pytest.mark.asyncio
    async def test_skill_id_extracted_from_data_part(
        self, handler: A2AHandler,
    ) -> None:
        """A2A messages can carry skill_id inside parts[].data."""
        result = await handler.create_task({
            "parts": [
                {"kind": "data", "data": {"skill_id": "status", "params": {}}},
            ],
        })
        assert result["status"]["state"] in ("submitted", "working", "completed")

    @pytest.mark.asyncio
    async def test_known_skills_all_registered(self, handler: A2AHandler) -> None:
        assert set(handler._skill_handlers.keys()) == {
            "infer", "status", "batch_infer", "preload",
        }


# ---------------------------------------------------------------------------
# TTL & compaction
# ---------------------------------------------------------------------------

class TestTTLAndCompaction:
    """Compaction visible at handler level; active TTL evicts expired tasks."""

    @pytest.mark.asyncio
    async def test_terminal_task_returned_as_compacted_dict(
        self, handler: A2AHandler,
    ) -> None:
        record = _make_record(task_id="ttl-001")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        handler._safe_transition(record.task_id, A2ATaskState.COMPLETED)
        # The handler's get_task wraps CompactedResult in a dict with
        # state + result_summary + artifacts.
        dump = await handler.get_task(record.task_id)
        assert dump is not None
        assert dump["status"]["state"] == "completed"
        assert "result_summary" in dump
        assert dump["artifacts"] == []  # status skill produces data parts only

    @pytest.mark.asyncio
    async def test_active_task_evicted_after_ttl(
        self, handler: A2AHandler,
    ) -> None:
        """Lazy eviction: an over-TTL active task disappears from get_task."""
        record = _make_record(task_id="ttl-002")
        handler._store.create(record)
        # Backdate the active timestamp past the TTL boundary.
        handler._store._active_timestamps[record.task_id] = (
            time.monotonic() - handler._store._task_ttl - 1.0
        )
        result = await handler.get_task(record.task_id)
        assert result is None
        # Tombstone retains the ID so future lookups remain None.
        assert record.task_id in handler._store._tombstones

    @pytest.mark.asyncio
    async def test_completed_task_evicted_after_completed_ttl(
        self,
        handler: A2AHandler,
    ) -> None:
        record = _make_record(task_id="ttl-003")
        handler._store.create(record)
        handler._safe_transition(record.task_id, A2ATaskState.WORKING)
        handler._safe_transition(record.task_id, A2ATaskState.COMPLETED)
        # Backdate the compacted entry.
        existing = handler._store._completed[record.task_id]
        handler._store._completed[record.task_id] = CompactedResult(
            task_id=existing.task_id,
            status=existing.status,
            result_summary=existing.result_summary,
            error=existing.error,
            completed_at=time.monotonic() - handler._store._completed_ttl - 1.0,
            output_artifacts=existing.output_artifacts,
        )
        assert await handler.get_task(record.task_id) is None


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------

class TestBackpressureContract:
    """``create_task`` surfaces TaskStoreFullError as a structured dict."""

    @pytest.mark.asyncio
    async def test_overloaded_store_rejects_with_retry_after(
        self,
        a2a_config: BrokerConfig,
        mock_vram: MagicMock,
        mock_scheduler: MagicMock,
    ) -> None:
        # Build the handler, then swap its store for a tiny one so we can
        # saturate it cheaply.
        async def fake_enqueue(_request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, (lambda: None), (lambda: None)

        h = A2AHandler(
            config=a2a_config,
            enqueue_fn=fake_enqueue,
            vram_tracker=mock_vram,
            scheduler=mock_scheduler,
        )
        # Replace with an effectively-zero-capacity store.
        h._store = TaskStore(maxsize=1)
        # Fill it with one synthetic record so the pressure level flips to
        # OVERLOADED on the next create attempt.
        h._store.create(_make_record(task_id="bp-fill"))

        result = await h.create_task({"skill_id": "status", "params": {}})
        assert "error" in result
        assert "retry_after" in result
        assert isinstance(result["retry_after"], int)

    def test_taskstore_raises_taskstore_full_at_capacity(self) -> None:
        store = TaskStore(maxsize=1)
        store.create(_make_record(task_id="bp-1"))
        with pytest.raises(TaskStoreFullError) as exc_info:
            store.create(_make_record(task_id="bp-2"))
        assert exc_info.value.retry_after > 0


# ---------------------------------------------------------------------------
# Lease lifecycle (handler API)
# ---------------------------------------------------------------------------

class TestLeaseLifecycle:
    """A2AHandler lease helpers — used by /a2a/leases/* and skill code."""

    @pytest.mark.asyncio
    async def test_create_validate_release_round_trip(
        self, handler: A2AHandler,
    ) -> None:
        lease = handler.create_lease(model="qwen3:14b", max_requests=10)
        assert lease.lease_id in handler._leases
        valid, reason = handler.validate_lease(lease.lease_id, lease.fencing_token)
        assert valid is True
        assert reason == "OK"

        released = handler.release_lease(lease.lease_id)
        assert released is True
        assert lease.lease_id not in handler._leases

    @pytest.mark.asyncio
    async def test_release_unknown_lease_returns_false(
        self, handler: A2AHandler,
    ) -> None:
        assert handler.release_lease("not-a-real-lease") is False

    @pytest.mark.asyncio
    async def test_validate_stale_fencing_token_rejected(
        self, handler: A2AHandler,
    ) -> None:
        lease = handler.create_lease(model="qwen3:14b")
        valid, reason = handler.validate_lease(
            lease.lease_id, lease.fencing_token + 99,
        )
        assert valid is False
        assert "Stale fencing token" in reason

    @pytest.mark.asyncio
    async def test_has_active_lease_tracks_model(self, handler: A2AHandler) -> None:
        assert handler.has_active_lease("qwen3:14b") is False
        lease = handler.create_lease(model="qwen3:14b")
        assert handler.has_active_lease("qwen3:14b") is True
        assert handler.has_active_lease("mistral-nemo:12b") is False
        handler.release_lease(lease.lease_id)
        assert handler.has_active_lease("qwen3:14b") is False


# ---------------------------------------------------------------------------
# HTTP surface — /a2a/* routes via TestClient
# ---------------------------------------------------------------------------

class TestA2AHttpSurface:
    """Pin the JSON contract at the HTTP boundary."""

    def test_post_tasks_returns_201_with_task_record(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, _ = a2a_app_client
        resp = client.post(
            "/a2a/tasks", json={"skill_id": "status", "params": {}},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert body["status"]["state"] in ("submitted", "working", "completed")

    def test_post_tasks_missing_skill_returns_failed_record(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, _ = a2a_app_client
        resp = client.post("/a2a/tasks", json={"params": {}})
        assert resp.status_code == 201  # Record created (in failed state)
        body = resp.json()
        assert body["status"]["state"] == "failed"

    def test_get_task_returns_record(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        # Inject a record directly so we don't depend on scheduler timing.
        rec = _make_record(task_id="http-001", state=A2ATaskState.WORKING)
        h._store.create(rec)

        resp = client.get(f"/a2a/tasks/{rec.task_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == rec.task_id
        assert body["status"]["state"] == "working"

    def test_get_unknown_task_returns_404(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, _ = a2a_app_client
        resp = client.get("/a2a/tasks/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Task not found"

    def test_delete_task_cancels_active_task(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        rec = _make_record(task_id="http-002", state=A2ATaskState.WORKING)
        h._store.create(rec)

        resp = client.delete(f"/a2a/tasks/{rec.task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "canceled"
        assert resp.json()["task_id"] == rec.task_id
        # Confirm the record is now compacted as canceled.
        compacted = h._store.get(rec.task_id)
        assert isinstance(compacted, CompactedResult)
        assert compacted.status == "canceled"

    def test_delete_unknown_task_returns_404(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, _ = a2a_app_client
        resp = client.delete("/a2a/tasks/does-not-exist")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    def test_delete_terminal_task_returns_404(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        """Cancelling an already-completed task surfaces as 404 'not cancelable'."""
        client, h = a2a_app_client
        rec = _make_record(task_id="http-003")
        h._store.create(rec)
        h._safe_transition(rec.task_id, A2ATaskState.WORKING)
        h._safe_transition(rec.task_id, A2ATaskState.COMPLETED)
        resp = client.delete(f"/a2a/tasks/{rec.task_id}")
        assert resp.status_code == 404

    def test_a2a_stats_returns_store_summary(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        # Add 2 active + 1 terminal records.
        h._store.create(_make_record(task_id="stat-1"))
        h._store.create(_make_record(task_id="stat-2"))
        rec = _make_record(task_id="stat-3")
        h._store.create(rec)
        h._safe_transition(rec.task_id, A2ATaskState.WORKING)
        h._safe_transition(rec.task_id, A2ATaskState.COMPLETED)

        resp = client.get("/a2a/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["active_count"] == 2
        assert stats["completed_count"] == 1
        assert "pressure_level" in stats
        assert stats["maxsize"] > 0

    def test_agent_card_endpoint_returns_public_card(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        """When A2A is enabled, the agent card comes from build_public_card."""
        client, _ = a2a_app_client
        resp = client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "BASTION GPU Inference Broker"
        assert "skills" in body
        assert "securitySchemes" in body

    def test_extended_card_endpoint_returns_supported_models(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, _ = a2a_app_client
        resp = client.get("/a2a/extended-card")
        assert resp.status_code == 200
        body = resp.json()
        assert "supported_models" in body
        model_names = {m["name"] for m in body["supported_models"]}
        assert "qwen3:14b" in model_names

    def test_stream_unknown_task_returns_404(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        """SSE stream for an unknown task returns 404 before subscribing."""
        client, _ = a2a_app_client
        resp = client.get("/a2a/tasks/no-such-task/stream")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# HTTP surface — lease endpoints
# ---------------------------------------------------------------------------

class TestA2AHttpLeases:
    """/a2a/leases/{id}/heartbeat + DELETE /a2a/leases/{id}."""

    def test_lease_heartbeat_with_valid_token(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        lease = h.create_lease(model="qwen3:14b", max_requests=5)
        resp = client.post(
            f"/a2a/leases/{lease.lease_id}/heartbeat",
            json={"fencing_token": lease.fencing_token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lease_id"] == lease.lease_id
        assert body["remaining_requests"] == 5

    def test_lease_heartbeat_without_token_returns_400(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        lease = h.create_lease(model="qwen3:14b")
        resp = client.post(
            f"/a2a/leases/{lease.lease_id}/heartbeat", json={},
        )
        assert resp.status_code == 400
        assert "fencing_token" in resp.json()["error"]

    def test_lease_heartbeat_stale_token_returns_409(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        lease = h.create_lease(model="qwen3:14b")
        resp = client.post(
            f"/a2a/leases/{lease.lease_id}/heartbeat",
            json={"fencing_token": lease.fencing_token + 1},
        )
        assert resp.status_code == 409

    def test_release_lease_via_delete(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, h = a2a_app_client
        lease = h.create_lease(model="qwen3:14b")
        resp = client.delete(f"/a2a/leases/{lease.lease_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "released"
        assert lease.lease_id not in h._leases

    def test_release_unknown_lease_returns_404(
        self, a2a_app_client: tuple[TestClient, A2AHandler],
    ) -> None:
        client, _ = a2a_app_client
        resp = client.delete("/a2a/leases/no-such-lease")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth: 401 when tokens are configured
# ---------------------------------------------------------------------------

class TestA2AAuthGate:
    """Bearer-token gate is enforced when ``a2a.tokens`` is non-empty."""

    def test_protected_route_rejects_without_bearer(
        self,
        mock_vram: MagicMock,
        mock_scheduler: MagicMock,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bastion.server as server_mod
        from bastion.server import create_app

        monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))
        cfg = _make_a2a_config(tokens=["s3cret"])
        app = create_app(cfg)

        async def fake_enqueue(_request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, (lambda: None), (lambda: None)

        with TestClient(app) as client:
            h: A2AHandler | None = server_mod._a2a_handler
            assert h is not None
            h._enqueue_fn = fake_enqueue
            h._vram = mock_vram

            orig_sched = server_mod._scheduler
            sched_stub: Any = MagicMock()
            sched_stub.is_draining = False
            sched_stub.current_model = "qwen3:14b"
            sched_stub.queue = MagicMock()
            sched_stub.queue.total_size = 0
            sched_stub.queue.queue_depth_by_model = MagicMock(return_value={})
            server_mod._scheduler = sched_stub
            try:
                # No Authorization header -> 401
                resp = client.post(
                    "/a2a/tasks", json={"skill_id": "status", "params": {}},
                )
                assert resp.status_code == 401

                # With bearer token -> accepted
                resp = client.post(
                    "/a2a/tasks",
                    json={"skill_id": "status", "params": {}},
                    headers={"Authorization": "Bearer s3cret"},
                )
                assert resp.status_code == 201
            finally:
                server_mod._scheduler = orig_sched


# ---------------------------------------------------------------------------
# Circuit breaker fast-fail
# ---------------------------------------------------------------------------

class TestCreateTaskCircuitBreaker:
    """create_task short-circuits with -32050 when the breaker is open."""

    @pytest.mark.asyncio
    async def test_open_breaker_returns_jsonrpc_error(
        self,
        a2a_config: BrokerConfig,
        mock_vram: MagicMock,
        mock_scheduler: MagicMock,
    ) -> None:
        async def fake_enqueue(_request: QueuedRequest):
            event = asyncio.Event()
            event.set()
            return event, (lambda: None), (lambda: None)

        cb = MagicMock()
        cb.state = "open"
        cb._recovery_remaining = MagicMock(return_value=30.0)

        h = A2AHandler(
            config=a2a_config,
            enqueue_fn=fake_enqueue,
            vram_tracker=mock_vram,
            scheduler=mock_scheduler,
            circuit_breaker=cb,
        )
        result = await h.create_task({"skill_id": "status", "params": {}})
        assert result.get("jsonrpc") == "2.0"
        assert result["error"]["code"] == -32050
        assert result["error"]["data"]["retryAfter"] == 30


# ---------------------------------------------------------------------------
# create_task with reservations (priority elevation path covered by infer)
# ---------------------------------------------------------------------------

class TestReservationStateContract:
    """has_active_reservation returns the right answer in edge cases."""

    @pytest.mark.asyncio
    async def test_handler_starts_with_no_reservations(
        self, handler: A2AHandler,
    ) -> None:
        assert handler.has_active_reservation("qwen3:14b") is False
        assert handler._reservations == {}

    @pytest.mark.asyncio
    async def test_manually_inserted_reservation_visible(
        self, handler: A2AHandler,
    ) -> None:
        from bastion.models import Reservation

        res = Reservation(
            model="qwen3:14b",
            remaining_requests=3,
            priority=PriorityTier.INTERACTIVE,
            created_at=time.time(),
            expires_at=time.time() + 30,
        )
        handler._reservations[res.reservation_id] = res
        assert handler.has_active_reservation("qwen3:14b") is True
