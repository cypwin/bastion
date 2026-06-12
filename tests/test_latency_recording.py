"""Recording semantics feeding /broker/recent and /broker/latency.

Covers the S130 review fixes: samples are recorded at TRUE completion
(streaming records after the last byte, in the generator's finally) and
carry the real outcome status instead of a hardcoded 200, so the latency
endpoint's duration percentiles and error_rate reflect reality.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion.models import BrokerConfig, ModelInfo
from bastion.proxy import OllamaProxy


def _make_request(
    path: str = "/api/generate",
    body: dict | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock FastAPI Request (mirrors test_complexity_routing)."""
    if body is None:
        body = {"model": "qwen3:14b", "prompt": "hello"}
    req = MagicMock()
    req.url.path = path
    req.method = "POST"
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.headers = headers or {"user-agent": "test-client/1.0"}
    return req


def _make_config() -> BrokerConfig:
    return BrokerConfig(models={"qwen3:14b": ModelInfo(vram_gb=9.8)})


async def _grant_immediately(req):
    event = asyncio.Event()
    event.set()
    return event, lambda: None, lambda: None


class _FakeStream:
    """Async context manager mimicking httpx.AsyncClient.stream()."""

    def __init__(self, status_code: int = 200, chunks: list[bytes] | None = None,
                 raise_on_enter: Exception | None = None):
        self.status_code = status_code
        self._chunks = chunks if chunks is not None else [b'{"done": true}\n']
        self._raise = raise_on_enter

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


def _capture_record(records: list[dict]):
    def record(**kwargs):
        records.append(kwargs)
    return record


class TestNonStreamingRecording:
    @pytest.mark.asyncio
    async def test_records_real_status_code(self):
        """A 502 from _forward_response must be recorded as 502, not 200."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._forward_response = AsyncMock(
            return_value=MagicMock(status_code=502)
        )

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x",
                                  "stream": False})
        await proxy._handle_scheduled(req, "/api/generate", await req.body())

        [rec] = records
        assert rec["status_code"] == 502
        assert rec["streaming"] is False

    @pytest.mark.asyncio
    async def test_records_success_status(self):
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._forward_response = AsyncMock(
            return_value=MagicMock(status_code=200)
        )

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x",
                                  "stream": False})
        await proxy._handle_scheduled(req, "/api/generate", await req.body())

        [rec] = records
        assert rec["status_code"] == 200
        assert rec["model"] == "qwen3:14b"


class TestStreamingRecording:
    @pytest.mark.asyncio
    async def test_records_after_last_byte_not_at_dispatch(self):
        """The sample must appear only once the stream is fully consumed —
        recording at response construction is the bug this guards against."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            status_code=200, chunks=[b'{"a":1}\n', b'{"done":true}\n'],
        ))

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x",
                                  "stream": True})
        result = await proxy._handle_scheduled(
            req, "/api/generate", await req.body()
        )

        assert records == []  # nothing recorded before consumption
        async for _chunk in result.body_iterator:
            pass

        [rec] = records
        assert rec["streaming"] is True
        assert rec["status_code"] == 200

    @pytest.mark.asyncio
    async def test_upstream_failure_records_502(self):
        """A connect failure mid-stream records 502 — error_rate must be
        able to rise above zero during an Ollama outage."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            raise_on_enter=ConnectionError("refused"),
        ))

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x",
                                  "stream": True})
        result = await proxy._handle_scheduled(
            req, "/api/generate", await req.body()
        )
        with contextlib.suppress(Exception):
            async for _chunk in result.body_iterator:
                pass

        [rec] = records
        assert rec["status_code"] == 502
        assert rec["streaming"] is True

    @pytest.mark.asyncio
    async def test_upstream_error_status_propagates_to_sample(self):
        """An upstream 404 (unknown model) is recorded as 404."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            status_code=404, chunks=[b'{"error":"model not found"}\n'],
        ))

        req = _make_request(body={"model": "qwen3:14b", "prompt": "x",
                                  "stream": True})
        result = await proxy._handle_scheduled(
            req, "/api/generate", await req.body()
        )
        async for _chunk in result.body_iterator:
            pass

        [rec] = records
        assert rec["status_code"] == 404
