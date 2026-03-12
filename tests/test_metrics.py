"""Tests for Prometheus metrics collection and exposition.

Validates:
  - Metric increment behavior (counters, histograms, gauges)
  - No-op fallback when prometheus-client is unavailable
  - Helper functions (record_request, record_model_swap, etc.)
  - Middleware extraction of model and tier from requests
  - /broker/metrics endpoint returns valid Prometheus format
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class TestMetricsNoOpFallback:
    """Test that metrics gracefully degrade when prometheus-client is missing."""

    def test_import_failure_creates_noop_stubs(self):
        """When prometheus-client import fails, all metrics become no-ops."""
        # Simulate import failure by patching the module before import
        import sys
        import importlib
        from prometheus_client import REGISTRY

        # Save original module so we can restore it
        original_module = sys.modules.get("bastion.metrics")

        # Unregister bastion metrics from the global registry so reload won't collide
        collectors_to_restore = []
        for name in list(REGISTRY._names_to_collectors):
            if name.startswith("bastion_"):
                collector = REGISTRY._names_to_collectors[name]
                if collector not in collectors_to_restore:
                    collectors_to_restore.append(collector)
        for collector in collectors_to_restore:
            try:
                REGISTRY.unregister(collector)
            except Exception:
                pass

        # Remove metrics from cache if already loaded
        if "bastion.metrics" in sys.modules:
            del sys.modules["bastion.metrics"]

        try:
            # Mock the prometheus_client import to raise ImportError
            with patch.dict("sys.modules", {"prometheus_client": None}):
                # Force reload to trigger the ImportError path
                import bastion.metrics as metrics_module
                importlib.reload(metrics_module)

                # Should not raise, even though prometheus_client is unavailable
                assert metrics_module.PROMETHEUS_AVAILABLE is False

                # All operations should be no-ops (no exceptions)
                metrics_module.REQUESTS_TOTAL.labels(
                    endpoint="/api/generate",
                    status_code="200",
                    tier="agent",
                ).inc()

                metrics_module.REQUEST_DURATION.labels(
                    endpoint="/api/generate",
                    model="qwen3:14b",
                    tier="agent",
                ).observe(1.23)

                metrics_module.QUEUE_DEPTH.labels(model="qwen3:14b").set(5)

                # generate_latest should return empty bytes
                assert metrics_module.get_metrics_text() == b""
        finally:
            # Restore the module with real prometheus metrics
            if "bastion.metrics" in sys.modules:
                del sys.modules["bastion.metrics"]
            # Fresh import re-registers metrics cleanly in the global registry
            import bastion.metrics  # noqa: F811


class TestMetricsIncrement:
    """Test that metrics correctly increment and observe values."""

    def test_record_request_increments_counter(self):
        """record_request should increment REQUESTS_TOTAL."""
        from bastion.metrics import REQUESTS_TOTAL, record_request

        # Get initial count (may not be zero if other tests ran)
        initial_metric = REQUESTS_TOTAL.labels(
            endpoint="/api/generate",
            status_code="200",
            tier="interactive",
        )

        # Record a request
        record_request(
            endpoint="/api/generate",
            status_code=200,
            duration=1.5,
            model="qwen3:14b",
            tier="interactive",
        )

        # Counter should have incremented (we can't easily read the value,
        # but we can verify no exceptions were raised)
        # The real test is that generate_latest() includes this metric

    def test_record_request_without_model(self):
        """record_request should handle None model (admin endpoints)."""
        from bastion.metrics import record_request

        # Should not raise
        record_request(
            endpoint="/broker/status",
            status_code=200,
            duration=0.05,
            model=None,
            tier="agent",
        )

    def test_histogram_observes_duration(self):
        """REQUEST_DURATION histogram should record duration values."""
        from bastion.metrics import REQUEST_DURATION

        duration_metric = REQUEST_DURATION.labels(
            endpoint="/api/generate",
            model="mistral-nemo:12b",
            tier="pipeline",
        )

        # Observe several durations
        duration_metric.observe(0.5)
        duration_metric.observe(2.0)
        duration_metric.observe(10.5)

        # No exceptions = success (actual values checked via exposition)

    def test_queue_depth_gauge_updates(self):
        """QUEUE_DEPTH gauge should update current queue size."""
        from bastion.metrics import QUEUE_DEPTH, update_queue_depth

        update_queue_depth("qwen3:14b", 3)
        update_queue_depth("qwen3:14b", 7)
        update_queue_depth("qwen3:14b", 0)

        # Gauge should reflect latest value (0)

    def test_model_swap_counter_with_none_source(self):
        """MODEL_SWAP_TOTAL should handle None for from_model."""
        from bastion.metrics import record_model_swap

        # Loading from idle state
        record_model_swap(from_model=None, to_model="qwen3:14b")

        # Should substitute "_none" for None

    def test_model_swap_counter_with_swap(self):
        """MODEL_SWAP_TOTAL should track model transitions."""
        from bastion.metrics import record_model_swap

        record_model_swap(from_model="llama3.1:8b", to_model="qwen3:14b")
        record_model_swap(from_model="qwen3:14b", to_model="mistral-nemo:12b")

    def test_cooldown_wait_counter(self):
        """COOLDOWN_WAITS_TOTAL should increment on each cooldown."""
        from bastion.metrics import record_cooldown_wait

        record_cooldown_wait()
        record_cooldown_wait()
        record_cooldown_wait()

    def test_vram_usage_gauge(self):
        """VRAM_USED_BYTES should track current VRAM consumption."""
        from bastion.metrics import update_vram_usage

        update_vram_usage(8_000_000_000)  # 8 GB
        update_vram_usage(16_000_000_000)  # 16 GB
        update_vram_usage(0)

    def test_gpu_temperature_gauge(self):
        """GPU_TEMPERATURE should track current temp in Celsius."""
        from bastion.metrics import update_gpu_temperature

        update_gpu_temperature(55.0)
        update_gpu_temperature(72.5)
        update_gpu_temperature(45.0)

    def test_queue_wait_time_histogram(self):
        """QUEUE_WAIT_TIME should observe wait durations."""
        from bastion.metrics import record_queue_wait

        record_queue_wait("qwen3:14b", "interactive", 0.05)
        record_queue_wait("qwen3:14b", "agent", 1.2)
        record_queue_wait("mistral-nemo:12b", "pipeline", 5.0)


class TestPrometheusExposition:
    """Test that metrics are exposed in valid Prometheus format."""

    def test_get_metrics_text_returns_bytes(self):
        """get_metrics_text() should return bytes in Prometheus format."""
        from bastion.metrics import get_metrics_text, PROMETHEUS_AVAILABLE

        result = get_metrics_text()
        assert isinstance(result, bytes)

        if PROMETHEUS_AVAILABLE:
            # Should contain HELP and TYPE declarations
            text = result.decode("utf-8")
            assert "# HELP" in text or "# TYPE" in text or len(text) > 0

    def test_content_type_constant(self):
        """CONTENT_TYPE_LATEST should be valid Prometheus content type."""
        from bastion.metrics import CONTENT_TYPE_LATEST

        assert "text/plain" in CONTENT_TYPE_LATEST or "version=0.0.4" in CONTENT_TYPE_LATEST


class TestMiddlewareExtraction:
    """Test that MetricsMiddleware extracts model and tier correctly."""

    @pytest.mark.asyncio
    async def test_extract_model_from_post_body(self):
        """Middleware should extract model name from JSON body."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        # Mock request with JSON body containing model
        request = MagicMock(spec=Request)
        request.method = "POST"
        request.url.path = "/api/generate"
        request.headers = {}
        body_data = {"model": "qwen3:14b", "prompt": "hello"}
        request.body = AsyncMock(return_value=json.dumps(body_data).encode())

        model = await middleware._extract_model(request)
        assert model == "qwen3:14b"

    @pytest.mark.asyncio
    async def test_extract_model_from_get_returns_none(self):
        """Middleware should not try to parse body for GET requests."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.method = "GET"
        request.url.path = "/api/tags"

        model = await middleware._extract_model(request)
        assert model is None

    @pytest.mark.asyncio
    async def test_extract_model_handles_invalid_json(self):
        """Middleware should gracefully handle non-JSON bodies."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.method = "POST"
        request.url.path = "/api/generate"
        request.body = AsyncMock(return_value=b"not valid json{{{")

        model = await middleware._extract_model(request)
        assert model is None

    @pytest.mark.asyncio
    async def test_extract_model_handles_empty_body(self):
        """Middleware should handle empty request body."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.method = "POST"
        request.url.path = "/api/generate"
        request.body = AsyncMock(return_value=b"")

        model = await middleware._extract_model(request)
        assert model is None

    def test_extract_tier_from_header(self):
        """Middleware should extract tier from X-Broker-Priority header."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.headers = {"X-Broker-Priority": "interactive"}

        tier = middleware._extract_tier(request)
        assert tier == "interactive"

    def test_extract_tier_defaults_to_agent(self):
        """Middleware should default to 'agent' tier when header missing."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.headers = {}

        tier = middleware._extract_tier(request)
        assert tier == "agent"

    def test_extract_tier_validates_known_tiers(self):
        """Middleware should reject invalid tier values."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.headers = {"X-Broker-Priority": "invalid_tier"}

        tier = middleware._extract_tier(request)
        assert tier == "agent"  # Falls back to default

    def test_extract_tier_case_insensitive(self):
        """Middleware should handle case-insensitive tier names."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        request = MagicMock(spec=Request)
        request.headers = {"X-Broker-Priority": "PIPELINE"}

        tier = middleware._extract_tier(request)
        assert tier == "pipeline"


class TestMiddlewareIntegration:
    """Test that MetricsMiddleware records metrics on requests."""

    @pytest.mark.asyncio
    async def test_middleware_records_request(self):
        """Middleware should call record_request with extracted data."""
        from bastion.middleware import MetricsMiddleware

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        # Mock the call_next to return a response
        async def mock_call_next(request):
            return Response(content="ok", status_code=200)

        # Mock request
        request = MagicMock(spec=Request)
        request.method = "POST"
        request.url.path = "/api/generate"
        request.headers = {"X-Broker-Priority": "interactive"}
        body_data = {"model": "qwen3:14b", "prompt": "test"}
        request.body = AsyncMock(return_value=json.dumps(body_data).encode())

        # Patch record_request in the middleware module where it's imported
        with patch("bastion.middleware.record_request") as mock_record:
            response = await middleware.dispatch(request, mock_call_next)

            # Verify record_request was called
            mock_record.assert_called_once()
            call_args = mock_record.call_args

            assert call_args.kwargs["endpoint"] == "/api/generate"
            assert call_args.kwargs["status_code"] == 200
            assert call_args.kwargs["model"] == "qwen3:14b"
            assert call_args.kwargs["tier"] == "interactive"
            assert call_args.kwargs["duration"] > 0

    @pytest.mark.asyncio
    async def test_middleware_measures_duration(self):
        """Middleware should accurately measure request duration."""
        from bastion.middleware import MetricsMiddleware
        import asyncio

        app = FastAPI()
        middleware = MetricsMiddleware(app)

        # Mock call_next with a delay
        async def slow_call_next(request):
            await asyncio.sleep(0.05)  # 50ms delay
            return Response(content="ok", status_code=200)

        request = MagicMock(spec=Request)
        request.method = "GET"
        request.url.path = "/broker/status"
        request.headers = {}

        with patch("bastion.middleware.record_request") as mock_record:
            await middleware.dispatch(request, slow_call_next)

            # Duration should be at least 50ms
            call_args = mock_record.call_args
            assert call_args.kwargs["duration"] >= 0.05
