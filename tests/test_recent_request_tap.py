"""T2-recent-request: thread inference-tap metrics through record_recent_request.

Spec Section 4.6 + 5.4. ``record_recent_request`` (server.py) gains six new
``None``-default keyword parameters appended after ``source``:
``prefill_tps``, ``decode_tps``, ``ttft_s``, ``ctx_utilization``,
``eval_count``, ``prompt_eval_count``. They are stored in the ``_recent_requests``
dict alongside the existing keys and surfaced verbatim by ``GET /broker/recent``.

Three contracts under test:
  1. BACK-COMPAT regression: every existing caller that omits the new kwargs
     keeps working unchanged, and the new keys default to ``None`` in the dict.
  2. The new kwargs, when supplied, surface verbatim in ``_recent_requests``
     (the body of ``/broker/recent``).
  3. WIRING: the proxy's inference streaming path feeds an
     ``InferenceTapCollector`` and supplies the six derived signals to
     ``record_recent_request`` when the request completes — with ns->s
     conversion, the cache-hit (``eval_duration==0``) ``decode_tps=None`` guard,
     the ``default_num_ctx`` ctx-utilization denominator fallback, and **without
     buffering** the NDJSON stream.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bastion.models import BrokerConfig, ModelInfo
from bastion.proxy import OllamaProxy

# --------------------------------------------------------------------------- #
# Unit: record_recent_request signature + dict shape (server.py)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fresh_recent(monkeypatch):
    """Isolate the module-level ``_recent_requests`` deque per test."""
    from collections import deque

    from bastion import server

    d: deque = deque(maxlen=500)
    monkeypatch.setattr(server, "_recent_requests", d)
    return d


class TestRecordRecentRequestSignature:
    def test_old_signature_still_works(self, fresh_recent):
        """Regression: a caller using the OLD positional+source signature
        (no new kwargs) must succeed, and the six new keys default to None."""
        from bastion.server import record_recent_request

        record_recent_request(
            "qwen3:14b", "/api/generate", "agent",
            0.0, 1.23, 200, True, "swarm-7",
        )

        [rec] = list(fresh_recent)
        assert rec["model"] == "qwen3:14b"
        assert rec["status_code"] == 200
        assert rec["source"] == "swarm-7"
        # New keys present and None by default — never a misleading 0.
        for key in (
            "prefill_tps", "decode_tps", "ttft_s",
            "ctx_utilization", "eval_count", "prompt_eval_count",
        ):
            assert key in rec, f"missing new key {key!r}"
            assert rec[key] is None

    def test_kwargs_only_minimal_call_still_works(self, fresh_recent):
        """A keyword-style caller omitting all the new kwargs also works."""
        from bastion.server import record_recent_request

        record_recent_request(
            model="m", endpoint="/api/chat", tier="user",
            queue_wait_s=0.0, duration_s=0.5, status_code=200,
        )
        [rec] = list(fresh_recent)
        assert rec["decode_tps"] is None
        assert rec["eval_count"] is None

    def test_new_kwargs_surface_in_recent(self, fresh_recent):
        """Supplying the six new kwargs surfaces them verbatim in the dict
        that backs GET /broker/recent."""
        from bastion.server import record_recent_request

        record_recent_request(
            "qwen3:14b", "/api/generate", "agent",
            0.0, 2.0, 200, True, "swarm-7",
            prefill_tps=120.5,
            decode_tps=48.25,
            ttft_s=0.31,
            ctx_utilization=0.42,
            eval_count=256,
            prompt_eval_count=1024,
        )

        [rec] = list(fresh_recent)
        assert rec["prefill_tps"] == 120.5
        assert rec["decode_tps"] == 48.25
        assert rec["ttft_s"] == 0.31
        assert rec["ctx_utilization"] == 0.42
        assert rec["eval_count"] == 256
        assert rec["prompt_eval_count"] == 1024


# --------------------------------------------------------------------------- #
# Integration: proxy stream tap -> record_recent_request kwargs
# --------------------------------------------------------------------------- #


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


def _make_config(default_num_ctx: int | None = None) -> BrokerConfig:
    cfg = BrokerConfig(models={"qwen3:14b": ModelInfo(vram_gb=9.8)})
    if default_num_ctx is not None:
        cfg.request_overrides.default_num_ctx = default_num_ctx
    return cfg


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


def _capture_record(records: list[dict]):
    def record(**kwargs):
        records.append(kwargs)
    return record


# A realistic Ollama done:true chunk (durations in NANOSECONDS).
# eval_count / (eval_duration / 1e9) = 256 / 2.0 = 128.0 tok/s
# prompt_eval_count / (prompt_eval_duration / 1e9) = 1024 / 0.5 = 2048.0 tok/s
_DONE_CHUNK = json.dumps({
    "done": True,
    "eval_count": 256,
    "eval_duration": 2_000_000_000,
    "prompt_eval_count": 1024,
    "prompt_eval_duration": 500_000_000,
}).encode() + b"\n"


class TestProxyTapWiring:
    @pytest.mark.asyncio
    async def test_streaming_tap_supplies_tokens_to_record(self):
        """A streaming inference request feeds the tap and surfaces decode_tps,
        prefill_tps, eval_count and prompt_eval_count to record_recent_request,
        with the ns->s conversion applied."""
        records: list[dict] = []
        # num_ctx in options gives ctx_utilization = 1024 / 4096 = 0.25.
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            status_code=200, chunks=[b'{"response":"hi"}\n', _DONE_CHUNK],
        ))

        body = {"model": "qwen3:14b", "prompt": "x", "stream": True,
                "options": {"num_ctx": 4096}}
        result = await proxy._handle_scheduled(
            _make_request(body=body), "/api/generate",
            json.dumps(body).encode(),
        )
        async for _chunk in result.body_iterator:
            pass

        [rec] = records
        assert rec["streaming"] is True
        assert rec["status_code"] == 200
        assert rec["decode_tps"] == pytest.approx(128.0)
        assert rec["prefill_tps"] == pytest.approx(2048.0)
        assert rec["eval_count"] == 256
        assert rec["prompt_eval_count"] == 1024
        assert rec["ctx_utilization"] == pytest.approx(0.25)
        assert rec["ttft_s"] is not None and rec["ttft_s"] >= 0.0

    @pytest.mark.asyncio
    async def test_cache_hit_zero_eval_duration_yields_none_decode_tps(self):
        """eval_duration==0 (cache hit) must yield decode_tps=None — never a
        divide-by-zero, never a misleading 0."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        cache_chunk = json.dumps({
            "done": True,
            "eval_count": 256,
            "eval_duration": 0,
            "prompt_eval_count": 1024,
            "prompt_eval_duration": 500_000_000,
        }).encode() + b"\n"
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            status_code=200, chunks=[cache_chunk],
        ))

        body = {"model": "qwen3:14b", "prompt": "x", "stream": True,
                "options": {"num_ctx": 4096}}
        result = await proxy._handle_scheduled(
            _make_request(body=body), "/api/generate",
            json.dumps(body).encode(),
        )
        async for _chunk in result.body_iterator:
            pass

        [rec] = records
        assert rec["decode_tps"] is None
        # Prefill still computed (its duration is non-zero).
        assert rec["prefill_tps"] == pytest.approx(2048.0)
        assert rec["eval_count"] == 256

    @pytest.mark.asyncio
    async def test_ctx_utilization_default_num_ctx_fallback(self):
        """With no per-request num_ctx and no per-model broker.yaml entry, the
        ctx_utilization denominator falls back to the global
        request_overrides.default_num_ctx (spec 5.4 precedence step 3), so the
        signal fires even for users without per-model config entries."""
        records: list[dict] = []
        # default_num_ctx=2048 -> ctx_utilization = 1024 / 2048 = 0.5. The
        # request targets a model with NO config entry, so the per-model
        # default cannot apply and the global override is used.
        proxy = OllamaProxy(_make_config(default_num_ctx=2048),
                            enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            status_code=200, chunks=[_DONE_CHUNK],
        ))

        # Unconfigured model + no "options" -> injection adds num_ctx=2048 from
        # the global request_overrides.default_num_ctx fallback.
        body = {"model": "unconfigured-model:7b", "prompt": "x", "stream": True}
        result = await proxy._handle_scheduled(
            _make_request(body=body), "/api/generate",
            json.dumps(body).encode(),
        )
        async for _chunk in result.body_iterator:
            pass

        [rec] = records
        assert rec["ctx_utilization"] == pytest.approx(0.5)
        assert rec["model"] == "unconfigured-model:7b"

    @pytest.mark.asyncio
    async def test_tap_does_not_buffer_chunks(self):
        """No buffering regression: every chunk is yielded, in order, unmodified
        — even with the tap feeding the collector per chunk."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)
        sent = [b'{"response":"a"}\n', b'{"response":"b"}\n', _DONE_CHUNK]
        proxy._http = MagicMock()
        proxy._http.stream = MagicMock(return_value=_FakeStream(
            status_code=200, chunks=list(sent),
        ))

        body = {"model": "qwen3:14b", "prompt": "x", "stream": True,
                "options": {"num_ctx": 4096}}
        result = await proxy._handle_scheduled(
            _make_request(body=body), "/api/generate",
            json.dumps(body).encode(),
        )
        received: list[bytes] = []
        async for chunk in result.body_iterator:
            received.append(chunk)

        assert received == sent

    @pytest.mark.asyncio
    async def test_non_streaming_records_token_counts(self):
        """The non-streaming path feeds the tap from the full response JSON and
        still surfaces token counts (decode_tps/prefill_tps) to record."""
        records: list[dict] = []
        proxy = OllamaProxy(_make_config(), enqueue_fn=_grant_immediately)
        proxy._record_fn = _capture_record(records)

        resp_json = {
            "done": True,
            "eval_count": 256, "eval_duration": 2_000_000_000,
            "prompt_eval_count": 1024, "prompt_eval_duration": 500_000_000,
        }
        fake_resp = MagicMock(status_code=200)
        fake_resp.json = MagicMock(return_value=resp_json)
        fake_resp.text = json.dumps(resp_json)
        proxy._http = MagicMock()
        proxy._http.post = AsyncMock(return_value=fake_resp)

        body = {"model": "qwen3:14b", "prompt": "x", "stream": False,
                "options": {"num_ctx": 4096}}
        await proxy._handle_scheduled(
            _make_request(body=body), "/api/generate",
            json.dumps(body).encode(),
        )

        [rec] = records
        assert rec["streaming"] is False
        assert rec["decode_tps"] == pytest.approx(128.0)
        assert rec["prefill_tps"] == pytest.approx(2048.0)
        assert rec["eval_count"] == 256
        assert rec["ctx_utilization"] == pytest.approx(0.25)
