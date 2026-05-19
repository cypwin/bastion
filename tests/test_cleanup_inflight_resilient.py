"""Regression: ``_cleanup_inflight`` must decrement ``_inflight_models`` even
when the upstream ``done_event.wait()`` raises an unexpected exception.

Per KNOWN_ISSUES.md (Important, resolved in v0.4.1):

    "The task is responsible for decrementing `_inflight_models` and calling
    `_scheduler.notify()`. If `_inflight_lock` is None unexpectedly or any
    context manager raises, the task dies silently. The inflight counter
    stays incremented forever, blocking the scheduler from evicting that
    model."

The fix wraps the cleanup body in ``try/except`` with the decrement in
``finally`` so the counter never gets stuck above its true value.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from bastion import server as bsrv
from bastion.models import (
    BrokerConfig,
    GPUConfig,
    ModelInfo,
    PriorityTier,
    ProxyConfig,
    QueuedRequest,
)


@pytest.fixture
def cleanup_request_id():
    """Counter to give each test a unique request id (no cross-test pollution)."""
    return "cleanup-test-req-0001"


@pytest.fixture(autouse=True)
def isolate_server_state():
    """Snapshot + restore server module-level state for each test."""
    snap_inflight = dict(bsrv._inflight_models)
    snap_grants = dict(bsrv._pending_grants)
    snap_completions = dict(bsrv._pending_completions)
    snap_lock = bsrv._inflight_lock
    snap_config = bsrv._config
    snap_scheduler = bsrv._scheduler

    bsrv._inflight_models.clear()
    bsrv._pending_grants.clear()
    bsrv._pending_completions.clear()
    bsrv._inflight_lock = asyncio.Lock()

    yield

    bsrv._inflight_models.clear()
    bsrv._inflight_models.update(snap_inflight)
    bsrv._pending_grants.clear()
    bsrv._pending_grants.update(snap_grants)
    bsrv._pending_completions.clear()
    bsrv._pending_completions.update(snap_completions)
    bsrv._inflight_lock = snap_lock
    bsrv._config = snap_config
    bsrv._scheduler = snap_scheduler


def _make_minimal_request(req_id: str, model: str) -> QueuedRequest:
    return QueuedRequest(
        id=req_id,
        model=model,
        endpoint="/api/generate",
        body=b"{}",
        priority=1.0,
        base_priority=1.0,
        tier=PriorityTier.AGENT,
        client_info="test",
    )


class TestCleanupInflightResilience:
    @pytest.mark.asyncio
    async def test_decrements_inflight_even_when_done_event_wait_raises(
        self, cleanup_request_id: str,
    ) -> None:
        """If ``done_event.wait()`` raises something other than TimeoutError,
        the inflight counter must STILL be decremented. The previous code
        died on the unhandled exception and left the counter pinned forever,
        blocking eviction for that model.

        Note: ``_dispatch_request`` takes the *non-blocking* path only when
        ``needs_swap=False`` AND the model has zero in-flight requests before
        this one, so the test starts with an empty counter and asserts the
        counter is back to zero after cleanup.
        """
        model = "qwen3:14b"
        # Counter must start at 0 so the non-blocking path (with _cleanup_inflight)
        # is taken. _dispatch_request will increment to 1; cleanup decrements to 0.
        bsrv._config = BrokerConfig(
            gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
            proxy=ProxyConfig(inference_timeout_seconds=0.05),
            models={model: ModelInfo(vram_gb=9.3)},
        )

        request = _make_minimal_request(cleanup_request_id, model)
        grant_event = asyncio.Event()
        done_event = MagicMock()

        async def evil_wait() -> None:
            raise RuntimeError("simulated unexpected done_event failure")

        done_event.wait = evil_wait

        bsrv._pending_grants[request.id] = grant_event
        bsrv._pending_completions[request.id] = done_event

        # Capture the cleanup task so we can await it
        captured: list[asyncio.Task] = []
        original_create_task = asyncio.create_task

        def capture(coro, *, name=None):
            t = original_create_task(coro, name=name)
            captured.append(t)
            return t

        with patch("bastion.server.asyncio.create_task", side_effect=capture):
            await bsrv._dispatch_request(request, needs_swap=False)

        # Wait for the captured cleanup task to finish (or fail)
        for t in captured:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except Exception:  # noqa: BLE001 — we want to swallow task errors
                pass

        # _dispatch_request incremented to 1; cleanup MUST decrement back to 0
        # (model removed from dict) even though done_event.wait raised. Without
        # the fix, the cleanup task died and the counter stayed at 1 forever.
        assert model not in bsrv._inflight_models, (
            f"inflight counter stuck: {bsrv._inflight_models}. "
            "The cleanup task died before decrementing — KNOWN_ISSUES regression."
        )

    @pytest.mark.asyncio
    async def test_cleanup_decrements_to_zero_on_normal_completion(
        self, cleanup_request_id: str,
    ) -> None:
        """Sanity check: when done_event completes normally, the counter
        still decrements correctly (we didn't break the happy path).
        """
        model = "qwen3:14b"
        bsrv._inflight_models[model] = 0
        bsrv._config = BrokerConfig(
            gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
            proxy=ProxyConfig(inference_timeout_seconds=5.0),
            models={model: ModelInfo(vram_gb=9.3)},
        )

        request = _make_minimal_request(cleanup_request_id, model)
        grant_event = asyncio.Event()
        done_event = asyncio.Event()
        done_event.set()  # Already complete — wait_for returns immediately

        bsrv._pending_grants[request.id] = grant_event
        bsrv._pending_completions[request.id] = done_event

        captured: list[asyncio.Task] = []
        original_create_task = asyncio.create_task

        def capture(coro, *, name=None):
            t = original_create_task(coro, name=name)
            captured.append(t)
            return t

        with patch("bastion.server.asyncio.create_task", side_effect=capture):
            await bsrv._dispatch_request(request, needs_swap=False)

        for t in captured:
            await asyncio.wait_for(t, timeout=2.0)

        # +1 from _dispatch_request, −1 from cleanup = 0 (removed from dict)
        assert model not in bsrv._inflight_models, (
            f"happy path regressed: {bsrv._inflight_models}"
        )
