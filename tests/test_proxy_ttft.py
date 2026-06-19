"""TTFT stream tap on the inference streaming path (spec Section 5.4).

The proxy's `_stream_response.generate()` (the inference NDJSON path, NOT the
raw passthrough) observes time-to-first-token exactly once per streaming
request: `time.time() - dispatch_start` measured at the first non-empty chunk,
reported via `metrics.observe_llm_ttft(model, ttft)`.

Hard constraints under test:
  - O(1) tap: every chunk is still yielded immediately and in order; nothing is
    withheld or buffered.
  - Called exactly once per streaming request, with a plausible (>= 0) value and
    the model Ollama was asked for (model-agnostic).
  - NOT called on the non-streaming path.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion import proxy as proxy_mod
from bastion.models import BrokerConfig, ModelInfo
from bastion.proxy import OllamaProxy


def _make_request(
    path: str = "/api/generate",
    body: dict | None = None,
    headers: dict | None = None,
) -> MagicMock:
    if body is None:
        body = {"model": "qwen3:14b", "prompt": "hello"}
    req = MagicMock()
    req.url.path = path
    req.method = "POST"
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = headers if headers is not None else {"user-agent": "test-client/1.0"}
    return req


def _make_config() -> BrokerConfig:
    return BrokerConfig(models={"qwen3:14b": ModelInfo(vram_gb=9.8)})


async def _grant_immediately(req):
    event = asyncio.Event()
    event.set()
    return event, lambda: None, lambda: None


class _FakeStream:
    """Async context manager mimicking httpx.AsyncClient.stream()."""

    def __init__(self, status_code: int = 200, chunks: list[bytes] | None = None):
        self.status_code = status_code
        self._chunks = chunks if chunks is not None else [b'{"done": true}\n']

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class TestProxyTTFT:
    @pytest.mark.asyncio
    async def test_ttft_observed_once_with_plausible_value(self, monkeypatch):
        """A streaming request observes TTFT exactly once, model-agnostic."""
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(
            proxy_mod.metrics,
            "observe_llm_ttft",
            lambda model, ttft_seconds: calls.append((model, ttft_seconds)),
        )

        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        chunks = [b'{"response":"He"}\n', b'{"response":"llo"}\n', b'{"done":true}\n']
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(
            return_value=_FakeStream(status_code=200, chunks=chunks)
        )

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x", "stream": True})
        result = await proxy._handle_scheduled(req, "/api/generate", await req.body())

        # Not observed until the stream is actually consumed.
        assert calls == []
        async for _chunk in result.body_iterator:
            pass

        assert len(calls) == 1
        model, ttft = calls[0]
        assert model == "qwen3:14b"
        assert isinstance(ttft, float)
        assert ttft >= 0.0

    @pytest.mark.asyncio
    async def test_all_chunks_yielded_in_order_none_withheld(self, monkeypatch):
        """The tap must not buffer: every chunk arrives, in order, unmodified."""
        monkeypatch.setattr(
            proxy_mod.metrics, "observe_llm_ttft", lambda model, ttft_seconds: None
        )

        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        sent = [b'{"response":"a"}\n', b'{"response":"b"}\n',
                b'{"response":"c"}\n', b'{"done":true}\n']
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(
            return_value=_FakeStream(status_code=200, chunks=list(sent))
        )

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x", "stream": True})
        result = await proxy._handle_scheduled(req, "/api/generate", await req.body())

        received: list[bytes] = []
        async for chunk in result.body_iterator:
            received.append(chunk)

        assert received == sent

    @pytest.mark.asyncio
    async def test_ttft_measures_to_first_non_empty_chunk(self, monkeypatch):
        """An empty leading chunk (keep-alive) does not count as first token."""
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(
            proxy_mod.metrics,
            "observe_llm_ttft",
            lambda model, ttft_seconds: calls.append((model, ttft_seconds)),
        )

        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        chunks = [b"", b'{"response":"first"}\n', b'{"done":true}\n']
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(
            return_value=_FakeStream(status_code=200, chunks=list(chunks))
        )

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x", "stream": True})
        result = await proxy._handle_scheduled(req, "/api/generate", await req.body())

        received: list[bytes] = []
        async for chunk in result.body_iterator:
            received.append(chunk)

        # Empty chunk still passes through untouched.
        assert received == chunks
        # Observed exactly once.
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_not_observed_on_non_streaming(self, monkeypatch):
        """The non-streaming path carries no token stream — no TTFT observation."""
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(
            proxy_mod.metrics,
            "observe_llm_ttft",
            lambda model, ttft_seconds: calls.append((model, ttft_seconds)),
        )

        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._forward_response = AsyncMock(return_value=MagicMock(status_code=200))

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x", "stream": False})
        await proxy._handle_scheduled(req, "/api/generate", await req.body())

        assert calls == []

    @pytest.mark.asyncio
    async def test_no_ttft_when_stream_empty(self, monkeypatch):
        """A stream that yields no non-empty chunk observes nothing (no spurious 0)."""
        calls: list[tuple[str, float]] = []
        monkeypatch.setattr(
            proxy_mod.metrics,
            "observe_llm_ttft",
            lambda model, ttft_seconds: calls.append((model, ttft_seconds)),
        )

        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(
            return_value=_FakeStream(status_code=200, chunks=[b"", b""])
        )

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x", "stream": True})
        result = await proxy._handle_scheduled(req, "/api/generate", await req.body())
        async for _chunk in result.body_iterator:
            pass

        assert calls == []
