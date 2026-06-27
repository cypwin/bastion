"""Integration tests for BASTION request serialization.

Proves that BASTION correctly queues, orders, and serializes multi-client,
multi-model workloads. Uses real AffinityQueue + Scheduler + OllamaProxy
with a mock Ollama HTTP backend (OllamaSimulator).

Unlike the existing unit tests (which mock Ollama), these tests verify the full
enqueue -> schedule -> dispatch -> done pipeline under concurrent load.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bastion.models import (
    BrokerConfig,
    CircuitBreakerConfig,
    GPUConfig,
    GPUStatus,
    LoadedModel,
    ModelInfo,
    OllamaConfig,
    PriorityConfig,
    ProxyConfig,
    QueuedRequest,
    SchedulerConfig,
    ServerConfig,
    SwapBrakeConfig,
)
from bastion.proxy import OllamaProxy
from bastion.queue import AffinityQueue
from bastion.scheduler import Scheduler
from bastion.vram import VRAMTracker

# ---------------------------------------------------------------------------
# OllamaSimulator — replaces proxy._http (the httpx.AsyncClient)
# ---------------------------------------------------------------------------


@dataclass
class RequestRecord:
    """Record of a single request processed by the simulator."""

    model: str
    endpoint: str
    use_mmap: bool | None
    streaming: bool
    start_time: float
    end_time: float = 0.0
    body: dict = field(default_factory=dict)


class OllamaSimulator:
    """Simulates the Ollama HTTP backend for integration testing.

    Tracks concurrent requests, records history, and provides configurable
    latency.  The key invariant: if serialization works, max_concurrent
    should never exceed 1.
    """

    def __init__(self, latency: float = 0.05) -> None:
        self.latency = latency
        self.records: list[RequestRecord] = []
        self._concurrent = 0
        self._max_concurrent = 0
        self._lock = asyncio.Lock()

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    async def _enter(self, body: dict, streaming: bool) -> RequestRecord:
        """Track a request entering the simulator."""
        async with self._lock:
            self._concurrent += 1
            if self._concurrent > self._max_concurrent:
                self._max_concurrent = self._concurrent
        record = RequestRecord(
            model=body.get("model", "unknown"),
            endpoint="/api/generate",
            use_mmap=body.get("options", {}).get("use_mmap"),
            streaming=streaming,
            start_time=time.monotonic(),
            body=body,
        )
        return record

    async def _exit(self, record: RequestRecord) -> None:
        """Track a request leaving the simulator."""
        record.end_time = time.monotonic()
        self.records.append(record)
        async with self._lock:
            self._concurrent -= 1

    async def post(
        self, url: str, *, content: bytes = b"", headers: dict = None, **kwargs
    ) -> _MockResponse:
        """Simulate a non-streaming POST (httpx.AsyncClient.post)."""
        body = json.loads(content) if content else {}
        record = await self._enter(body, streaming=False)
        await asyncio.sleep(self.latency)
        await self._exit(record)
        return _MockResponse(
            status_code=200,
            json_data={
                "response": "test output",
                "done": True,
                "model": body.get("model", ""),
            },
        )

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        content: bytes = b"",
        headers: dict = None,
        **kwargs,
    ) -> AsyncGenerator[_MockStreamResponse, None]:
        """Simulate a streaming POST (httpx.AsyncClient.stream)."""
        body = json.loads(content) if content else {}
        record = await self._enter(body, streaming=True)
        latency = self.latency

        async def _aiter_bytes() -> AsyncGenerator[bytes, None]:
            chunks = 3
            per_chunk = latency / chunks
            for i in range(chunks):
                await asyncio.sleep(per_chunk)
                chunk = {"response": f"token{i}", "done": i == chunks - 1}
                yield json.dumps(chunk).encode() + b"\n"

        resp = _MockStreamResponse(aiter_fn=_aiter_bytes)
        try:
            yield resp
        finally:
            await self._exit(record)

    # ── Assertion helpers ──────────────────────────────────────────────

    def assert_serialized(self) -> None:
        """Assert that no two requests overlapped in the simulator."""
        assert self._max_concurrent <= 1, (
            f"Serialization violated: max_concurrent={self._max_concurrent} "
            f"(expected <= 1). {len(self.records)} total requests."
        )

    def assert_all_use_mmap_false(self) -> None:
        """Assert every request had use_mmap: false."""
        for i, r in enumerate(self.records):
            assert r.use_mmap is False, (
                f"Request {i} for model '{r.model}' had use_mmap={r.use_mmap} "
                f"(expected False)"
            )

    def served_models(self) -> list[str]:
        """Return list of models served, in order."""
        return [r.model for r in self.records]


class _MockResponse:
    """Minimal mock of httpx.Response for non-streaming responses."""

    def __init__(self, status_code: int = 200, json_data: dict = None) -> None:
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json


class _MockStreamResponse:
    """Minimal mock of httpx streaming response."""

    def __init__(self, aiter_fn: Callable) -> None:
        self.status_code = 200
        self._aiter_fn = aiter_fn

    def aiter_bytes(self) -> AsyncGenerator[bytes, None]:
        return self._aiter_fn()


# ---------------------------------------------------------------------------
# Mock FastAPI Request
# ---------------------------------------------------------------------------


class _MockFastAPIRequest:
    """Minimal mock of starlette.requests.Request for OllamaProxy."""

    def __init__(
        self,
        path: str,
        body_bytes: bytes,
        headers: dict = None,
        method: str = "POST",
    ) -> None:
        self.url = type("URL", (), {"path": path})()
        self._body = body_bytes
        self.headers = headers or {}
        self.method = method

    async def body(self) -> bytes:
        return self._body


# ---------------------------------------------------------------------------
# Test configuration helpers
# ---------------------------------------------------------------------------


def make_config(
    cooldown: float = 0.05,
    affinity_bonus: float = 10.0,
    aging_rate: float = 2.0,
    max_queue_size: int = 64,
    loop_interval: float = 0.01,
) -> BrokerConfig:
    """Create a BrokerConfig tuned for integration tests."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434),
        gpu=GPUConfig(
            total_vram_gb=32.0,
            headroom_gb=6.0,
            max_temperature_c=82,
        ),
        proxy=ProxyConfig(
            inference_timeout_seconds=30.0,
            queue_timeout_seconds=30.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=cooldown,
            model_affinity_bonus=affinity_bonus,
            aging_rate=aging_rate,
            max_queue_size=max_queue_size,
            loop_interval_seconds=loop_interval,
            shutdown_timeout_seconds=5.0,
            # Brake-neutral so the startup just-swapped seed doesn't space the
            # first swap past these tests' windows (brake tests: test_swapbrake.py).
            swap_brake=SwapBrakeConfig(min_spacing_seconds=0.0, bucket_capacity=1_000_000.0),
        ),
        priorities=PriorityConfig(
            interactive=100.0,
            agent=50.0,
            pipeline=25.0,
            background=10.0,
        ),
        circuit_breaker=CircuitBreakerConfig(enabled=False),
        models={
            "alpha:7b": ModelInfo(vram_gb=5.0),
            "beta:7b": ModelInfo(vram_gb=5.0),
            "gamma:7b": ModelInfo(vram_gb=5.0),
            "delta:7b": ModelInfo(vram_gb=5.0),
        },
    )


