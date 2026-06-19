"""HTTP-contract tests for ``GET /broker/latency``.

Uses the ``app_with_stub_scheduler`` fixture from ``conftest.py`` so the
endpoint exercises its full path (auth + router + handler) without a
real Ollama. The aggregation logic itself is covered by
``test_latency_aggregator.py``; these tests pin the wire contract:
status codes, schema keys, query-param clamping, and seeded-data flow.
"""

from __future__ import annotations

import time
from collections import deque

import bastion.server as server_mod


def _sample(
    *,
    timestamp: float | None = None,
    model: str = "qwen3:14b",
    duration_s: float = 1.0,
    queue_wait_s: float = 0.05,
    status_code: int = 200,
) -> dict:
    return {
        "timestamp": timestamp if timestamp is not None else time.time(),
        "model": model,
        "endpoint": "/api/generate",
        "tier": "agent",
        "queue_wait_s": queue_wait_s,
        "duration_s": duration_s,
        "status_code": status_code,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Empty / default-window shape
# ─────────────────────────────────────────────────────────────────────────────


class TestLatencyEndpointEmpty:
    """No samples in the ring buffer → zero-shaped response, 200 OK."""

    def test_returns_200_on_empty_buffer(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        resp = client.get("/broker/latency")
        assert resp.status_code == 200

    def test_empty_buffer_yields_zero_sample_total(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        body = client.get("/broker/latency").json()
        assert body["sample_total"] == 0
        assert body["per_model"] == []
        assert body["overall"] is None

    def test_default_requested_window_is_300(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        body = client.get("/broker/latency").json()
        assert body["requested_window_s"] == 300.0

    def test_response_has_required_top_level_keys(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        body = client.get("/broker/latency").json()
        for key in (
            "window_s",
            "requested_window_s",
            "sample_total",
            "per_model",
            "overall",
        ):
            assert key in body, f"missing key: {key}"


# ─────────────────────────────────────────────────────────────────────────────
# Seeded data — per_model + overall shapes
# ─────────────────────────────────────────────────────────────────────────────


class TestLatencyEndpointWithData:
    """Seed the ring buffer; verify shape and percentile flow."""

    def test_per_model_bucket_present_for_active_model(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        now = time.time()
        server_mod._recent_requests.clear()
        for i in range(5):
            server_mod._recent_requests.appendleft(
                _sample(timestamp=now - i, duration_s=1.0 + i * 0.1)
            )
        body = client.get("/broker/latency?window_s=60").json()
        models = [b["model"] for b in body["per_model"]]
        assert "qwen3:14b" in models
        assert body["sample_total"] == 5

    def test_overall_bucket_aggregates_across_models(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        now = time.time()
        server_mod._recent_requests.clear()
        for i in range(3):
            server_mod._recent_requests.appendleft(
                _sample(timestamp=now - i, model="a:1", duration_s=1.0)
            )
            server_mod._recent_requests.appendleft(
                _sample(timestamp=now - i, model="b:1", duration_s=2.0)
            )
        body = client.get("/broker/latency?window_s=60").json()
        assert body["overall"] is not None
        assert body["overall"]["model"] == "__overall__"
        assert body["overall"]["sample_count"] == 6

    def test_min_samples_floor_omits_lonely_model_from_per_model(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        now = time.time()
        server_mod._recent_requests.clear()
        # 2 samples for "lonely" (below floor=3), 3 for "popular".
        for i in range(2):
            server_mod._recent_requests.appendleft(
                _sample(timestamp=now - i, model="lonely:7b")
            )
        for i in range(3):
            server_mod._recent_requests.appendleft(
                _sample(timestamp=now - i, model="popular:13b")
            )
        body = client.get("/broker/latency?window_s=60").json()
        per_model_names = {b["model"] for b in body["per_model"]}
        assert "popular:13b" in per_model_names
        assert "lonely:7b" not in per_model_names
        # Overall bucket counts ALL samples regardless of the floor.
        assert body["overall"]["sample_count"] == 5

    def test_error_count_propagates_to_overall(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        now = time.time()
        server_mod._recent_requests.clear()
        for sc in (200, 200, 500, 503, 200):
            server_mod._recent_requests.appendleft(
                _sample(timestamp=now, status_code=sc)
            )
        body = client.get("/broker/latency?window_s=60").json()
        assert body["overall"]["error_count"] == 2
        assert 0.39 < body["overall"]["error_rate"] < 0.41


# ─────────────────────────────────────────────────────────────────────────────
# Query-param clamping
# ─────────────────────────────────────────────────────────────────────────────


class TestLatencyEndpointClamping:
    """window_s is clamped to [10, 3600] — out-of-band values must not 422."""

    def test_window_below_floor_clamped_to_10(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        body = client.get("/broker/latency?window_s=1").json()
        assert body["requested_window_s"] == 10.0

    def test_window_above_ceiling_clamped_to_3600(
        self, app_with_stub_scheduler
    ) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        body = client.get("/broker/latency?window_s=99999").json()
        assert body["requested_window_s"] == 3600.0

    def test_window_zero_does_not_error(self, app_with_stub_scheduler) -> None:
        client = app_with_stub_scheduler
        server_mod._recent_requests.clear()
        resp = client.get("/broker/latency?window_s=0")
        assert resp.status_code == 200
        assert resp.json()["requested_window_s"] == 10.0


# ─────────────────────────────────────────────────────────────────────────────
# Buffer-isolation safety net (cleanup)
# ─────────────────────────────────────────────────────────────────────────────


def teardown_module(_module) -> None:
    """Ring buffer is module-level state; reset it between test modules."""
    server_mod._recent_requests = deque(maxlen=server_mod._recent_requests.maxlen)
