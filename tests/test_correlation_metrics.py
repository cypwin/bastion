"""Prometheus-metric tests for the correlation engine integration (T5).

The correlation engine surfaces five NEW metrics, and the cardinality rule
(Constraint #2) is load-bearing: process-attribution data is NEVER a label;
correlation metrics use ONLY bounded labels (``factor``/``kind`` enums). These
tests assert the metric objects exist, carry only the permitted bounded label
names, and that their helper functions move the underlying value.

  - ``bastion_risk_index`` — Gauge, NO labels (spec 6.4 / 7).
  - ``bastion_risk_dominant_factor_total{factor}`` — Counter, 5 bounded names.
  - ``bastion_contention_events_total{kind}`` — Counter, 4 bounded kinds.
  - ``bastion_thermal_coupling_active`` — Gauge, NO labels (0/1).
  - ``bastion_thermal_headroom_celsius`` — Gauge, NO labels.
"""

from __future__ import annotations

import pytest

import bastion.metrics as metrics
from bastion.correlation import RISK_COMPONENT_NAMES


# ─────────────────────────────────────────────────────────────────────────────
# Metric objects exist with the right (bounded) label names
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrelationMetricObjectsExist:
    def test_risk_index_gauge_exists(self) -> None:
        assert hasattr(metrics, "RISK_INDEX")

    def test_risk_dominant_factor_counter_exists(self) -> None:
        assert hasattr(metrics, "RISK_DOMINANT_FACTOR_TOTAL")

    def test_contention_events_counter_exists(self) -> None:
        assert hasattr(metrics, "CONTENTION_EVENTS_TOTAL")

    def test_thermal_coupling_active_gauge_exists(self) -> None:
        assert hasattr(metrics, "THERMAL_COUPLING_ACTIVE")

    def test_thermal_headroom_gauge_exists(self) -> None:
        assert hasattr(metrics, "THERMAL_HEADROOM_CELSIUS")


# ─────────────────────────────────────────────────────────────────────────────
# BOUNDED-LABEL discipline (Constraint #2). Only factor/kind enums are allowed;
# the value-less gauges carry NO labels at all.
# ─────────────────────────────────────────────────────────────────────────────


def _labelnames(metric: object) -> tuple[str, ...]:
    """Return the configured Prometheus label names for a metric object."""
    # prometheus_client stores them on ``_labelnames``; the no-op stub omits it.
    return tuple(getattr(metric, "_labelnames", ()) or ())


@pytest.mark.skipif(
    not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed"
)
class TestBoundedLabelsOnly:
    def test_risk_index_has_no_labels(self) -> None:
        assert _labelnames(metrics.RISK_INDEX) == ()

    def test_thermal_coupling_active_has_no_labels(self) -> None:
        assert _labelnames(metrics.THERMAL_COUPLING_ACTIVE) == ()

    def test_thermal_headroom_has_no_labels(self) -> None:
        assert _labelnames(metrics.THERMAL_HEADROOM_CELSIUS) == ()

    def test_dominant_factor_label_is_only_factor(self) -> None:
        assert _labelnames(metrics.RISK_DOMINANT_FACTOR_TOTAL) == ("factor",)

    def test_contention_events_label_is_only_kind(self) -> None:
        assert _labelnames(metrics.CONTENTION_EVENTS_TOTAL) == ("kind",)

    def test_no_correlation_metric_uses_a_forbidden_label(self) -> None:
        # Constraint #2: no per-PID/per-request/per-task/per-context labels.
        forbidden = {"pid", "request_id", "task_id", "context_id", "process"}
        for name in (
            "RISK_INDEX",
            "RISK_DOMINANT_FACTOR_TOTAL",
            "CONTENTION_EVENTS_TOTAL",
            "THERMAL_COUPLING_ACTIVE",
            "THERMAL_HEADROOM_CELSIUS",
        ):
            labels = set(_labelnames(getattr(metrics, name)))
            assert labels & forbidden == set(), f"{name} has a forbidden label"


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions move the underlying value (and accept only bounded labels)
# ─────────────────────────────────────────────────────────────────────────────


class TestCorrelationMetricHelpers:
    def test_update_risk_index_callable(self) -> None:
        # Should not raise on the no-op stub OR the real gauge.
        metrics.update_risk_index(0.42)

    def test_record_dominant_factor_accepts_each_bounded_name(self) -> None:
        for name in RISK_COMPONENT_NAMES:
            metrics.record_risk_dominant_factor(name)

    def test_record_contention_event_accepts_each_bounded_kind(self) -> None:
        for kind in ("nvme_burst", "mem_pressure", "cpu_contention", "combined"):
            metrics.record_contention_event(kind)

    def test_update_thermal_coupling_active_callable(self) -> None:
        metrics.update_thermal_coupling_active(True)
        metrics.update_thermal_coupling_active(False)

    def test_update_thermal_headroom_callable(self) -> None:
        metrics.update_thermal_headroom_celsius(12.5)

    @pytest.mark.skipif(
        not metrics.PROMETHEUS_AVAILABLE,
        reason="prometheus_client not installed",
    )
    def test_update_risk_index_sets_gauge_value(self) -> None:
        metrics.update_risk_index(0.77)
        # _value.get() is the prometheus_client gauge value accessor.
        assert metrics.RISK_INDEX._value.get() == pytest.approx(0.77)

    @pytest.mark.skipif(
        not metrics.PROMETHEUS_AVAILABLE,
        reason="prometheus_client not installed",
    )
    def test_dominant_factor_counter_increments(self) -> None:
        before = metrics.RISK_DOMINANT_FACTOR_TOTAL.labels(
            factor="vram_headroom"
        )._value.get()
        metrics.record_risk_dominant_factor("vram_headroom")
        after = metrics.RISK_DOMINANT_FACTOR_TOTAL.labels(
            factor="vram_headroom"
        )._value.get()
        assert after == pytest.approx(before + 1.0)

    def test_helpers_in_all(self) -> None:
        for helper in (
            "update_risk_index",
            "record_risk_dominant_factor",
            "record_contention_event",
            "update_thermal_coupling_active",
            "update_thermal_headroom_celsius",
        ):
            assert helper in metrics.__all__
