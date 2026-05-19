"""Failure-mode tests: queue overflow, drain semantics, VRAM convergence, unload contention.

Complements tests/test_server_admin.py (HTTP-level pins) by exercising the
underlying broker behaviors that make the truthful drain/unload contract real.

Contracts pinned here:

A. Queue overflow
   - AffinityQueue.enqueue returns False once max_queue_size is reached.
   - drain_all empties the queue and the limit can be reached again afterwards.
   - Per-model isolation: a full queue rejects further enqueues regardless of model.

B. Drain semantics (commit 901c910)
   - OllamaProxy._handle_passthrough rejects with 503 when draining AND path is
     NOT in passthrough_endpoints (catches /api/embeddings plural fall-through).
   - OllamaProxy._handle_passthrough STILL serves /api/tags, /api/ps, /api/show
     when draining (operator visibility preserved).
   - When _is_draining_fn is None, drain has no effect on passthrough.

C. VRAM convergence / honest unload
   - VRAMTracker.unload_model returns True only when /api/ps confirms model
     disappearance.
   - VRAMTracker.unload_model returns False on timeout (commit 901c910 honest
     flag); residency cache is still invalidated.
   - Network/exception path returns False.

D. Unload contention (scheduler.unload_model_admin)
   - In-flight model: outcome "inflight" (NOT "unloaded"), tracker.unload_model
     is never called.
   - Active A2A reservation: outcome "reserved", tracker.unload_model is never
     called.
   - Tracker confirms unload: outcome "unloaded" and VRAMManager.release_model
     is called (ledger stays in sync).
   - Tracker fails to confirm: outcome "failed", ledger NOT released
     (truthful flag prevents phantom VRAM-budget rejections on next preload).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bastion.models import (
    BrokerConfig,
    GPUConfig,
    LoadedModel,
    ModelInfo,
    OllamaConfig,
    ProxyConfig,
    SchedulerConfig,
)
from bastion.proxy import OllamaProxy
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAMTracker
from tests.conftest import make_request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proxy_request(
    path: str = "/api/tags",
    method: str = "GET",
    body: bytes = b"",
    headers: dict | None = None,
) -> MagicMock:
    """Build a FastAPI-like Request stub for proxy.handle_request."""
    req = MagicMock()
    req.url.path = path
    req.method = method
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {"user-agent": "test-client/1.0"}
    return req


# ---------------------------------------------------------------------------
# Queue overflow
# ---------------------------------------------------------------------------


class TestQueueOverflow:
    """AffinityQueue must reject excess requests once max_queue_size is hit."""

    def test_enqueue_returns_false_when_full(self) -> None:
        """Contract: AffinityQueue.enqueue returns False at capacity."""
        cfg = SchedulerConfig(max_queue_size=3, cooldown_seconds=0.0)
        q = AffinityQueue(cfg)

        for _ in range(3):
            assert q.enqueue(make_request(model="qwen3:14b")) is True
        # 4th rejected
        assert q.enqueue(make_request(model="qwen3:14b")) is False
        assert q.total_size == 3

    def test_full_queue_rejects_other_models_too(self) -> None:
        """Capacity is global, not per-model."""
        cfg = SchedulerConfig(max_queue_size=2, cooldown_seconds=0.0)
        q = AffinityQueue(cfg)

        assert q.enqueue(make_request(model="qwen3:14b")) is True
        assert q.enqueue(make_request(model="qwen3:14b")) is True
        # Another model cannot sneak in
        assert q.enqueue(make_request(model="mistral-nemo:12b")) is False

    def test_drain_all_restores_capacity(self) -> None:
        """After drain_all, fresh requests are accepted again."""
        cfg = SchedulerConfig(max_queue_size=2, cooldown_seconds=0.0)
        q = AffinityQueue(cfg)

        for _ in range(2):
            assert q.enqueue(make_request(model="qwen3:14b")) is True
        assert q.enqueue(make_request(model="qwen3:14b")) is False

        drained = q.drain_all()
        assert len(drained) == 2
        assert q.total_size == 0
        # Capacity is restored
        assert q.enqueue(make_request(model="qwen3:14b")) is True

    def test_dequeue_makes_room_for_new_request(self) -> None:
        """Dequeueing one slot frees space for one more enqueue."""
        cfg = SchedulerConfig(max_queue_size=2, cooldown_seconds=0.0)
        q = AffinityQueue(cfg)

        assert q.enqueue(make_request(model="qwen3:14b")) is True
        assert q.enqueue(make_request(model="qwen3:14b")) is True
        assert q.enqueue(make_request(model="qwen3:14b")) is False

        assert q.dequeue_for_model("qwen3:14b") is not None
        # One slot freed
        assert q.enqueue(make_request(model="qwen3:14b")) is True
        assert q.total_size == 2


# ---------------------------------------------------------------------------
# Drain semantics (commit 901c910)
# ---------------------------------------------------------------------------


class TestDrainSemanticsPassthrough:
    """Validate the 901c910 fix: drain blocks inference-adjacent passthrough,
    but management endpoints (/api/tags, /api/ps, /api/show) keep serving."""

    @pytest.mark.asyncio
    async def test_drain_blocks_inference_adjacent_passthrough(self) -> None:
        """Path not in passthrough_endpoints is rejected with 503 when draining.

        This catches /api/embeddings (plural) which falls through to passthrough
        but should still be blocked during drain.
        """
        # Build a proxy whose passthrough set deliberately omits /api/embeddings
        cfg = BrokerConfig(
            proxy=ProxyConfig(
                passthrough_endpoints={"/api/tags", "/api/ps", "/api/show"},
            ),
        )
        proxy = OllamaProxy(cfg)
        proxy._is_draining_fn = lambda: True

        req = _make_proxy_request(path="/api/embeddings", method="POST")
        resp = await proxy.handle_request(req)
        assert resp.status_code == 503
        body = resp.body.decode() if hasattr(resp, "body") else ""
        assert "draining" in body.lower()

    @pytest.mark.asyncio
    async def test_drain_allows_api_tags(self) -> None:
        """/api/tags must keep serving while draining (operator visibility)."""
        cfg = BrokerConfig()
        proxy = OllamaProxy(cfg)
        proxy._is_draining_fn = lambda: True

        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "qwen3:14b"}]},
            request=httpx.Request("GET", "http://mock"),
            headers={"content-type": "application/json"},
        )
        with patch.object(
            proxy._http, "request", new_callable=AsyncMock, return_value=mock_resp,
        ):
            req = _make_proxy_request(path="/api/tags", method="GET")
            resp = await proxy.handle_request(req)

        # /api/tags is in passthrough_endpoints -> served, not blocked
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_drain_allows_api_ps(self) -> None:
        """/api/ps must keep serving while draining (operator visibility)."""
        cfg = BrokerConfig()
        proxy = OllamaProxy(cfg)
        proxy._is_draining_fn = lambda: True

        mock_resp = httpx.Response(
            200,
            json={"models": []},
            request=httpx.Request("GET", "http://mock"),
            headers={"content-type": "application/json"},
        )
        with patch.object(
            proxy._http, "request", new_callable=AsyncMock, return_value=mock_resp,
        ):
            req = _make_proxy_request(path="/api/ps", method="GET")
            resp = await proxy.handle_request(req)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_drain_allows_api_show(self) -> None:
        """/api/show must keep serving while draining (operator visibility)."""
        cfg = BrokerConfig()
        proxy = OllamaProxy(cfg)
        proxy._is_draining_fn = lambda: True

        mock_resp = httpx.Response(
            200,
            json={"modelfile": "FROM qwen3:14b"},
            request=httpx.Request("POST", "http://mock"),
            headers={"content-type": "application/json"},
        )
        with patch.object(
            proxy._http, "request", new_callable=AsyncMock, return_value=mock_resp,
        ):
            req = _make_proxy_request(
                path="/api/show",
                method="POST",
                body=b'{"name": "qwen3:14b"}',
            )
            resp = await proxy.handle_request(req)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_drain_callable_means_no_blocking(self) -> None:
        """When _is_draining_fn is None (no scheduler wired), drain doesn't apply.

        Pins the default-safe behaviour for tests / unit-only proxies.
        """
        cfg = BrokerConfig(
            proxy=ProxyConfig(
                passthrough_endpoints={"/api/tags"},
            ),
        )
        proxy = OllamaProxy(cfg)
        assert proxy._is_draining_fn is None

        mock_resp = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("POST", "http://mock"),
            headers={"content-type": "application/json"},
        )
        with patch.object(
            proxy._http, "request", new_callable=AsyncMock, return_value=mock_resp,
        ):
            # /api/embeddings is NOT in passthrough_endpoints but drain is off
            req = _make_proxy_request(path="/api/embeddings", method="POST")
            resp = await proxy.handle_request(req)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_drain_off_serves_everything(self) -> None:
        """is_draining_fn returns False -> passthrough behaves normally."""
        cfg = BrokerConfig(
            proxy=ProxyConfig(
                passthrough_endpoints={"/api/tags"},
            ),
        )
        proxy = OllamaProxy(cfg)
        proxy._is_draining_fn = lambda: False  # actively NOT draining

        mock_resp = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("POST", "http://mock"),
            headers={"content-type": "application/json"},
        )
        with patch.object(
            proxy._http, "request", new_callable=AsyncMock, return_value=mock_resp,
        ):
            req = _make_proxy_request(path="/api/embeddings", method="POST")
            resp = await proxy.handle_request(req)

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# VRAM convergence / honest unload (commit 901c910)
# ---------------------------------------------------------------------------


def _ps_response(models: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"models": models},
        request=httpx.Request("GET", "http://mock"),
    )


def _post_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={},
        request=httpx.Request("POST", "http://mock"),
    )


class TestVRAMConvergenceUnload:
    """VRAMTracker.unload_model must only return True when /api/ps confirms removal."""

    @pytest.mark.asyncio
    async def test_unload_confirmed_returns_true(self) -> None:
        """Model leaves /api/ps -> unload returns True."""
        cfg = BrokerConfig(
            ollama=OllamaConfig(unload_timeout_seconds=1.0),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(cfg)

        # Single empty /api/ps after the unload POST -> confirmed
        with patch.object(
            tracker._http, "post", new_callable=AsyncMock, return_value=_post_response(),
        ), patch.object(
            tracker._http, "get", new_callable=AsyncMock, return_value=_ps_response([]),
        ):
            assert await tracker.unload_model("qwen3:14b") is True

    @pytest.mark.asyncio
    async def test_unload_timeout_returns_false(self) -> None:
        """Model never leaves /api/ps within timeout -> unload returns False.

        This is the 901c910 honesty fix: callers must not see True when the
        model could still be resident (would cause phantom 409s on preload).
        """
        cfg = BrokerConfig(
            ollama=OllamaConfig(unload_timeout_seconds=0.3),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(cfg)

        stuck = _ps_response(
            [{"name": "qwen3:14b", "size": 9_000_000_000, "details": {}}]
        )
        with patch.object(
            tracker._http, "post", new_callable=AsyncMock, return_value=_post_response(),
        ), patch.object(
            tracker._http, "get", new_callable=AsyncMock, return_value=stuck,
        ):
            assert await tracker.unload_model("qwen3:14b") is False

    @pytest.mark.asyncio
    async def test_unload_invalidates_residency_cache_on_timeout(self) -> None:
        """Even when unload can't be confirmed, the residency cache is invalidated.

        Otherwise the scheduler could keep dispatching to a "resident" model that
        Ollama is actively trying to drop.
        """
        cfg = BrokerConfig(
            ollama=OllamaConfig(unload_timeout_seconds=0.2),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(cfg)
        # Prime the cache
        tracker.residency_cache._cache = [LoadedModel(name="qwen3:14b", vram_gb=9.3)]
        tracker.residency_cache._cache_timestamp = 99999999999.0  # "fresh"

        stuck = _ps_response(
            [{"name": "qwen3:14b", "size": 9_000_000_000, "details": {}}]
        )
        with patch.object(
            tracker._http, "post", new_callable=AsyncMock, return_value=_post_response(),
        ), patch.object(
            tracker._http, "get", new_callable=AsyncMock, return_value=stuck,
        ):
            confirmed = await tracker.unload_model("qwen3:14b")

        assert confirmed is False
        # invalidate() sets timestamp to 0 so the next read refreshes
        assert tracker.residency_cache._cache_timestamp == 0.0

    @pytest.mark.asyncio
    async def test_unload_http_failure_returns_false(self) -> None:
        """If the POST to /api/generate (keep_alive=0) raises, unload returns False."""
        cfg = BrokerConfig(
            ollama=OllamaConfig(unload_timeout_seconds=0.2),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        tracker = VRAMTracker(cfg)

        with patch.object(
            tracker._http,
            "post",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            assert await tracker.unload_model("qwen3:14b") is False


# ---------------------------------------------------------------------------
# Unload contention (scheduler.unload_model_admin discriminated outcomes)
# ---------------------------------------------------------------------------


def _scheduler_config() -> BrokerConfig:
    return BrokerConfig(
        gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
        scheduler=SchedulerConfig(cooldown_seconds=0.0, max_queue_size=8),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
        },
    )


async def _noop_dispatch(req, needs_swap: bool = True) -> None:  # pragma: no cover
    """Default dispatch_fn for scheduler-under-test (never reached)."""
    return None


class TestUnloadContention:
    """scheduler.unload_model_admin returns a discriminated outcome that the
    admin route maps to HTTP statuses.  HTTP mapping is pinned in
    test_server_admin.py; here we pin the underlying invariants."""

    @pytest.mark.asyncio
    async def test_inflight_blocks_unload_without_calling_tracker(self) -> None:
        """In-flight model -> outcome "inflight"; tracker.unload_model never called."""
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=True,
        ) as unload_mock:
            sched = Scheduler(
                config=cfg,
                queue=queue,
                vram_tracker=tracker,
                dispatch_fn=_noop_dispatch,
                has_inflight_fn=lambda m: m == "qwen3:14b",  # qwen3 is busy
                inflight_count_fn=lambda: 1,
            )

            status, details = await sched.unload_model_admin("qwen3:14b")

        assert status == "inflight"
        assert details["model"] == "qwen3:14b"
        assert "in-flight" in details["reason"].lower()
        unload_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_reservation_blocks_unload(self) -> None:
        """Model with active A2A reservation -> outcome "reserved"; tracker not called."""
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=True,
        ) as unload_mock:
            sched = Scheduler(
                config=cfg,
                queue=queue,
                vram_tracker=tracker,
                dispatch_fn=_noop_dispatch,
                reservation_check_fn=lambda m: m == "qwen3:14b",
            )

            status, details = await sched.unload_model_admin("qwen3:14b")

        assert status == "reserved"
        assert details["model"] == "qwen3:14b"
        assert "reservation" in details["reason"].lower()
        unload_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reservation_check_takes_priority_over_inflight(self) -> None:
        """If a model has BOTH a reservation and is in-flight, "reserved" wins.

        The admin path checks reservation_check_fn first; this pins that order.
        """
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        sched = Scheduler(
            config=cfg,
            queue=queue,
            vram_tracker=tracker,
            dispatch_fn=_noop_dispatch,
            reservation_check_fn=lambda m: True,
            has_inflight_fn=lambda m: True,
        )

        status, _ = await sched.unload_model_admin("qwen3:14b")
        assert status == "reserved"

    @pytest.mark.asyncio
    async def test_unload_confirmed_returns_unloaded(self) -> None:
        """Tracker confirms unload -> outcome "unloaded"; vram_manager release_model called."""
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        # Wire a VRAMManager-like stub to verify the ledger is released.
        vmgr = MagicMock(name="StubVRAMManager")
        vmgr.release_model = AsyncMock(return_value=9_900_000_000)
        vmgr.wait_for_vram_convergence = AsyncMock(return_value=True)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=True,
        ):
            sched = Scheduler(
                config=cfg,
                queue=queue,
                vram_tracker=tracker,
                dispatch_fn=_noop_dispatch,
                vram_manager=vmgr,
            )
            sched._current_model = "qwen3:14b"

            status, details = await sched.unload_model_admin("qwen3:14b")

        assert status == "unloaded"
        assert details["model"] == "qwen3:14b"
        # Ledger consistency: VRAMManager.release_model was called for this model.
        vmgr.release_model.assert_awaited_once_with("qwen3:14b")
        vmgr.wait_for_vram_convergence.assert_awaited_once()
        # current_model cleared so scheduler doesn't think it's still resident.
        assert sched.current_model is None

    @pytest.mark.asyncio
    async def test_unload_unconfirmed_returns_failed_without_releasing_ledger(self) -> None:
        """Tracker returns False (timeout / refused) -> outcome "failed"; ledger NOT released.

        This is critical: if we released the VRAMManager ledger when Ollama
        couldn't confirm the unload, the next preload would think VRAM is free
        and get a phantom 409 when actually loading.
        """
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)
        vmgr = MagicMock(name="StubVRAMManager")
        vmgr.release_model = AsyncMock(return_value=0)
        vmgr.wait_for_vram_convergence = AsyncMock(return_value=True)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=False,
        ):
            sched = Scheduler(
                config=cfg,
                queue=queue,
                vram_tracker=tracker,
                dispatch_fn=_noop_dispatch,
                vram_manager=vmgr,
            )
            sched._current_model = "qwen3:14b"

            status, details = await sched.unload_model_admin("qwen3:14b")

        assert status == "failed"
        assert "timeout" in details["reason"].lower() or "did not" in details["reason"].lower()
        # Ledger NOT released — model may still be resident
        vmgr.release_model.assert_not_awaited()
        # current_model NOT cleared
        assert sched.current_model == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_unload_without_vram_manager_still_returns_unloaded(self) -> None:
        """When vram_manager is None, the discriminated outcome still works."""
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        with patch.object(
            tracker, "unload_model", new_callable=AsyncMock, return_value=True,
        ):
            sched = Scheduler(
                config=cfg,
                queue=queue,
                vram_tracker=tracker,
                dispatch_fn=_noop_dispatch,
                vram_manager=None,
            )
            sched._current_model = "qwen3:14b"

            status, details = await sched.unload_model_admin("qwen3:14b")

        assert status == "unloaded"
        assert details["model"] == "qwen3:14b"
        assert sched.current_model is None


# ---------------------------------------------------------------------------
# Drain semantics — scheduler.drain / resume invariants
# ---------------------------------------------------------------------------


class TestSchedulerDrainResume:
    """drain() must flip is_draining True; resume() flips it back."""

    @pytest.mark.asyncio
    async def test_drain_sets_is_draining(self) -> None:
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        sched = Scheduler(
            config=cfg, queue=queue, vram_tracker=tracker,
            dispatch_fn=_noop_dispatch,
        )
        assert sched.is_draining is False
        await sched.drain()
        assert sched.is_draining is True

    @pytest.mark.asyncio
    async def test_resume_clears_is_draining(self) -> None:
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        sched = Scheduler(
            config=cfg, queue=queue, vram_tracker=tracker,
            dispatch_fn=_noop_dispatch,
        )
        await sched.drain()
        assert sched.is_draining is True
        await sched.resume()
        assert sched.is_draining is False

    @pytest.mark.asyncio
    async def test_drain_wakes_scheduler(self) -> None:
        """drain() must set the wake event so a sleeping loop notices immediately."""
        cfg = _scheduler_config()
        queue = AffinityQueue(cfg.scheduler)
        tracker = VRAMTracker(cfg)

        sched = Scheduler(
            config=cfg, queue=queue, vram_tracker=tracker,
            dispatch_fn=_noop_dispatch,
        )
        # Clear the event explicitly so we can assert drain() sets it.
        sched._wake_event.clear()
        assert not sched._wake_event.is_set()

        await sched.drain()
        assert sched._wake_event.is_set()

        # Sanity: the event can be awaited without blocking
        await asyncio.wait_for(sched._wake_event.wait(), timeout=0.1)
