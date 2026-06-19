"""InferenceTapCollector — stream-tapped tokens/sec, prefill/decode, ctx-util.

Spec Section 5.4 (inference-native cluster). The collector centralizes the
streaming-token extraction (previously ``proxy._extract_streaming_tokens``,
which captured only ``prompt_eval_count``/``eval_count``) and EXTENDS it to
parse the final ``done:true`` NDJSON chunk for ``eval_duration`` and
``prompt_eval_duration`` as well, converting Ollama nanoseconds to seconds.

Hard constraints under test:
  - O(1) tap: ``on_chunk`` parses one small object per chunk and never buffers;
    non-final / partial / unparseable chunks are ignored (return without state
    mutation of the done-fields).
  - decode_tps = eval_count / (eval_duration / 1e9);
    prefill_tps = prompt_eval_count / (prompt_eval_duration / 1e9);
    ctx_utilization = prompt_eval_count / injected_num_ctx.
  - Divide-by-zero guard: ``eval_duration == 0`` (cache hit) yields rate ``None``,
    never an exception.
  - Missing fields yield ``None`` (never a misleading 0).
  - Model-agnostic: whatever model / counts Ollama returns are used as-is.
"""

from __future__ import annotations

import pytest

from bastion.inference_tap import InferenceTapCollector

# A representative final chunk. Durations are in nanoseconds (Ollama convention).
# eval: 100 tokens in 2.0 s -> 50 tok/s decode.
# prompt: 40 tokens in 0.5 s -> 80 tok/s prefill.
_FINAL_CHUNK = (
    b'{"done":true,'
    b'"eval_count":100,"eval_duration":2000000000,'
    b'"prompt_eval_count":40,"prompt_eval_duration":500000000}\n'
)


def test_final_chunk_yields_correct_rates_and_ctx_util() -> None:
    tap = InferenceTapCollector(injected_num_ctx=160)
    tap.on_chunk(_FINAL_CHUNK)

    assert tap.decode_tps == pytest.approx(50.0)
    assert tap.prefill_tps == pytest.approx(80.0)
    # 40 / 160 = 0.25
    assert tap.ctx_utilization == pytest.approx(0.25)
    # Raw counts are retained for record_recent_request.
    assert tap.eval_count == 100
    assert tap.prompt_eval_count == 40


def test_eval_duration_zero_cache_hit_yields_none_not_zero_division() -> None:
    """A cache hit reports eval_duration == 0 — decode_tps must be None."""
    chunk = b'{"done":true,"eval_count":12,"eval_duration":0}\n'
    tap = InferenceTapCollector()
    tap.on_chunk(chunk)

    assert tap.decode_tps is None
    # The count itself is still captured (it is real); only the rate is undefined.
    assert tap.eval_count == 12


def test_prompt_eval_duration_zero_yields_none_prefill() -> None:
    chunk = (
        b'{"done":true,"prompt_eval_count":7,"prompt_eval_duration":0}\n'
    )
    tap = InferenceTapCollector()
    tap.on_chunk(chunk)

    assert tap.prefill_tps is None
    assert tap.prompt_eval_count == 7


def test_missing_duration_fields_yield_none_rates() -> None:
    """Only counts present (no durations) -> rates None, never a fabricated 0."""
    chunk = b'{"done":true,"eval_count":50,"prompt_eval_count":10}\n'
    tap = InferenceTapCollector(injected_num_ctx=100)
    tap.on_chunk(chunk)

    assert tap.decode_tps is None
    assert tap.prefill_tps is None
    # ctx_utilization only needs prompt_eval_count + injected_num_ctx.
    assert tap.ctx_utilization == pytest.approx(0.10)


def test_missing_count_fields_yield_none() -> None:
    chunk = b'{"done":true}\n'
    tap = InferenceTapCollector(injected_num_ctx=100)
    tap.on_chunk(chunk)

    assert tap.decode_tps is None
    assert tap.prefill_tps is None
    assert tap.ctx_utilization is None
    assert tap.eval_count is None
    assert tap.prompt_eval_count is None


def test_partial_and_nonfinal_chunks_are_ignored() -> None:
    """Streaming token chunks (done:false) and unparseable bytes do not mutate state."""
    tap = InferenceTapCollector(injected_num_ctx=160)
    # Non-final content chunk: ignored.
    tap.on_chunk(b'{"response":"He","done":false}\n')
    assert tap.eval_count is None
    assert tap.decode_tps is None

    # Partial / truncated JSON: swallowed, no exception, no state change.
    tap.on_chunk(b'{"done":true,"eval_count":1')
    assert tap.eval_count is None

    # Empty keep-alive chunk: ignored.
    tap.on_chunk(b"")
    assert tap.eval_count is None

    # Now the real final chunk arrives and is parsed normally.
    tap.on_chunk(_FINAL_CHUNK)
    assert tap.eval_count == 100
    assert tap.decode_tps == pytest.approx(50.0)


def test_ctx_utilization_none_when_num_ctx_absent() -> None:
    """No injected_num_ctx denominator -> ctx_utilization None (no misleading 0)."""
    tap = InferenceTapCollector(injected_num_ctx=None)
    tap.on_chunk(_FINAL_CHUNK)

    assert tap.ctx_utilization is None
    # Rates are unaffected by the missing ctx denominator.
    assert tap.decode_tps == pytest.approx(50.0)


def test_ctx_utilization_zero_num_ctx_yields_none() -> None:
    """A zero/garbage denominator must not divide-by-zero."""
    tap = InferenceTapCollector(injected_num_ctx=0)
    tap.on_chunk(_FINAL_CHUNK)

    assert tap.ctx_utilization is None


def test_ns_to_s_conversion_is_exact() -> None:
    """Explicitly verify the 1e9 nanosecond->second conversion."""
    # 3 tokens in 1_000_000_000 ns (== 1.0 s) -> 3.0 tok/s.
    chunk = b'{"done":true,"eval_count":3,"eval_duration":1000000000}\n'
    tap = InferenceTapCollector()
    tap.on_chunk(chunk)
    assert tap.decode_tps == pytest.approx(3.0)


def test_on_complete_response_parses_dict_for_nonstreaming() -> None:
    """Non-streaming path feeds a parsed JSON dict, same compute applies."""
    resp_json = {
        "done": True,
        "eval_count": 200,
        "eval_duration": 4000000000,  # 4.0 s -> 50 tok/s
        "prompt_eval_count": 64,
        "prompt_eval_duration": 1000000000,  # 1.0 s -> 64 tok/s
    }
    tap = InferenceTapCollector(injected_num_ctx=128)
    tap.on_complete_response(resp_json)

    assert tap.decode_tps == pytest.approx(50.0)
    assert tap.prefill_tps == pytest.approx(64.0)
    assert tap.ctx_utilization == pytest.approx(0.5)
