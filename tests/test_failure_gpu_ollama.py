"""End-to-end failure-mode contract tests.

Pins the safety guarantees that justify BASTION's existence:

  A. GPU hot (>= max_temperature_c)   -> scheduler holds dispatch.
  B. GPU unavailable (nvidia-smi N/A) -> graceful degradation, no crash.
  C. Ollama down (connect refused)    -> circuit breaker opens after threshold.
  D. Ollama 5xx                       -> counts toward breaker opening.
  E. Recovery                         -> half-open probe closes or reopens.

These are integration-level tests covering the scheduler->dispatch path and
the proxy->circuit_breaker path together; the underlying unit-level state
machine has separate coverage in :mod:`tests.test_circuitbreaker`.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from bastion.circuitbreaker import (
    CircuitBreaker,
    CircuitBreakerTransport,
    CircuitOpenError,
    OllamaBackendError,
)
from bastion.models import (
    BrokerConfig,
    CircuitBreakerConfig,
    GPUConfig,
    GPUStatus,
    LoadedModel,
    ModelInfo,
    PriorityTier,
    SchedulerConfig,
)
from bastion.proxy import OllamaProxy
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAMTracker
from tests.conftest import make_request

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _safe_status() -> GPUStatus:
    return GPUStatus(
        temperature_c=55,
        vram_used_mb=8000,
        vram_free_mb=24000,
        vram_total_mb=32000,
        power_draw_watts=180.0,
    )


def _hot_status() -> GPUStatus:
    return GPUStatus(
        temperature_c=90,
        vram_used_mb=8000,
        vram_free_mb=24000,
        vram_total_mb=32000,
        power_draw_watts=200.0,
    )


def _unavailable_status() -> GPUStatus:
    """Mirrors what query_gpu_status() returns when nvidia-smi is missing."""
    return GPUStatus()  # All fields None.


def _failure_config() -> BrokerConfig:
    """Scheduler config tuned for fast failure-mode tests."""
    return BrokerConfig(
        gpu=GPUConfig(
            total_vram_gb=32.0,
            headroom_gb=6.0,
            max_temperature_c=82,
            max_power_watts=450.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=32,
            gpu_unsafe_backoff_seconds=0.1,
            loop_interval_seconds=0.05,
        ),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3),
            "mistral-nemo:12b": ModelInfo(vram_gb=8.1),
        },
    )


def _make_fake_fastapi_request(
    path: str = "/api/generate",
    method: str = "POST",
    body: bytes = b'{"model": "qwen3:14b", "prompt": "hi", "stream": false}',
    headers: dict | None = None,
) -> MagicMock:
    """Mock fastapi.Request matching the shape OllamaProxy reads."""
    req = MagicMock()
    req.url.path = path
    req.method = method
    req.body = AsyncMock(return_value=body)
    req.headers = headers or {"user-agent": "failure-mode-test/1.0"}
    return req


# ---------------------------------------------------------------------------
# A. GPU hot — scheduler holds dispatch
# ---------------------------------------------------------------------------


class TestGPUHotHoldsDispatch:
    """When GPU temp >= max_temperature_c, scheduler must not dispatch new work."""

    @pytest.mark.asyncio
    async def test_scheduler_holds_load_when_gpu_hot(self) -> None:
        """Hot GPU -> queued request stays queued, dispatch not called."""
        config = _failure_config()
        log: list = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            log.append(request)

        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)

        queue.enqueue(make_request(model="qwen3:14b", tier=PriorityTier.INTERACTIVE))

        with (
            patch.object(
                tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[],
            ),
            patch(
                "bastion.health.query_gpu_status",
                AsyncMock(return_value=_hot_status()),
            ),
        ):
            sched = Scheduler(config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()
            # Give the loop multiple ticks; check_gpu_safe must block dispatch.
            await asyncio.sleep(0.3)
            await sched.stop()

        assert log == [], "Hot GPU must not dispatch"
        # Request should remain in the queue (queue depth grows / stays > 0).
        assert queue.total_size == 1
        assert sched.total_dispatched == 0

    @pytest.mark.asyncio
    async def test_gpu_hot_does_not_evict_in_use_model(self) -> None:
        """The hot-GPU guard fires before any eviction or swap decision.

        Pins the contract: an already-loaded model continues to occupy VRAM —
        we don't tear it down just because the GPU is hot.  (Eviction is a
        write to GPU that itself stresses the hardware; doing it while the
        GPU is hot would defeat the safety guarantee.)
        """
        config = _failure_config()
        dispatched: list = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            dispatched.append(request)

        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)
        # An already-loaded model exists; if the scheduler were to evict it
        # we'd see a swap counter bump.
        loaded = LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3, details={})

        # Queue requests a *different* model — would normally trigger eviction.
        queue.enqueue(make_request(model="mistral-nemo:12b"))

        with (
            patch.object(
                tracker, "get_loaded_models",
                new_callable=AsyncMock, return_value=[loaded],
            ),
            patch(
                "bastion.health.query_gpu_status",
                AsyncMock(return_value=_hot_status()),
            ),
        ):
            sched = Scheduler(config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()
            await asyncio.sleep(0.3)
            unloads_attempted = sched.total_swaps
            await sched.stop()

        assert dispatched == []
        assert unloads_attempted == 0, "No swaps may occur while GPU is hot"

    @pytest.mark.asyncio
    async def test_gpu_cools_resumes_dispatch(self) -> None:
        """Once the temperature drops below the limit, dispatch resumes."""
        config = _failure_config()
        dispatched: list = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            dispatched.append(request)

        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)
        queue.enqueue(make_request(model="qwen3:14b"))

        # Flip from hot to safe after the first invocation.
        states = [_hot_status(), _hot_status(), _safe_status()]
        call_idx = {"i": 0}

        async def _flip_status() -> GPUStatus:
            i = call_idx["i"]
            call_idx["i"] = min(i + 1, len(states) - 1)
            return states[i]

        with (
            patch.object(
                tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[],
            ),
            patch.object(
                tracker, "can_load_model",
                new_callable=AsyncMock, return_value=(True, "OK"),
            ),
            patch("bastion.health.query_gpu_status", side_effect=_flip_status),
        ):
            sched = Scheduler(config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()
            for _ in range(80):
                await asyncio.sleep(0.02)
                if dispatched:
                    break
                sched.notify()
            await sched.stop()

        assert len(dispatched) == 1, "Cool GPU must release the request"


# ---------------------------------------------------------------------------
# B. GPU unavailable — graceful degradation
# ---------------------------------------------------------------------------


class TestGPUUnavailableGracefulDegradation:
    """nvidia-smi missing/timeout -> scheduler does not crash, dispatch proceeds.

    Contract (from health.py): when fields are None we treat them as
    "unknown -> safe" so the broker keeps serving on hardware without
    nvidia-smi (AMD, CPU-only fallback, broken driver).
    """

    @pytest.mark.asyncio
    async def test_gpu_unavailable_does_not_crash_scheduler(self) -> None:
        """All-None GPUStatus -> check_gpu_safe returns True ('OK') and
        the scheduler dispatches normally.
        """
        config = _failure_config()
        dispatched: list = []

        async def dispatch_fn(request, needs_swap=True) -> None:
            dispatched.append(request)

        queue = AffinityQueue(config.scheduler)
        tracker = VRAMTracker(config)
        queue.enqueue(make_request(model="qwen3:14b"))

        with (
            patch.object(
                tracker, "get_loaded_models", new_callable=AsyncMock, return_value=[],
            ),
            patch.object(
                tracker, "can_load_model",
                new_callable=AsyncMock, return_value=(True, "OK"),
            ),
            patch(
                "bastion.health.query_gpu_status",
                AsyncMock(return_value=_unavailable_status()),
            ),
        ):
            sched = Scheduler(config, queue, tracker, dispatch_fn)
            await sched.start()
            sched.notify()
            for _ in range(80):
                await asyncio.sleep(0.02)
                if dispatched:
                    break
                sched.notify()
            await sched.stop()

        assert len(dispatched) == 1, (
            "Graceful degradation: unknown GPU state must not block work."
        )

    @pytest.mark.asyncio
    async def test_gpu_unavailable_surfaces_in_status_endpoint(
        self, app_with_stub_scheduler,
    ) -> None:
        """/broker/status reflects the unavailable GPU state.

        The contract we verify: when nvidia-smi is absent, the status payload
        still returns 200 with a GPUStatus whose temperature/VRAM fields are
        ``None`` (rather than 500-ing or crashing the handler).
        """
        with patch(
            "bastion.server.query_gpu_status",
            AsyncMock(return_value=_unavailable_status()),
        ):
            resp = app_with_stub_scheduler.get("/broker/status")

        assert resp.status_code == 200
        body = resp.json()
        gpu = body["gpu"]
        assert gpu["temperature_c"] is None
        assert gpu["vram_used_mb"] is None
        # gpu_is_safe falls back to True ("safe" given no contradicting data).
        assert body["gpu_is_safe"] is True


# ---------------------------------------------------------------------------
# C. Ollama down — circuit breaker opens after threshold
# ---------------------------------------------------------------------------


class TestOllamaConnectFailuresOpenCircuit:
    """Connection refused / connect timeout count toward CB opening."""

    @pytest.mark.asyncio
    async def test_ollama_connection_refused_threshold_opens_circuit(self) -> None:
        """N consecutive ConnectError -> CB transitions CLOSED -> OPEN."""
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=3, recovery_timeout=1.0,
        )
        breaker = CircuitBreaker(cb_cfg)

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused"),
        )
        transport = CircuitBreakerTransport(breaker, inner=inner)

        req = httpx.Request("POST", "http://localhost:11435/api/generate")

        assert breaker.state == "closed"
        for i in range(3):
            with pytest.raises(httpx.ConnectError):
                await transport.handle_async_request(req)
            # Up through (threshold-1) failures we remain closed.
            if i < 2:
                assert breaker.state == "closed"

        assert breaker.state == "open"
        assert breaker._consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_circuit_open_returns_fast_fail(self) -> None:
        """While OPEN, transport raises CircuitOpenError without calling inner.

        This is the user-visible "fail fast" contract — no waiting for TCP
        retries when we already know Ollama is down.
        """
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=2, recovery_timeout=60.0,
        )
        breaker = CircuitBreaker(cb_cfg)
        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker.state == "open"

        inner = AsyncMock()
        transport = CircuitBreakerTransport(breaker, inner=inner)

        req = httpx.Request("GET", "http://localhost:11435/api/tags")
        with pytest.raises(CircuitOpenError) as exc:
            await transport.handle_async_request(req)
        assert exc.value.recovery_remaining > 0
        inner.handle_async_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_proxy_passthrough_502_on_connect_error(self) -> None:
        """End-to-end envelope: with CB CLOSED, a ConnectError surfaces as
        HTTP 502 to the client (with the error message), and the breaker
        records the failure.
        """
        config = BrokerConfig()
        config.circuit_breaker = CircuitBreakerConfig(
            enabled=True, failure_threshold=3, recovery_timeout=60.0,
        )
        proxy = OllamaProxy(config)
        assert proxy.circuit_breaker is not None

        # /api/tags is a passthrough endpoint — uses _http.request().
        with patch.object(
            proxy._http, "request",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            req = _make_fake_fastapi_request(
                path="/api/tags", method="GET", body=b"",
            )
            resp = await proxy.handle_request(req)

        assert resp.status_code == 502
        assert proxy.circuit_breaker._consecutive_failures == 1


# ---------------------------------------------------------------------------
# D. Ollama 5xx — counts toward CB opening
# ---------------------------------------------------------------------------


class TestOllama5xxCountsTowardOpen:
    """Server errors from Ollama must trip the breaker just like network errors."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [500, 502, 503, 504])
    async def test_5xx_records_failure(self, status_code: int) -> None:
        """Any 5xx response increments the consecutive failure counter."""
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=5, recovery_timeout=1.0,
        )
        breaker = CircuitBreaker(cb_cfg)

        req = httpx.Request("POST", "http://localhost:11435/api/generate")
        resp = httpx.Response(status_code=status_code, request=req)
        resp.stream = None  # type: ignore[assignment]

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(return_value=resp)
        transport = CircuitBreakerTransport(breaker, inner=inner)

        with pytest.raises(OllamaBackendError) as exc:
            await transport.handle_async_request(req)
        assert exc.value.status_code == status_code
        assert breaker._consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_5xx_threshold_opens_circuit(self) -> None:
        """Repeated 5xx responses (>= threshold) trip the CB to OPEN."""
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=3, recovery_timeout=1.0,
        )
        breaker = CircuitBreaker(cb_cfg)

        req = httpx.Request("POST", "http://localhost:11435/api/generate")
        resp = httpx.Response(status_code=503, request=req)
        resp.stream = None  # type: ignore[assignment]

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(return_value=resp)
        transport = CircuitBreakerTransport(breaker, inner=inner)

        for _ in range(3):
            with pytest.raises(OllamaBackendError):
                await transport.handle_async_request(req)
        assert breaker.state == "open"

        # Subsequent call fails fast (without calling inner) — verifies the
        # 5xx-induced OPEN state is honored on the very next request.
        inner.handle_async_request.reset_mock()
        with pytest.raises(CircuitOpenError):
            await transport.handle_async_request(req)
        inner.handle_async_request.assert_not_called()


