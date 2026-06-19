"""Inference-native stream tap (spec Section 5.4).

A single :class:`InferenceTapCollector` instance is captured per request by the
proxy's inference streaming generator (``_stream_response.generate()``). It is an
**O(1) tap**: ``on_chunk`` parses one small JSON object per NDJSON chunk and
returns immediately — it never buffers the stream, and the proxy keeps yielding
each chunk before and after the parse. Only the final ``done:true`` chunk carries
Ollama's token accounting; every other chunk is ignored.

This module centralizes the token extraction that previously lived inline in
``proxy._extract_streaming_tokens`` (which captured only ``prompt_eval_count``
and ``eval_count``) and EXTENDS it to also capture ``eval_duration`` and
``prompt_eval_duration`` so tokens/sec can be computed. Ollama reports durations
in **nanoseconds**, so they are divided by ``1e9`` to obtain seconds.

Derived signals (all model-agnostic — whatever model and counts Ollama returns
for any model the user runs are used as-is):

* ``decode_tps``      = ``eval_count / (eval_duration / 1e9)``
* ``prefill_tps``     = ``prompt_eval_count / (prompt_eval_duration / 1e9)``
* ``ctx_utilization`` = ``prompt_eval_count / injected_num_ctx``

Graceful degradation (mandatory): a cache hit reports ``eval_duration == 0`` —
the corresponding rate is ``None``, never a divide-by-zero. A missing count or
duration field yields ``None`` for the affected signal, never a misleading ``0``.

The module imports only :mod:`bastion.metrics` and the standard library so it can
be imported by ``proxy.py`` without a circular dependency and carries no heavy
dependencies.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bastion import metrics

# Ollama reports prompt_eval_duration / eval_duration in nanoseconds.
_NS_PER_S = 1_000_000_000.0


def _rate(count: int | None, duration_ns: int | None) -> float | None:
    """tokens / seconds, with the cache-hit (``duration == 0``) guard.

    Returns ``None`` (never raises, never fabricates 0) when either operand is
    missing or the duration is zero/negative.
    """
    if count is None or duration_ns is None:
        return None
    if duration_ns <= 0:  # cache hit (0) or malformed (<0): rate is undefined.
        return None
    return count / (duration_ns / _NS_PER_S)


@dataclass
class InferenceTapCollector:
    """Per-request O(1) tap over the inference NDJSON stream (spec 5.4).

    Parameters
    ----------
    injected_num_ctx:
        The ``num_ctx`` actually sent to Ollama for this request (the context
        window denominator), captured at injection time in the proxy. ``None``
        when no value was set at any tier — ``ctx_utilization`` is then ``None``.
    """

    injected_num_ctx: int | None = None

    # Captured from the final ``done:true`` chunk. None until that chunk arrives.
    eval_count: int | None = None
    eval_duration_ns: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration_ns: int | None = None

    # TTFT support: monotonic-or-wall time of the first non-empty chunk. The
    # proxy already owns its own TTFT flag today; this field lets the collector
    # subsume it when the streaming path is migrated onto the collector.
    first_chunk_time: float | None = None

    # All fields parsed out of the final chunk, kept for callers that want the
    # raw accounting (e.g. token-count response headers).
    done_fields: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ ingest

    def on_chunk(self, chunk: bytes | str, now: float | None = None) -> None:
        """Tap a single streaming chunk. O(1); never buffers.

        Records the first-chunk time on the first non-empty chunk, then attempts
        to parse the chunk as the final ``done:true`` object. Non-final, empty,
        and unparseable chunks are ignored without mutating the captured fields.
        """
        if now is not None and self.first_chunk_time is None and chunk:
            self.first_chunk_time = now

        data = self._loads(chunk)
        if data is None or not data.get("done"):
            return
        self._capture_done(data)

    def on_complete_response(self, resp_json: dict[str, Any]) -> None:
        """Non-streaming path: feed the already-parsed full response JSON."""
        if isinstance(resp_json, dict):
            self._capture_done(resp_json)

    # --------------------------------------------------------------- accessors

    @property
    def eval_duration_s(self) -> float | None:
        """Decode duration in seconds (ns / 1e9), or ``None`` if absent."""
        if self.eval_duration_ns is None:
            return None
        return self.eval_duration_ns / _NS_PER_S

    @property
    def prompt_eval_duration_s(self) -> float | None:
        """Prefill duration in seconds (ns / 1e9), or ``None`` if absent."""
        if self.prompt_eval_duration_ns is None:
            return None
        return self.prompt_eval_duration_ns / _NS_PER_S

    @property
    def decode_tps(self) -> float | None:
        """Decode tokens/sec; ``None`` on cache hit or missing fields."""
        return _rate(self.eval_count, self.eval_duration_ns)

    @property
    def prefill_tps(self) -> float | None:
        """Prefill tokens/sec; ``None`` on cache hit or missing fields."""
        return _rate(self.prompt_eval_count, self.prompt_eval_duration_ns)

    @property
    def ctx_utilization(self) -> float | None:
        """``prompt_eval_count / injected_num_ctx``; ``None`` if either absent.

        A zero/negative denominator yields ``None`` (no divide-by-zero, no
        misleading ratio). Malformed ratios > 1.0 are *not* clamped here — the
        surface layer decides whether to drop them (spec 5.4).
        """
        if self.prompt_eval_count is None or self.injected_num_ctx is None:
            return None
        if self.injected_num_ctx <= 0:
            return None
        return self.prompt_eval_count / self.injected_num_ctx

    # ------------------------------------------------------------------- flush

    def flush(
        self,
        model: str,
        dispatch_start: float | None = None,
        record_fn: Callable[..., None] | None = None,
    ) -> None:
        """Emit the tapped signals once the stream is exhausted.

        Calls ``record_fn`` (``record_recent_request`` in the server) with the
        six inference kwargs from spec 4.6, and observes the Prometheus
        histograms when the corresponding helpers exist. Every emission is
        individually guarded — a missing metric helper or a ``record_fn`` that
        does not accept the kwargs must never break the streaming finally block.
        """
        decode = self.decode_tps
        prefill = self.prefill_tps
        ctx = self.ctx_utilization
        ttft = None
        if dispatch_start is not None and self.first_chunk_time is not None:
            ttft = max(0.0, self.first_chunk_time - dispatch_start)

        self._observe(model, decode, prefill, ctx)

        if record_fn is not None:
            # A record_fn predating the new kwargs (or a test double): the
            # tap is best-effort and must not break the request finally.
            with contextlib.suppress(TypeError):
                record_fn(
                    prefill_tps=prefill,
                    decode_tps=decode,
                    ttft_s=ttft,
                    ctx_utilization=ctx,
                    eval_count=self.eval_count,
                    prompt_eval_count=self.prompt_eval_count,
                )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _loads(chunk: bytes | str) -> dict[str, Any] | None:
        if not chunk:
            return None
        try:
            data = json.loads(chunk)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _capture_done(self, data: dict[str, Any]) -> None:
        """Store the four token-accounting fields from a final chunk.

        Each field is captured independently so a final chunk carrying only a
        subset (e.g. counts without durations) still records what it has.
        """
        self.done_fields = data
        ec = data.get("eval_count")
        ed = data.get("eval_duration")
        pec = data.get("prompt_eval_count")
        ped = data.get("prompt_eval_duration")
        if isinstance(ec, int):
            self.eval_count = ec
        if isinstance(ed, int):
            self.eval_duration_ns = ed
        if isinstance(pec, int):
            self.prompt_eval_count = pec
        if isinstance(ped, int):
            self.prompt_eval_duration_ns = ped

    @staticmethod
    def _observe(
        model: str,
        decode_tps: float | None,
        prefill_tps: float | None,
        ctx_utilization: float | None,
    ) -> None:
        """Observe the Prometheus histograms if their helpers are defined.

        The histogram objects/helpers are defined in a separate metrics slice;
        this tap calls them defensively via ``getattr`` so it neither hard-
        depends on them nor fabricates a series. ``model`` is the only label
        (bounded), per the cardinality rule — never per-request/per-pid labels.
        """
        if decode_tps is not None:
            _try_observe("observe_llm_decode_tps", model, decode_tps)
        if prefill_tps is not None:
            _try_observe("observe_llm_prefill_tps", model, prefill_tps)
        if ctx_utilization is not None:
            _try_observe("observe_llm_ctx_utilization", model, ctx_utilization)


def _try_observe(helper_name: str, model: str, value: float) -> None:
    fn = getattr(metrics, helper_name, None)
    if fn is None:
        return
    with contextlib.suppress(Exception):  # metrics must never break the stream finally block.
        fn(model, value)
