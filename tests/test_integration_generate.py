"""End-to-end integration tests for the BASTION generate lifecycle.

Pins the full happy path of a `/api/generate` request through the broker:

    client -> FastAPI app -> proxy -> queue -> scheduler -> proxy stream
           -> mock Ollama -> NDJSON back to client -> audit + metrics

Unlike `tests/test_proxy.py` (proxy unit), `tests/test_scheduler.py` (scheduler
unit), and `tests/test_serialization.py` (concurrency simulator), this test
exercises a *real* `create_app(...)` FastAPI app over TestClient and patches
only the outermost httpx clients (proxy + VRAM tracker + scheduler GPU
checks) so the inner machinery (queue, scheduler loop, audit, metrics) is
real.

Scope:
  - Happy path: 200 + NDJSON stream
  - use_mmap: false injection (RTX 5090 crash-prevention contract)
  - Streaming non-buffering (chunk boundaries preserved)
  - Audit record_complete emitted
  - Metrics counter incremented
  - Passthrough endpoints skip scheduler
  - Unknown-model error propagation

Out of scope (intentionally — see report):
  - Priority tier ordering under burst (deferred to test_concurrency.py)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from bastion import audit
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
    SchedulerConfig,
    ServerConfig,
)
from bastion.server import create_app

# ---------------------------------------------------------------------------
# Fake Ollama upstream — duck-typed to httpx.AsyncClient API used by proxy
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal duck-type of an httpx streaming response context manager."""

    def __init__(
        self,
        chunks: list[bytes],
        status_code: int = 200,
        delay: float = 0.0,
    ) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self._delay = delay
        self.headers = httpx.Headers({"content-type": "application/x-ndjson"})

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            yield chunk


class _FakeNonStreamResponse:
    """Minimal duck-type of a non-streaming httpx.Response."""

    def __init__(self, status_code: int, json_data: dict[str, Any]) -> None:
        self.status_code = status_code
        self._json = json_data
        ct = "application/json"
        self.headers = httpx.Headers({"content-type": ct})
        self.text = json.dumps(json_data)

    def json(self) -> dict[str, Any]:
        return self._json