# ---------------------------------------------------------------------------
# BastionHarness — wires real components with mock Ollama
# ---------------------------------------------------------------------------


class BastionHarness:
    """Integration test harness wiring real BASTION components.

    Uses real AffinityQueue, Scheduler, and OllamaProxy with a mock
    Ollama backend (OllamaSimulator).  Reproduces the exact
    _enqueue_request/_dispatch_request grant+done mechanism from server.py.
    """

    def __init__(self, config: BrokerConfig, simulator: OllamaSimulator) -> None:
        self.config = config
        self.simulator = simulator

        # Real components
        self.queue = AffinityQueue(config.scheduler)
        self.vram = VRAMTracker(config)

        # Local grant/done state (mirrors server.py module globals)
        self._pending_grants: dict[str, asyncio.Event] = {}
        self._pending_completions: dict[str, asyncio.Event] = {}

        # Created in start()
        self.proxy: OllamaProxy | None = None
        self.scheduler: Scheduler | None = None

    async def _enqueue_request(
        self, request: QueuedRequest,
    ) -> tuple[asyncio.Event, Callable[[], None]]:
        """Mirror of server._enqueue_request with local state."""
        grant_event = asyncio.Event()
        done_event = asyncio.Event()
        self._pending_grants[request.id] = grant_event
        self._pending_completions[request.id] = done_event

        accepted = self.queue.enqueue(request)
        if not accepted:
            self._pending_grants.pop(request.id, None)
            self._pending_completions.pop(request.id, None)
            raise RuntimeError("Queue full")

        if self.scheduler:
            self.scheduler.notify()

        def done_fn() -> None:
            evt = self._pending_completions.pop(request.id, None)
            if evt:
                evt.set()

        def cancel_fn() -> None:
            self._pending_grants.pop(request.id, None)
            completion_evt = self._pending_completions.pop(request.id, None)
            if completion_evt:
                completion_evt.set()
            self.queue.cancel(request.id)

        return grant_event, done_fn, cancel_fn

    async def _dispatch_request(self, request: QueuedRequest, needs_swap: bool = True) -> None:
        """Mirror of server._dispatch_request with local state.

        Always blocks (serialized) — the serialization tests verify that
        the harness properly serializes requests. Concurrent dispatch
        behavior is tested in test_scheduler.py.
        """
        grant_event = self._pending_grants.pop(request.id, None)
        done_event = self._pending_completions.get(request.id)

        if grant_event is not None:
            grant_event.set()
            if done_event is not None:
                timeout = self.config.proxy.inference_timeout_seconds + 60.0
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=timeout)
                except TimeoutError:
                    self._pending_completions.pop(request.id, None)

    async def start(self) -> None:
        """Start the harness (proxy + scheduler)."""
        self.proxy = OllamaProxy(
            self.config,
            enqueue_fn=self._enqueue_request,
            record_fn=lambda **kwargs: None,
        )
        # Replace the real httpx client with our simulator
        self.proxy._http = self.simulator
        # Disable circuit breaker for tests
        self.proxy.circuit_breaker = None

        self.scheduler = Scheduler(
            config=self.config,
            queue=self.queue,
            vram_tracker=self.vram,
            dispatch_fn=self._dispatch_request,
        )
        await self.scheduler.start()

    async def stop(self) -> None:
        """Stop the harness."""
        if self.scheduler:
            await self.scheduler.stop()
        for evt in self._pending_grants.values():
            evt.set()
        self._pending_grants.clear()
        for evt in self._pending_completions.values():
            evt.set()
        self._pending_completions.clear()

    def _make_request(
        self,
        model: str,
        priority: float = 50.0,
        stream: bool = False,
    ) -> _MockFastAPIRequest:
        """Create a mock FastAPI Request for the proxy."""
        payload = {
            "model": model,
            "prompt": f"p={priority}",
            "stream": stream,
        }
        body = json.dumps(payload).encode()

        # Map priority value to tier header
        if priority >= 100:
            tier_str = "interactive"
        elif priority >= 50:
            tier_str = "agent"
        elif priority >= 25:
            tier_str = "pipeline"
        else:
            tier_str = "background"

        headers = {
            "content-type": "application/json",
            "user-agent": "test-client",
            "x-broker-priority": tier_str,
        }
        return _MockFastAPIRequest(
            path="/api/generate",
            body_bytes=body,
            headers=headers,
        )

    async def send(
        self,
        model: str,
        priority: float = 50.0,
        stream: bool = False,
    ) -> Any:
        """Send one request through the full pipeline.

        For streaming responses, drains body_iterator to trigger done_fn.
        """
        request = self._make_request(model, priority, stream)
        response = await self.proxy.handle_request(request)

        # For streaming responses, drain the body_iterator so the
        # generator runs to completion and done_fn() fires.
        if hasattr(response, "body_iterator"):
            async for _ in response.body_iterator:
                pass

        return response

    async def send_many(
        self,
        specs: list[tuple[str, float]],
        stream: bool = False,
    ) -> list[Any]:
        """Send N concurrent requests via asyncio.gather.

        specs: List of (model, priority) tuples.
        """
        tasks = [self.send(model, priority, stream) for model, priority in specs]
        return await asyncio.gather(*tasks)


