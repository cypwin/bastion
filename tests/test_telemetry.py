"""Tests for telemetry.py: no-op behavior, span creation, context propagation.

Covers D1: verify all functions are no-ops when OTel missing,
verify span creation/linking when OTel available (mock the SDK).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import bastion.telemetry as telemetry

# ---------------------------------------------------------------------------
# No-op behavior when OTel unavailable
# ---------------------------------------------------------------------------


class TestNoOpWhenOTelMissing:
    """All telemetry functions should be no-ops when OTEL_AVAILABLE is False."""

    def setup_method(self) -> None:
        """Reset module state before each test."""
        telemetry._tracer = None
        telemetry._enabled = False

    def test_is_enabled_false_by_default(self) -> None:
        assert telemetry.is_enabled() is False

    def test_get_tracer_none_by_default(self) -> None:
        assert telemetry.get_tracer() is None

    def test_record_task_submit_returns_empty_dict(self) -> None:
        result = telemetry.record_task_submit("task-1", "infer", "model")
        assert result == {}

    def test_record_task_process_returns_none(self) -> None:
        result = telemetry.record_task_process("task-1", "infer", "model")
        assert result is None

    def test_record_queue_wait_yields_without_span(self) -> None:
        with telemetry.record_queue_wait("req-1", "model"):
            pass  # Should not raise

    def test_record_model_swap_yields_without_span(self) -> None:
        with telemetry.record_model_swap("model-a", "model-b"):
            pass  # Should not raise

    def test_record_inference_yields_none(self) -> None:
        with telemetry.record_inference("model") as span:
            assert span is None

    def test_set_inference_tokens_noop_with_none_span(self) -> None:
        telemetry.set_inference_tokens(None, input_tokens=10, output_tokens=20)
        # Should not raise

    def test_end_span_noop_with_none(self) -> None:
        telemetry.end_span(None)
        # Should not raise

    def test_end_span_noop_with_error(self) -> None:
        telemetry.end_span(None, error="Some error")
        # Should not raise

    def test_inject_trace_context_returns_empty(self) -> None:
        result = telemetry.inject_trace_context()
        assert result == {}

    def test_extract_trace_context_returns_none(self) -> None:
        result = telemetry.extract_trace_context({"traceparent": "fake"})
        assert result is None

    def test_shutdown_noop_when_disabled(self) -> None:
        telemetry.shutdown_telemetry()
        # Should not raise

    def test_init_with_disabled_config(self) -> None:
        mock_config = MagicMock()
        mock_config.enabled = False
        telemetry.init_telemetry(mock_config)
        assert telemetry.is_enabled() is False

    def test_init_with_otel_unavailable(self) -> None:
        mock_config = MagicMock()
        mock_config.enabled = True
        with patch.object(telemetry, "OTEL_AVAILABLE", False):
            telemetry.init_telemetry(mock_config)
        assert telemetry.is_enabled() is False


# ---------------------------------------------------------------------------
# Behavior when OTel is available (mocked)
# ---------------------------------------------------------------------------


class TestWithMockedOTel:
    """Test span creation and linking with a mocked OTel tracer."""

    def setup_method(self) -> None:
        """Set up a mock tracer for each test."""
        telemetry._tracer = None
        telemetry._enabled = False

    def _enable_mock_tracer(self) -> MagicMock:
        """Enable telemetry with a mock tracer and return it."""
        mock_tracer = MagicMock()
        telemetry._tracer = mock_tracer
        telemetry._enabled = True
        return mock_tracer

    def test_is_enabled_true_with_tracer(self) -> None:
        self._enable_mock_tracer()
        assert telemetry.is_enabled() is True

    def test_get_tracer_returns_mock(self) -> None:
        mock = self._enable_mock_tracer()
        assert telemetry.get_tracer() is mock

    def test_record_task_submit_creates_span(self) -> None:
        if not telemetry.OTEL_AVAILABLE:
            pytest.skip("OTel not installed")

        mock_tracer = self._enable_mock_tracer()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        telemetry.record_task_submit("task-1", "infer", "qwen3:14b")
        mock_tracer.start_as_current_span.assert_called_once()
        call_args = mock_tracer.start_as_current_span.call_args
        assert call_args[0][0] == "a2a.task.submit"

    def test_record_task_process_creates_span(self) -> None:
        if not telemetry.OTEL_AVAILABLE:
            pytest.skip("OTel not installed")

        mock_tracer = self._enable_mock_tracer()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        telemetry.record_task_process("task-1", "infer", "model")
        mock_tracer.start_span.assert_called_once()
        call_args = mock_tracer.start_span.call_args
        assert call_args[0][0] == "a2a.task.process"

    def test_end_span_calls_end(self) -> None:
        if not telemetry.OTEL_AVAILABLE:
            pytest.skip("OTel not installed")

        self._enable_mock_tracer()
        mock_span = MagicMock()
        telemetry.end_span(mock_span)
        mock_span.end.assert_called_once()

    def test_end_span_records_error(self) -> None:
        if not telemetry.OTEL_AVAILABLE:
            pytest.skip("OTel not installed")

        self._enable_mock_tracer()
        mock_span = MagicMock()
        telemetry.end_span(mock_span, error="Something went wrong")
        mock_span.set_status.assert_called_once()
        mock_span.set_attribute.assert_called_once_with("error.message", "Something went wrong")
        mock_span.end.assert_called_once()

    def test_set_inference_tokens_sets_attributes(self) -> None:
        if not telemetry.OTEL_AVAILABLE:
            pytest.skip("OTel not installed")

        self._enable_mock_tracer()
        mock_span = MagicMock()
        telemetry.set_inference_tokens(mock_span, input_tokens=100, output_tokens=200)
        assert mock_span.set_attribute.call_count == 2

    def test_set_inference_tokens_partial(self) -> None:
        if not telemetry.OTEL_AVAILABLE:
            pytest.skip("OTel not installed")

        self._enable_mock_tracer()
        mock_span = MagicMock()
        telemetry.set_inference_tokens(mock_span, output_tokens=50)
        mock_span.set_attribute.assert_called_once()

    def teardown_method(self) -> None:
        """Reset module state."""
        telemetry._tracer = None
        telemetry._enabled = False
