"""Pure-function latency aggregation for ``GET /broker/latency``.

Factored out of the FastAPI handler so it's unit-testable without
spinning up the app. Operates on the same sample dicts that
``server.record_recent_request`` writes into ``_recent_requests``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from bastion.models import BrokerLatency, LatencyBucket

OVERALL_KEY = "__overall__"


def aggregate_latency(
    samples: Iterable[dict],
    window_s: float,
    min_samples_per_model: int = 3,
    now: float | None = None,
) -> BrokerLatency:
    """Aggregate ``_recent_requests`` entries into a :class:`BrokerLatency`.

    Parameters
    ----------
    samples
        Iterable of recent-request dicts. Each must contain keys
        ``timestamp``, ``model``, ``duration_s``, ``queue_wait_s``,
        and ``status_code``.
    window_s
        Seconds back from ``now`` to include. Samples older than this
        are dropped. Must be non-negative.
    min_samples_per_model
        Models with fewer than this many samples in the window are
        omitted from ``per_model``. Prevents single-call noise from
        dominating p95. The ``overall`` bucket is unaffected by this
        floor.
    now
        Reference timestamp; defaults to ``time.time()``. Injectable
        for tests.

    Returns
    -------
    BrokerLatency
        ``window_s`` on the response reflects the actual age of the
        oldest considered sample (not the requested window), so
        consumers can detect a young broker.
    """
    if now is None:
        now = time.time()
    cutoff = now - window_s
    in_window = [s for s in samples if s["timestamp"] >= cutoff]

    if not in_window:
        return BrokerLatency(
            window_s=0.0,
            requested_window_s=window_s,
            sample_total=0,
            per_model=[],
            overall=None,
        )

    # Clamp at 0: a sample stamped ahead of `now` (backwards wall-clock
    # step, NTP correction) must not produce a negative window and fail
    # BrokerLatency validation mid-request.
    actual_window_s = max(0.0, now - min(s["timestamp"] for s in in_window))

    by_model: dict[str, list[dict]] = {}
    for s in in_window:
        by_model.setdefault(s["model"], []).append(s)

    per_model_buckets = [
        _bucket(model, model_samples)
        for model, model_samples in sorted(by_model.items())
        if len(model_samples) >= min_samples_per_model
    ]

    overall = _bucket(OVERALL_KEY, in_window)

    return BrokerLatency(
        window_s=actual_window_s,
        requested_window_s=window_s,
        sample_total=len(in_window),
        per_model=per_model_buckets,
        overall=overall,
    )


def _bucket(model: str, samples: list[dict]) -> LatencyBucket:
    """Compute one :class:`LatencyBucket` from a list of samples."""
    durations = sorted(s["duration_s"] for s in samples)
    waits = sorted(s["queue_wait_s"] for s in samples)
    errors = sum(1 for s in samples if s["status_code"] >= 400)
    n = len(samples)

    return LatencyBucket(
        model=model,
        sample_count=n,
        p50_s=_pct(durations, 0.50),
        p95_s=_pct(durations, 0.95),
        p99_s=_pct(durations, 0.99),
        queue_wait_p50_s=_pct(waits, 0.50),
        queue_wait_p95_s=_pct(waits, 0.95),
        error_count=errors,
        error_rate=errors / n if n > 0 else 0.0,
    )


def _pct(sorted_values: list[float], p: float) -> float | None:
    """Linear-interpolation percentile over a pre-sorted list.

    Returns ``None`` when ``sorted_values`` is empty. For a single-element
    list, returns that element. Matches numpy.percentile's ``linear``
    method on contiguous data without taking a numpy dependency.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)