@asynccontextmanager
async def running_harness(
    config: BrokerConfig,
    simulator: OllamaSimulator,
    all_resident: bool = True,
) -> AsyncGenerator[BastionHarness, None]:
    """Async context manager: patches dependencies, starts/stops harness."""
    harness = BastionHarness(config, simulator)

    # Build mock loaded models based on all_resident flag
    if all_resident:
        loaded = [
            LoadedModel(name=name, size_bytes=0, vram_gb=info.vram_gb)
            for name, info in config.models.items()
        ]
    else:
        loaded = []

    resident_names = {m.name for m in loaded} if all_resident else set()

    async def mock_is_resident(model_name: str) -> bool:
        return all_resident

    async def mock_get_resident_models() -> set[str]:
        return resident_names

    with patch("bastion.scheduler.check_gpu_safe", AsyncMock(return_value=(True, "OK"))), \
         patch("bastion.scheduler.query_gpu_status",
               AsyncMock(return_value=GPUStatus(temperature_c=50))), \
         patch("bastion.proxy.audit"), \
         patch("bastion.scheduler.audit"), \
         patch.object(
             harness.vram, "get_loaded_models",
             new_callable=AsyncMock, return_value=loaded,
         ), \
         patch.object(
             harness.vram, "can_load_model",
             new_callable=AsyncMock, return_value=(True, "OK"),
         ), \
         patch.object(
             harness.vram, "get_loaded_vram_gb",
             new_callable=AsyncMock, return_value=0.0,
         ), \
         patch.object(
             harness.vram.residency_cache, "is_model_resident",
             new_callable=AsyncMock, side_effect=mock_is_resident,
         ), \
         patch.object(
             harness.vram.residency_cache, "get_resident_models",
             new_callable=AsyncMock, side_effect=mock_get_resident_models,
         ):
        await harness.start()
        try:
            yield harness
        finally:
            await harness.stop()