# ---------------------------------------------------------------------------
# E. Recovery — HALF_OPEN probe
# ---------------------------------------------------------------------------


class TestRecoveryFromOpen:
    """After recovery_timeout, the breaker enters HALF_OPEN.  One probe
    decides whether to close (success) or reopen (failure)."""

    @pytest.mark.asyncio
    async def test_half_open_probe_success_closes_circuit(self) -> None:
        """A successful call while HALF_OPEN transitions back to CLOSED."""
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=3, recovery_timeout=0.5,
        )
        breaker = CircuitBreaker(cb_cfg)
        # Trip the breaker.
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state == "open"

        # Simulate recovery_timeout passing by rewinding ``_opened_at`` —
        # this mirrors the unit-test approach in test_circuitbreaker.py and
        # avoids depending on real wall-clock sleeps.
        breaker._opened_at = time.monotonic() - cb_cfg.recovery_timeout - 0.1
        assert breaker.state == "half_open"

        req = httpx.Request("POST", "http://localhost:11435/api/generate")
        ok_resp = httpx.Response(status_code=200, request=req)
        ok_resp.stream = None  # type: ignore[assignment]

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(return_value=ok_resp)
        transport = CircuitBreakerTransport(breaker, inner=inner)

        resp = await transport.handle_async_request(req)
        assert resp.status_code == 200
        assert breaker.state == "closed"
        assert breaker._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_half_open_probe_failure_reopens_circuit(self) -> None:
        """A failed probe while HALF_OPEN moves the breaker back to OPEN
        and resets the recovery timer (verified by checking ``_opened_at``
        advanced)."""
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=3, recovery_timeout=0.5,
        )
        breaker = CircuitBreaker(cb_cfg)
        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state == "open"
        first_opened_at = breaker._opened_at

        # Force into half-open by rewinding.
        breaker._opened_at = time.monotonic() - cb_cfg.recovery_timeout - 0.1
        assert breaker.state == "half_open"

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(
            side_effect=httpx.ConnectError("still down"),
        )
        transport = CircuitBreakerTransport(breaker, inner=inner)

        req = httpx.Request("POST", "http://localhost:11435/api/generate")
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(req)

        assert breaker.state == "open"
        # Recovery timer must have been reset (advanced forward).
        assert breaker._opened_at > first_opened_at

    @pytest.mark.asyncio
    async def test_recovery_round_trip_closed_open_half_open_closed(self) -> None:
        """Full lifecycle: CLOSED -> OPEN (5xx threshold) -> HALF_OPEN
        (after timeout) -> CLOSED (probe success).

        This is the single end-to-end "did the recovery contract hold?"
        scenario; the other tests in this class pin individual transitions.
        """
        cb_cfg = CircuitBreakerConfig(
            enabled=True, failure_threshold=2, recovery_timeout=0.2,
        )
        breaker = CircuitBreaker(cb_cfg)

        req = httpx.Request("POST", "http://localhost:11435/api/generate")
        fail_resp = httpx.Response(status_code=502, request=req)
        fail_resp.stream = None  # type: ignore[assignment]
        ok_resp = httpx.Response(status_code=200, request=req)
        ok_resp.stream = None  # type: ignore[assignment]

        responses: list[httpx.Response | Exception] = [fail_resp, fail_resp, ok_resp]
        idx = {"i": 0}

        async def _respond(_req: httpx.Request) -> httpx.Response:
            i = idx["i"]
            idx["i"] += 1
            r = responses[i]
            if isinstance(r, Exception):
                raise r
            return r

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(side_effect=_respond)
        transport = CircuitBreakerTransport(breaker, inner=inner)

        # Phase 1: CLOSED -> OPEN via two 5xx
        for _ in range(2):
            with pytest.raises(OllamaBackendError):
                await transport.handle_async_request(req)
        assert breaker.state == "open"

        # Phase 2: rewind into HALF_OPEN
        breaker._opened_at = time.monotonic() - cb_cfg.recovery_timeout - 0.05
        assert breaker.state == "half_open"

        # Phase 3: probe success -> CLOSED
        resp = await transport.handle_async_request(req)
        assert resp.status_code == 200
        assert breaker.state == "closed"
        assert breaker._consecutive_failures == 0


# ---------------------------------------------------------------------------
# F. Disabled breaker — failures pass through
# ---------------------------------------------------------------------------


class TestDisabledBreakerContract:
    """Sanity: when the breaker is disabled, no state transition occurs and
    failures pass through.  Pins behavior so a misconfiguration doesn't get
    silently masked by an over-eager test fixture."""

    @pytest.mark.asyncio
    async def test_disabled_breaker_does_not_trip(self) -> None:
        cb_cfg = CircuitBreakerConfig(
            enabled=False, failure_threshold=1, recovery_timeout=1.0,
        )
        breaker = CircuitBreaker(cb_cfg)

        inner = AsyncMock()
        inner.handle_async_request = AsyncMock(
            side_effect=httpx.ConnectError("refused"),
        )
        transport = CircuitBreakerTransport(breaker, inner=inner)

        req = httpx.Request("POST", "http://localhost:11435/api/generate")
        # The transport short-circuits to the inner when disabled, so the
        # raw exception propagates without being recorded.
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(req)
        assert breaker.state == "closed"
