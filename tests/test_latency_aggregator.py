"""Unit tests for :mod:`bastion.latency_aggregator`.

Covers the pure aggregation function used by ``GET /broker/latency``.
No FastAPI / no httpx — direct function calls with synthetic samples.
"""

from __future__ import annotations

import math

import pytest

from bastion.latency_aggregator import OVERALL_KEY, _pct, aggregate_latency


def _sample(
    *,
    timestamp: float,
    model: str = "qwen3:30b",
    duration_s: float = 1.0,
    queue_wait_s: float = 0.0,
    status_code: int = 200,
) -> dict:
    """Build a single sample dict matching ``record_recent_request``'s shape."""
    return {
        "timestamp": timestamp,
        "model": model,
        "endpoint": "/api/generate",
        "tier": "agent",
        "queue_wait_s": queue_wait_s,
        "duration_s": duration_s,
        "status_code": status_code,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Empty / out-of-window inputs
# ─────────────────────────────────────────────────────────────────────────────


def test_empty_samples_returns_zeros() -> None:
    out = aggregate_latency([], window_s=300.0, now=1000.0)
    assert out.sample_total == 0
    assert out.per_model == []
    assert out.overall is None
    assert out.window_s == 0.0
    assert out.requested_window_s == 300.0


def test_all_samples_older_than_window_returns_zeros() -> None:
    samples = [_sample(timestamp=100.0), _sample(timestamp=200.0)]
    out = aggregate_latency(samples, window_s=10.0, now=1000.0)
    assert out.sample_total == 0
    assert out.per_model == []
    assert out.overall is None
    assert out.window_s == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Window edge / actual-vs-requested
# ─────────────────────────────────────────────────────────────────────────────


def test_window_s_reflects_oldest_sample_not_requested() -> None:
    # Oldest in-window sample is 50s old; requested window is 300s.
    now = 1000.0
    samples = [
        _sample(timestamp=now - 50.0, duration_s=1.0),
        _sample(timestamp=now - 10.0, duration_s=1.0),
        _sample(timestamp=now - 5.0, duration_s=1.0),
    ]
    out = aggregate_latency(samples, window_s=300.0, now=now)
    assert out.window_s == pytest.approx(50.0, abs=1e-6)
    assert out.requested_window_s == 300.0


def test_sample_at_cutoff_boundary_is_included() -> None:
    # Boundary semantics: timestamp >= cutoff is in-window.
    now = 1000.0
    cutoff_sample = _sample(timestamp=now - 100.0)  # exactly at cutoff
    out = aggregate_latency([cutoff_sample] * 3, window_s=100.0, now=now)
    assert out.sample_total == 3


# ─────────────────────────────────────────────────────────────────────────────
# Per-model bucketing + min_samples_per_model floor
# ─────────────────────────────────────────────────────────────────────────────


def test_mixed_model_window_buckets_correctly() -> None:
    now = 1000.0
    samples = [
        _sample(timestamp=now - i, model="qwen3:30b", duration_s=1.0 + i * 0.1)
        for i in range(5)
    ] + [
        _sample(timestamp=now - i, model="llama3:8b", duration_s=0.5 + i * 0.05)
        for i in range(5)
    ]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    models_in_response = {b.model for b in out.per_model}
    assert models_in_response == {"qwen3:30b", "llama3:8b"}
    assert out.sample_total == 10
    # per_model is sorted alphabetically by model name.
    assert [b.model for b in out.per_model] == ["llama3:8b", "qwen3:30b"]


def test_min_samples_floor_omits_low_count_models() -> None:
    now = 1000.0
    # Model A has 2 samples (below default floor of 3); Model B has 3.
    samples = [
        _sample(timestamp=now - 1.0, model="lonely:7b"),
        _sample(timestamp=now - 2.0, model="lonely:7b"),
        _sample(timestamp=now - 1.0, model="popular:13b"),
        _sample(timestamp=now - 2.0, model="popular:13b"),
        _sample(timestamp=now - 3.0, model="popular:13b"),
    ]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    models = {b.model for b in out.per_model}
    assert "popular:13b" in models
    assert "lonely:7b" not in models
    # Overall bucket aggregates ALL samples, regardless of the floor.
    assert out.overall is not None
    assert out.overall.sample_count == 5


def test_min_samples_floor_configurable() -> None:
    now = 1000.0
    samples = [_sample(timestamp=now - i, model="a:1") for i in range(2)]
    # With floor=2, the 2-sample model IS included.
    out = aggregate_latency(samples, window_s=60.0, now=now, min_samples_per_model=2)
    assert any(b.model == "a:1" for b in out.per_model)


# ─────────────────────────────────────────────────────────────────────────────
# Percentile correctness
# ─────────────────────────────────────────────────────────────────────────────


def test_percentile_known_values_p50_p95_p99() -> None:
    # 100 samples with duration_s 1..100 → p50≈50.5, p95≈95.05, p99≈99.01.
    now = 1000.0
    samples = [
        _sample(timestamp=now - 1.0, model="m", duration_s=float(i))
        for i in range(1, 101)
    ]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    bucket = next(b for b in out.per_model if b.model == "m")
    assert bucket.p50_s == pytest.approx(50.5, abs=0.01)
    assert bucket.p95_s == pytest.approx(95.05, abs=0.01)
    assert bucket.p99_s == pytest.approx(99.01, abs=0.01)


def test_single_sample_percentile_returns_that_value() -> None:
    # Single sample → all percentiles equal the value. Won't appear in
    # per_model under default floor=3 but overall bucket exposes it.
    now = 1000.0
    out = aggregate_latency(
        [_sample(timestamp=now - 1.0, duration_s=42.0)],
        window_s=60.0,
        now=now,
    )
    assert out.overall is not None
    assert out.overall.p50_s == 42.0
    assert out.overall.p95_s == 42.0
    assert out.overall.p99_s == 42.0


def test_pct_helper_edge_cases() -> None:
    assert _pct([], 0.5) is None
    assert _pct([7.0], 0.99) == 7.0
    assert _pct([1.0, 2.0], 0.0) == 1.0
    assert _pct([1.0, 2.0], 1.0) == 2.0
    # Midpoint interpolation on a known 2-element list.
    assert _pct([0.0, 10.0], 0.5) == pytest.approx(5.0)


# ─────────────────────────────────────────────────────────────────────────────
# Error counting
# ─────────────────────────────────────────────────────────────────────────────


def test_error_count_and_rate_counts_status_400_and_up() -> None:
    now = 1000.0
    samples = [
        _sample(timestamp=now - 1.0, status_code=200),
        _sample(timestamp=now - 2.0, status_code=201),
        _sample(timestamp=now - 3.0, status_code=400),  # error
        _sample(timestamp=now - 4.0, status_code=500),  # error
        _sample(timestamp=now - 5.0, status_code=503),  # error
    ]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.overall is not None
    assert out.overall.error_count == 3
    assert out.overall.error_rate == pytest.approx(3 / 5)


def test_error_rate_zero_when_no_errors() -> None:
    now = 1000.0
    samples = [_sample(timestamp=now - i, status_code=200) for i in range(5)]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.overall is not None
    assert out.overall.error_count == 0
    assert out.overall.error_rate == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Queue-wait percentiles + overall bucket identity
# ─────────────────────────────────────────────────────────────────────────────


def test_queue_wait_percentiles_use_queue_wait_s_not_duration() -> None:
    now = 1000.0
    # duration_s constant; queue_wait_s varies — make sure we don't mix them up.
    samples = [
        _sample(timestamp=now - i, duration_s=1.0, queue_wait_s=float(i))
        for i in range(1, 6)
    ]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.overall is not None
    # 5 samples: queue_wait_s ∈ {1,2,3,4,5} → p50=3, p95≈4.8
    assert out.overall.queue_wait_p50_s == pytest.approx(3.0)
    assert out.overall.queue_wait_p95_s == pytest.approx(4.8, abs=0.01)
    # duration_s is constant 1.0
    assert out.overall.p50_s == pytest.approx(1.0)


def test_overall_bucket_uses_sentinel_model_name() -> None:
    now = 1000.0
    samples = [_sample(timestamp=now - i, model="m") for i in range(3)]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.overall is not None
    assert out.overall.model == OVERALL_KEY == "__overall__"


def test_overall_sample_count_matches_total() -> None:
    now = 1000.0
    samples = [
        _sample(timestamp=now - i, model="a", duration_s=0.5) for i in range(4)
    ] + [
        _sample(timestamp=now - i, model="b", duration_s=2.0) for i in range(4)
    ]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.sample_total == 8
    assert out.overall is not None
    assert out.overall.sample_count == 8


# ─────────────────────────────────────────────────────────────────────────────
# Determinism / no NaN
# ─────────────────────────────────────────────────────────────────────────────


def test_no_nan_in_percentiles_for_valid_input() -> None:
    now = 1000.0
    samples = [_sample(timestamp=now - i, duration_s=float(i)) for i in range(1, 11)]
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.overall is not None
    for field in (out.overall.p50_s, out.overall.p95_s, out.overall.p99_s):
        assert field is not None
        assert not math.isnan(field)


def test_future_timestamp_clamps_window_to_zero_instead_of_failing() -> None:
    """Backwards wall-clock step: a sample stamped ahead of `now` must not
    produce a negative window_s (which would fail model validation and 500
    the endpoint)."""
    now = 1000.0
    samples = [_sample(timestamp=now + 5.0)]  # stamped in the "future"
    out = aggregate_latency(samples, window_s=60.0, now=now)
    assert out.window_s == 0.0
    assert out.sample_total == 1