# ===========================================================================
# Test Classes
# ===========================================================================


class TestSerialization:
    """Verify max_concurrent == 1 under various concurrent request patterns."""

    @pytest.mark.asyncio
    async def test_same_model_concurrent(self) -> None:
        """5 concurrent requests for the same model — never overlap."""
        sim = OllamaSimulator(latency=0.05)
        config = make_config()

        async with running_harness(config, sim) as harness:
            await harness.send_many([("alpha:7b", 50)] * 5)

        assert len(sim.records) == 5
        sim.assert_serialized()

    @pytest.mark.asyncio
    async def test_different_models_concurrent(self) -> None:
        """5 concurrent requests for different models — never overlap."""
        sim = OllamaSimulator(latency=0.05)
        config = make_config()

        async with running_harness(config, sim) as harness:
            await harness.send_many([
                ("alpha:7b", 50),
                ("beta:7b", 50),
                ("gamma:7b", 50),
                ("delta:7b", 50),
                ("alpha:7b", 50),
            ])

        assert len(sim.records) == 5
        sim.assert_serialized()

    @pytest.mark.asyncio
    async def test_burst_of_ten(self) -> None:
        """10 concurrent requests — serialized despite burst."""
        sim = OllamaSimulator(latency=0.05)
        config = make_config()

        async with running_harness(config, sim) as harness:
            specs = [
                (f"{'alpha' if i % 2 == 0 else 'beta'}:7b", 50)
                for i in range(10)
            ]
            await harness.send_many(specs)

        assert len(sim.records) == 10
        sim.assert_serialized()

    @pytest.mark.asyncio
    async def test_streaming_serialized(self) -> None:
        """5 concurrent streaming requests — never overlap."""
        sim = OllamaSimulator(latency=0.05)
        config = make_config()

        async with running_harness(config, sim) as harness:
            await harness.send_many(
                [("alpha:7b", 50)] * 5,
                stream=True,
            )

        assert len(sim.records) == 5
        sim.assert_serialized()
        assert all(r.streaming for r in sim.records)

    @pytest.mark.asyncio
    async def test_mixed_stream_nonstream(self) -> None:
        """Mix of streaming and non-streaming — never overlap."""
        sim = OllamaSimulator(latency=0.05)
        config = make_config()

        async with running_harness(config, sim) as harness:
            tasks = [
                harness.send("alpha:7b", 50, stream=True),
                harness.send("alpha:7b", 50, stream=False),
                harness.send("beta:7b", 50, stream=True),
                harness.send("beta:7b", 50, stream=False),
                harness.send("alpha:7b", 50, stream=True),
            ]
            await asyncio.gather(*tasks)

        assert len(sim.records) == 5
        sim.assert_serialized()