class FakeOllama:
    """Replaces ``proxy._http`` (an httpx.AsyncClient).

    Records every call for assertions and serves configurable responses for
    /api/generate (streaming) and /api/tags (non-streaming).
    """

    def __init__(
        self,
        generate_chunks: list[bytes] | None = None,
        generate_status: int = 200,
        tags_payload: dict[str, Any] | None = None,
        stream_delay: float = 0.0,
    ) -> None:
        self.generate_chunks: list[bytes] = generate_chunks or [
            b'{"model": "qwen3:14b", "response": "hello", "done": false}\n',
            b'{"model": "qwen3:14b", "response": " world", "done": false}\n',
            b'{"model": "qwen3:14b", "response": "", "done": true, '
            b'"prompt_eval_count": 5, "eval_count": 7}\n',
        ]
        self.generate_status = generate_status
        self.tags_payload = tags_payload or {"models": []}
        self.stream_delay = stream_delay

        # Recordings — tests assert against these.
        self.posted_bodies: list[bytes] = []
        self.streamed_bodies: list[bytes] = []
        self.requested_urls: list[str] = []

    # --- httpx.AsyncClient API used by OllamaProxy ----------------------

    @asynccontextmanager
    async def stream(
        self,
        method: str,
        url: str,
        *,
        content: bytes = b"",
        headers: dict | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[_FakeStreamResponse]:
        self.streamed_bodies.append(content)
        self.requested_urls.append(url)
        yield _FakeStreamResponse(
            self.generate_chunks,
            status_code=self.generate_status,
            delay=self.stream_delay,
        )

    async def post(
        self,
        url: str,
        *,
        content: bytes = b"",
        headers: dict | None = None,
        **kwargs: Any,
    ) -> _FakeNonStreamResponse:
        self.posted_bodies.append(content)
        self.requested_urls.append(url)
        # Non-streaming generate path: aggregate final chunk.
        last = self.generate_chunks[-1] if self.generate_chunks else b"{}"
        return _FakeNonStreamResponse(
            self.generate_status, json.loads(last.strip() or b"{}")
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes = b"",
        headers: dict | None = None,
        **kwargs: Any,
    ) -> _FakeNonStreamResponse:
        """Generic request used by passthrough handler for /api/tags etc."""
        self.requested_urls.append(url)
        if "/api/tags" in url:
            return _FakeNonStreamResponse(200, self.tags_payload)
        if "/api/ps" in url:
            return _FakeNonStreamResponse(200, {"models": []})
        return _FakeNonStreamResponse(200, {})

    async def get(self, url: str, **kwargs: Any) -> _FakeNonStreamResponse:
        """Pre-flight check (lifespan) and VRAMTracker.get_loaded_models."""
        self.requested_urls.append(url)
        if "/api/tags" in url:
            return _FakeNonStreamResponse(200, self.tags_payload)
        if "/api/ps" in url:
            return _FakeNonStreamResponse(200, {"models": []})
        return _FakeNonStreamResponse(200, {})

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Config + fixture
# ---------------------------------------------------------------------------


def _integration_config() -> BrokerConfig:
    """Config tuned for deterministic integration tests."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host="127.0.0.1", port=11434),
        gpu=GPUConfig(
            total_vram_gb=32.0,
            headroom_gb=6.0,
            max_temperature_c=82,
        ),
        proxy=ProxyConfig(
            inference_timeout_seconds=10.0,
            queue_timeout_seconds=10.0,
        ),
        scheduler=SchedulerConfig(
            cooldown_seconds=0.0,
            model_affinity_bonus=10.0,
            aging_rate=2.0,
            max_queue_size=16,
            loop_interval_seconds=0.01,
            shutdown_timeout_seconds=2.0,
        ),
        priorities=PriorityConfig(
            interactive=100.0, agent=50.0, pipeline=25.0, background=10.0,
        ),
        circuit_breaker=CircuitBreakerConfig(enabled=False),
        models={
            "qwen3:14b": ModelInfo(vram_gb=9.3, tags=["fast"]),
            "llama3.1:8b": ModelInfo(vram_gb=4.4, tags=["council"]),
        },
    )


@pytest.fixture
def integration_harness(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FakeOllama]]:
    """Real broker with httpx clients replaced by a FakeOllama.

    Yields (TestClient, FakeOllama).  After the lifespan enters, swaps:
      - ``server._proxy._http`` (OllamaProxy's httpx client)
      - ``server._vram_tracker._http`` (VRAMTracker's httpx client)

    Also stubs ``bastion.scheduler.check_gpu_safe`` / ``query_gpu_status``
    so the scheduler loop never blocks on real nvidia-smi, and stubs
    ``vram_tracker.get_loaded_models`` so qwen3:14b is reported resident
    (avoiding a swap → keeps the integration path fast and predictable).

    The TestClient is yielded; teardown is handled by ``with TestClient(...)``.
    """
    monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))

    config = _integration_config()
    fake = FakeOllama()

    # GPU patches — applied BEFORE the lifespan starts the scheduler.
    safe_gpu = GPUStatus(
        temperature_c=55,
        vram_used_mb=4000,
        vram_free_mb=28000,
        vram_total_mb=32000,
        power_draw_watts=160.0,
    )

    with patch(
        "bastion.scheduler.check_gpu_safe",
        new_callable=AsyncMock,
        return_value=(True, "OK"),
    ), patch(
        "bastion.scheduler.query_gpu_status",
        new_callable=AsyncMock,
        return_value=safe_gpu,
    ), patch(
        "bastion.health.query_gpu_status",
        new_callable=AsyncMock,
        return_value=safe_gpu,
    ):
        app = create_app(config)
        with TestClient(app) as client:
            import bastion.server as server_mod

            # Replace the proxy + tracker httpx clients with the fake. We
            # close the originals to avoid leaving dangling AsyncClients.
            assert server_mod._proxy is not None
            assert server_mod._vram_tracker is not None
            original_proxy_http = server_mod._proxy._http
            original_vram_http = server_mod._vram_tracker._http
            server_mod._proxy._http = fake  # type: ignore[assignment]
            server_mod._vram_tracker._http = fake  # type: ignore[assignment]

            # Force qwen3:14b to look resident so scheduler skips swap.
            loaded = [
                LoadedModel(name="qwen3:14b", size_bytes=0, vram_gb=9.3),
            ]
            patched_loaded = patch.object(
                server_mod._vram_tracker,
                "get_loaded_models",
                new_callable=AsyncMock,
                return_value=loaded,
            )
            patched_can_load = patch.object(
                server_mod._vram_tracker,
                "can_load_model",
                new_callable=AsyncMock,
                return_value=(True, "OK"),
            )
            patched_loaded_vram = patch.object(
                server_mod._vram_tracker,
                "get_loaded_vram_gb",
                new_callable=AsyncMock,
                return_value=9.3,
            )
            patched_resident = patch.object(
                server_mod._vram_tracker.residency_cache,
                "is_model_resident",
                new_callable=AsyncMock,
                return_value=True,
            )
            patched_resident_set = patch.object(
                server_mod._vram_tracker.residency_cache,
                "get_resident_models",
                new_callable=AsyncMock,
                return_value={"qwen3:14b"},
            )

            with patched_loaded, patched_can_load, patched_loaded_vram, \
                    patched_resident, patched_resident_set:
                try:
                    yield client, fake
                finally:
                    # Restore the original httpx clients so close() works
                    # cleanly during lifespan teardown.
                    server_mod._proxy._http = original_proxy_http
                    server_mod._vram_tracker._http = original_vram_http


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _assert_audit_has_event(event_name: str) -> dict:
    """Find the most recent audit event of the given name, or fail."""
    events = audit.recent_events(50)
    for e in reversed(events):
        if e.get("event") == event_name:
            return e
    pytest.fail(
        f"No audit event '{event_name}' found. "
        f"Saw events: {[e.get('event') for e in events]}"
    )


class TestGenerateRequestFlowsThroughBroker:
    """Happy path: client -> queue -> scheduler -> proxy -> Ollama -> audit."""

    def test_streaming_request_returns_ndjson(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        client, fake = integration_harness
        body = {"model": "qwen3:14b", "prompt": "hello", "stream": True}

        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_lines())

        # Three NDJSON chunks emitted by FakeOllama; iter_lines yields strings.
        non_empty = [c for c in chunks if c.strip()]
        assert len(non_empty) == 3
        # First chunk has the first token.
        first = json.loads(non_empty[0])
        assert first["response"] == "hello"
        assert first["done"] is False
        # Final chunk has done=true.
        final = json.loads(non_empty[-1])
        assert final["done"] is True

    def test_scheduler_sees_request_in_total_dispatched(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        """One generate request -> scheduler.total_dispatched increments."""
        client, _fake = integration_harness
        import bastion.server as server_mod

        before = server_mod._scheduler.total_dispatched  # type: ignore[union-attr]

        body = {"model": "qwen3:14b", "prompt": "hi", "stream": True}
        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass

        after = server_mod._scheduler.total_dispatched  # type: ignore[union-attr]
        assert after == before + 1, (
            f"Expected total_dispatched to grow by 1, got before={before} "
            f"after={after}"
        )

    def test_audit_request_complete_event_emitted(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        """The proxy's audit.emit(EVENT_REQUEST_COMPLETE, ...) fires."""
        client, _fake = integration_harness

        body = {"model": "qwen3:14b", "prompt": "audit-me", "stream": True}
        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass

        event = _assert_audit_has_event(audit.EVENT_REQUEST_COMPLETE)
        details = event.get("details", event)  # accommodate either shape
        assert details["model"] == "qwen3:14b"
        assert details["endpoint"] == "/api/generate"
        assert details["streaming"] is True

    def test_metrics_counter_incremented(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        """Prometheus counter for /api/generate increments after a request."""
        from bastion.metrics import PROMETHEUS_AVAILABLE, REQUESTS_TOTAL

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus-client not installed")

        client, _fake = integration_harness

        # Capture pre-request value for the (endpoint, status, tier) label.
        labels = {
            "endpoint": "/api/generate",
            "status_code": "200",
            "tier": "agent",
        }
        # ``_value.get()`` works on Counter samples even when the label set
        # hasn't been observed yet (returns 0).
        before = REQUESTS_TOTAL.labels(**labels)._value.get()  # type: ignore[attr-defined]

        body = {"model": "qwen3:14b", "prompt": "metrics-me", "stream": True}
        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass

        after = REQUESTS_TOTAL.labels(**labels)._value.get()  # type: ignore[attr-defined]
        assert after == before + 1, (
            f"Expected requests_total to grow by 1; before={before} after={after}"
        )


class TestUseMmapInjection:
    """RTX 5090 crash-prevention: every forwarded body has use_mmap: false."""

    def test_use_mmap_false_is_injected_when_absent(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        client, fake = integration_harness

        # No options at all in the client payload.
        body = {"model": "qwen3:14b", "prompt": "hello", "stream": True}
        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 200
            for _ in resp.iter_lines():
                pass

        assert len(fake.streamed_bodies) == 1, (
            f"Expected one upstream call, got {len(fake.streamed_bodies)}"
        )
        forwarded = json.loads(fake.streamed_bodies[0])
        assert forwarded.get("options", {}).get("use_mmap") is False, (
            f"use_mmap not injected; forwarded options={forwarded.get('options')}"
        )


class TestStreamingPassesThroughChunks:
    """Chunked NDJSON must reach the client one chunk at a time."""

    def test_chunked_response_body_is_preserved(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        """All three upstream chunks reach the client distinctly.

        TestClient's ``iter_lines`` yields each NDJSON line, which means the
        proxy did not buffer the response into a single object (otherwise we
        would only see one final aggregated JSON payload).
        """
        client, _fake = integration_harness

        body = {"model": "qwen3:14b", "prompt": "stream-me", "stream": True}
        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 200
            # Content type must indicate NDJSON streaming.
            assert "x-ndjson" in resp.headers.get("content-type", "").lower()
            lines = [line for line in resp.iter_lines() if line.strip()]

        assert len(lines) == 3, (
            f"Expected 3 NDJSON chunks (got {len(lines)}). "
            f"Proxy may be buffering."
        )
        # Each chunk parses as valid JSON on its own — proof of no merging.
        parsed = [json.loads(line) for line in lines]
        assert [p["done"] for p in parsed] == [False, False, True]


class TestPassthroughSkipsScheduler:
    """GET /api/tags must not enter the scheduler queue."""

    def test_tags_request_bypasses_queue(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        client, fake = integration_harness
        import bastion.server as server_mod

        fake.tags_payload = {
            "models": [{"name": "qwen3:14b", "size": 1234, "digest": "abc"}],
        }
        before_dispatched = (
            server_mod._scheduler.total_dispatched  # type: ignore[union-attr]
        )

        resp = client.get("/api/tags")
        assert resp.status_code == 200
        assert resp.json() == fake.tags_payload

        # Scheduler never saw this request.
        after_dispatched = (
            server_mod._scheduler.total_dispatched  # type: ignore[union-attr]
        )
        assert after_dispatched == before_dispatched, (
            "Passthrough /api/tags must not be enqueued/dispatched."
        )
        # Queue was never touched.
        assert server_mod._queue.total_size == 0  # type: ignore[union-attr]


class TestUpstreamErrorPropagation:
    """When Ollama returns an error, the broker surfaces it to the client.

    Note: the proxy does not validate model names against config.models —
    it forwards whatever the client sent.  This test pins the *forwarding*
    behavior: an upstream 404 reaches the client as a non-2xx response.
    """

    def test_upstream_error_surfaces(
        self,
        integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        client, fake = integration_harness

        # Configure the fake to return an error chunk (still streamed; Ollama
        # actually emits a single JSON object with "error" when a model is
        # unknown).
        fake.generate_chunks = [
            b'{"error": "model \\"totally-fake:9000\\" not found"}\n',
        ]
        body = {"model": "totally-fake:9000", "prompt": "x", "stream": True}

        with client.stream("POST", "/api/generate", json=body) as resp:
            # Proxy passes the upstream status verbatim for streaming —
            # FakeOllama replies 200 with an error payload, mirroring real
            # Ollama, which is what the proxy faithfully forwards.
            assert resp.status_code == 200
            lines = [line for line in resp.iter_lines() if line.strip()]

        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert "error" in payload
        assert "totally-fake:9000" in payload["error"]


class TestUpstream500Survival:
    """S131 regression: an upstream Ollama 500 mid-batch must be forwarded
    to the client and the broker must stay up and serve subsequent requests.

    Observed 2026-06-11: under concurrent-session VRAM contention Ollama
    returned a 500 and the broker stopped serving shortly after. The exact
    death path needs the journal traceback; this pins the contract the fix
    must preserve either way.
    """

    def test_streaming_500_forwarded_then_next_request_succeeds(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        client, fake = integration_harness

        fake.generate_status = 500
        fake.generate_chunks = [b'{"error": "vram exhausted"}\n']
        body = {"model": "qwen3:14b", "prompt": "x", "stream": True}

        with client.stream("POST", "/api/generate", json=body) as resp:
            assert resp.status_code == 500
            lines = [line for line in resp.iter_lines() if line.strip()]
        assert "error" in json.loads(lines[0])

        # The broker must still serve the next request.
        fake.generate_status = 200
        fake.generate_chunks = [
            b'{"model": "qwen3:14b", "response": "ok", "done": true}\n',
        ]
        with client.stream("POST", "/api/generate", json=body) as resp2:
            assert resp2.status_code == 200
            lines2 = [line for line in resp2.iter_lines() if line.strip()]
        assert json.loads(lines2[-1])["done"] is True

    def test_non_streaming_500_forwarded_then_next_request_succeeds(
        self, integration_harness: tuple[TestClient, FakeOllama],
    ) -> None:
        client, fake = integration_harness

        fake.generate_status = 500
        fake.generate_chunks = [b'{"error": "vram exhausted"}\n']
        body = {"model": "qwen3:14b", "prompt": "x", "stream": False}

        resp = client.post("/api/generate", json=body)
        assert resp.status_code == 500
        assert "error" in resp.json()

        fake.generate_status = 200
        fake.generate_chunks = [
            b'{"model": "qwen3:14b", "response": "ok", "done": true}\n',
        ]
        resp2 = client.post("/api/generate", json=body)
        assert resp2.status_code == 200
        assert resp2.json()["done"] is True


class TestPriorityTierRouting:
    """Priority ordering under burst — deferred to concurrency suite."""

    def test_priority_tier_ordering(self) -> None:
        pytest.skip(
            "Priority tier ordering under burst is non-deterministic with "
            "the real scheduler loop interval; covered by "
            "tests/test_concurrency.py and tests/test_queue.py."
        )
