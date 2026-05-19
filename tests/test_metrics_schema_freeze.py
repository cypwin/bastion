"""CI canary: the five Vision C metric names are part of BASTION's public contract.

This test is the schema-freeze enforcement for v0.4. Renaming or removing any
of the listed metrics is a breaking change for Grafana dashboards, Prometheus
rules, and Alertmanager expressions that ship with the docker-compose stack.

If you need to rename one of these metrics, the contract change requires:
  1. A major-version bump (v1.x -> v2.x), AND
  2. A migration note in CHANGELOG.md, AND
  3. Updates to docs/design/specs/2026-05-14-dashboard-v0.4-vision-c.md.

Otherwise: do not rename. Add a parallel metric with the new name and deprecate
the old one over at least one minor-version cycle.
"""

from __future__ import annotations

import pytest

# The five frozen names — DO NOT EDIT without following the migration steps above.
SCHEMA_FROZEN_METRIC_NAMES: tuple[str, ...] = (
    "bastion_model_swap_total",
    "bastion_request_queue_wait_seconds",
    "bastion_vram_used_mb",
    "bastion_thrashing_detector_halt_total",
    "bastion_concurrent_requests_active",
)


def test_five_public_metric_names_present() -> None:
    """Assert the five schema-frozen metric names exist and carry correct label keys.

    Emits one observation per metric (covering each label combination the
    Grafana dashboard and Alertmanager rules depend on), then scans the
    exposition output for each canonical name. The test passes when
    prometheus-client is available; with no-op stubs, ``get_metrics_text()``
    returns ``b""`` and the per-name assertions are skipped.
    """
    from bastion.metrics import (
        CONCURRENT_REQUESTS_ACTIVE,
        MODEL_SWAP_TOTAL,
        PROMETHEUS_AVAILABLE,
        REQUEST_QUEUE_WAIT,
        THRASHING_DETECTOR_HALT_TOTAL,
        VRAM_USED_MB,
        get_metrics_text,
    )

    # Emit one observation for each. Histograms must have a label-set to
    # appear in exposition; counters/gauges need at least one .inc()/.set()
    # so the series materializes.
    MODEL_SWAP_TOTAL.labels(
        from_model="_none",
        to_model="qwen3:8b",
        reason="scheduler_pick",
    ).inc()
    REQUEST_QUEUE_WAIT.labels(priority="agent", model="qwen3:8b").observe(0.1)
    VRAM_USED_MB.labels(gpu_index="0").set(8192)
    THRASHING_DETECTOR_HALT_TOTAL.labels(
        agent_id="test-agent",
        verdict="HALTED",
    ).inc()
    CONCURRENT_REQUESTS_ACTIVE.set(1)

    text = get_metrics_text().decode()

    if not PROMETHEUS_AVAILABLE:
        # With no-op stubs, exposition is empty by design. The schema is still
        # frozen — the imports above succeed, which is the binding contract.
        assert text == "", (
            "no-op stub path should produce empty exposition; got non-empty output"
        )
        pytest.skip("prometheus-client not installed; metric symbol imports verified")

    for name in SCHEMA_FROZEN_METRIC_NAMES:
        assert name in text, (
            f"Schema-frozen metric {name!r} missing from exposition. "
            "If this fails after a refactor: revert the rename. See "
            "docs/design/specs/2026-05-14-dashboard-v0.4-vision-c.md."
        )


def test_schema_frozen_helpers_are_importable() -> None:
    """All five Vision C helper functions must be importable.

    A rename of a helper without keeping a deprecated alias breaks the
    instrumentation call sites in scheduler.py, thrashing.py, and vram.py.
    """
    from bastion.metrics import (  # noqa: F401
        record_model_swap,
        record_queue_wait,
        record_thrashing_verdict,
        set_concurrent_requests_active,
        update_vram_used_mb,
    )


def test_schema_frozen_metric_objects_are_exported() -> None:
    """The five frozen metric objects must be re-exported via __all__."""
    import bastion.metrics as m

    frozen_symbols = (
        "MODEL_SWAP_TOTAL",
        "REQUEST_QUEUE_WAIT",
        "VRAM_USED_MB",
        "THRASHING_DETECTOR_HALT_TOTAL",
        "CONCURRENT_REQUESTS_ACTIVE",
    )
    for sym in frozen_symbols:
        assert sym in m.__all__, (
            f"{sym!r} must be in bastion.metrics.__all__ "
            "(part of the Vision C public contract)"
        )