class TestModelAffinity:
    """Verify same-model requests cluster via affinity bonus."""

    @pytest.mark.asyncio
    async def test_same_model_clusters(self) -> None:
        """With current model = alpha, alpha requests should be served
        before beta (affinity_bonus=10 gives alpha a priority edge).
        """
        sim = OllamaSimulator(latency=0.2)
        config = make_config(affinity_bonus=10.0)

        async with running_harness(config, sim) as harness:
            # Current model starts as alpha:7b (first model in dict,
            # set by _sync_current_model picking the first max-vram model)
            assert harness.scheduler._current_model == "alpha:7b"

            # Use a blocker to ensure all requests are queued before scheduling
            blocker = asyncio.create_task(harness.send("alpha:7b", 50))
            await asyncio.sleep(0.05)

            # Submit 2 alpha and 2 beta while blocker is processing
            remaining = asyncio.gather(
                harness.send("alpha:7b", 50),
                harness.send("beta:7b", 50),
                harness.send("alpha:7b", 50),
                harness.send("beta:7b", 50),
            )
            await asyncio.gather(blocker, remaining)

        assert len(sim.records) == 5
        sim.assert_serialized()

        # Alpha requests should cluster first (all alphas before any beta)
        models = sim.served_models()
        alpha_indices = [i for i, m in enumerate(models) if m == "alpha:7b"]
        beta_indices = [i for i, m in enumerate(models) if m == "beta:7b"]
        assert max(alpha_indices) < min(beta_indices), (
            f"Alpha requests should cluster before beta. Order: {models}"
        )

    @pytest.mark.asyncio
    async def test_affinity_drains_before_swap(self) -> None:
        """Affinity bonus keeps current model draining before swapping."""
        sim = OllamaSimulator(latency=0.1)
        config = make_config(affinity_bonus=10.0)

        async with running_harness(config, sim) as harness:
            assert harness.scheduler._current_model == "alpha:7b"

            # Blocker to queue everything before scheduling
            blocker = asyncio.create_task(harness.send("alpha:7b", 50))
            await asyncio.sleep(0.05)

            # 3 alpha + 1 beta queued while blocker is in-flight
            remaining = asyncio.gather(
                harness.send("alpha:7b", 50),
                harness.send("alpha:7b", 50),
                harness.send("beta:7b", 50),
                harness.send("alpha:7b", 50),
            )
            await asyncio.gather(blocker, remaining)

        models = sim.served_models()
        alpha_indices = [i for i, m in enumerate(models) if m == "alpha:7b"]
        beta_indices = [i for i, m in enumerate(models) if m == "beta:7b"]
        assert max(alpha_indices) < min(beta_indices), (
            f"Alpha should drain completely before beta. Order: {models}"
        )


class TestPriorityOrdering:
    """Verify priority-based ordering and aging."""

    @pytest.mark.asyncio
    async def test_interactive_before_background(self) -> None:
        """Interactive (100) is served before background (10)."""
        sim = OllamaSimulator(latency=0.2)
        config = make_config()

        async with running_harness(config, sim) as harness:
            # Blocker keeps scheduler busy so both bg and interactive
            # are in the queue when scheduler picks next
            blocker = asyncio.create_task(harness.send("alpha:7b", 50))
            await asyncio.sleep(0.05)

            bg = asyncio.create_task(harness.send("alpha:7b", 10))
            inter = asyncio.create_task(harness.send("alpha:7b", 100))

            await asyncio.gather(blocker, bg, inter)

        assert len(sim.records) == 3
        sim.assert_serialized()

        # Order: blocker (agent/50), then interactive (100), then background (10)
        prompts = [r.body.get("prompt") for r in sim.records]
        assert prompts[0] == "p=50"
        assert prompts[1] == "p=100"
        assert prompts[2] == "p=10"

    @pytest.mark.asyncio
    async def test_aging_prevents_starvation(self) -> None:
        """An old background request eventually beats a fresh agent request."""
        sim = OllamaSimulator(latency=0.5)
        config = make_config(aging_rate=200.0)

        async with running_harness(config, sim) as harness:
            # Blocker keeps scheduler busy for 0.5s
            blocker = asyncio.create_task(harness.send("alpha:7b", 50))
            await asyncio.sleep(0.05)

            # Background enqueued early — will age while blocker runs
            bg = asyncio.create_task(harness.send("alpha:7b", 10))
            await asyncio.sleep(0.3)  # bg ages ~0.3s: 10 + 0.3*200 = 70

            # Agent enqueued late — higher base priority but no aging
            agent = asyncio.create_task(harness.send("alpha:7b", 50))

            await asyncio.gather(blocker, bg, agent)

        assert len(sim.records) == 3
        sim.assert_serialized()

        # At dispatch time: bg effective ~= 10 + 0.45*200 = 100
        #                   agent effective ~= 50 + 0.15*200 = 80
        # bg wins despite lower base priority
        prompts = [r.body.get("prompt") for r in sim.records]
        assert prompts[0] == "p=50"   # blocker
        assert prompts[1] == "p=10"   # aged background (beats agent)
        assert prompts[2] == "p=50"   # agent


class TestModelJuggling:
    """Verify multi-model scheduling with swaps and cooldowns."""

    @pytest.mark.asyncio
    async def test_four_model_round_robin(self) -> None:
        """4 different models all get served."""
        sim = OllamaSimulator(latency=0.05)
        config = make_config(cooldown=0.02)

        async with running_harness(config, sim, all_resident=False) as harness:
            await harness.send_many([
                ("alpha:7b", 50),
                ("beta:7b", 50),
                ("gamma:7b", 50),
                ("delta:7b", 50),
            ])

        assert len(sim.records) == 4
        sim.assert_serialized()
        assert set(sim.served_models()) == {
            "alpha:7b", "beta:7b", "gamma:7b", "delta:7b",
        }

    @pytest.mark.asyncio
    async def test_cooldown_enforced(self) -> None:
        """Non-resident model swaps respect the brake's min-spacing floor."""
        spacing = 0.15
        sim = OllamaSimulator(latency=0.05)
        config = make_config(cooldown=spacing)
        # The swap brake's min-spacing floor — not the legacy cooldown — paces
        # non-resident swaps under the brake architecture. Set it explicitly so
        # this test verifies the real spacing mechanism (make_config otherwise
        # defaults to a brake-neutral config for the other serialization tests).
        config.scheduler.swap_brake = SwapBrakeConfig(min_spacing_seconds=spacing)

        async with running_harness(config, sim, all_resident=False) as harness:
            await harness.send_many([
                ("alpha:7b", 50),
                ("beta:7b", 50),
            ])

        assert len(sim.records) == 2
        sim.assert_serialized()

        # Gap between first and second request entering the simulator should be
        # at least the brake's min-spacing (with tolerance for timing).
        gap = sim.records[1].start_time - sim.records[0].start_time
        assert gap >= spacing - 0.03, (
            f"Swap spacing not enforced: gap={gap:.3f}s, "
            f"expected >= {spacing - 0.03:.3f}s"
        )


class TestUseMmapSafety:
    """Verify use_mmap: false is injected into every request."""

    @pytest.mark.asyncio
    async def test_all_requests_use_mmap_false(self) -> None:
        """Every request (all models, priorities, stream modes)
        gets use_mmap: false.
        """
        sim = OllamaSimulator(latency=0.05)
        config = make_config()

        async with running_harness(config, sim) as harness:
            tasks = [
                harness.send("alpha:7b", 100, stream=False),
                harness.send("beta:7b", 50, stream=True),
                harness.send("gamma:7b", 25, stream=False),
                harness.send("delta:7b", 10, stream=True),
                harness.send("alpha:7b", 50, stream=True),
                harness.send("beta:7b", 100, stream=False),
            ]
            await asyncio.gather(*tasks)

        assert len(sim.records) == 6
        sim.assert_serialized()
        sim.assert_all_use_mmap_false()
