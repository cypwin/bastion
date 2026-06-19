"""Presence + content-lint for the observability-expansion governance docs (T3).

Phase 4 of the inference-correlated observatory (spec
``docs/design/specs/2026-06-19-observability-expansion.md`` §5.6/§9) is largely a
documentation deliverable: ADR-005-B, an ADR-009 addendum, a Prometheus
metric-freeze proposal, and a GATED Grafana panel catalogue. This module asserts
the four new docs exist and that the two load-bearing governance contracts are
actually written down:

* ADR-005-B references the ``mcp_adapter`` gate (ADR-007) and records the
  MCP ``broker_snapshot_v1`` tool as the third operational surface
  (ADR-005 gating event #1).
* the metric-freeze doc references the cardinality **permitted-set** and the
  v0.6 freeze target.

These are cheap, non-flaky structural checks — they do not parse Markdown
semantics, only assert the files are present and contain the governance anchors
the task requires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Repo root = two levels up from this test file (tests/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent

ADR_005_B = _REPO_ROOT / "docs" / "adrs" / "ADR-005-B-mcp-third-surface.md"
ADR_009 = _REPO_ROOT / "docs" / "adrs" / "ADR-009-tui-deprecation-trigger.md"
METRIC_FREEZE = (
    _REPO_ROOT
    / "docs"
    / "design"
    / "specs"
    / "2026-06-19-observability-expansion-metric-freeze.md"
)
GRAFANA_CATALOGUE = (
    _REPO_ROOT
    / "docs"
    / "design"
    / "specs"
    / "2026-06-19-observability-expansion-grafana-catalogue.md"
)

# The four NEW docs this task produces (the ADR-009 addendum is appended to the
# existing ADR-009, so it is checked separately, not as a new file).
_NEW_DOCS = [ADR_005_B, METRIC_FREEZE, GRAFANA_CATALOGUE]


@pytest.mark.parametrize("doc", _NEW_DOCS, ids=lambda p: p.name)
def test_new_doc_exists_and_nonempty(doc: Path) -> None:
    """Each new governance doc exists and has real content."""
    assert doc.exists(), f"expected doc missing: {doc}"
    assert doc.stat().st_size > 0, f"doc is empty: {doc}"


def test_adr_005_b_references_mcp_adapter_gate_and_third_surface() -> None:
    """ADR-005-B records the mcp_adapter gate + the third-surface trigger."""
    text = ADR_005_B.read_text(encoding="utf-8")
    assert "mcp_adapter" in text, "ADR-005-B must name the mcp_adapter package gate"
    assert "broker_snapshot_v1" in text, "ADR-005-B must name the MCP tool"
    assert "ADR-007" in text, "ADR-005-B must cite ADR-007 (MCP versioning)"
    # The governance anchor: this is ADR-005 gating event #1 / the third surface.
    lowered = text.lower()
    assert "gating event #1" in lowered, "ADR-005-B must record ADR-005 gating event #1"
    assert "third operational surface" in lowered or "third surface" in lowered, (
        "ADR-005-B must record the third-operational-surface trigger"
    )
    # And that the subscriber/pub-sub bus stays deferred.
    assert "subscriber" in lowered, "ADR-005-B must address the subscriber-bus deferral"


def test_metric_freeze_references_permitted_set_and_v06() -> None:
    """The metric-freeze doc references the permitted-set and the v0.6 freeze."""
    text = METRIC_FREEZE.read_text(encoding="utf-8")
    assert "permitted-set" in text.lower(), (
        "metric-freeze doc must reference the cardinality permitted-set"
    )
    assert "v0.6" in text, "metric-freeze doc must propose freezing at the v0.6 tag"
    # Sanity: it must enumerate at least one real new metric object name.
    assert "bastion_risk_index" in text, (
        "metric-freeze doc must enumerate the real new metric objects from metrics.py"
    )
    # The permitted-set members should be spelled out (label-name discipline).
    for label in ("gpu_index", "factor", "kind"):
        assert label in text, f"metric-freeze doc must name permitted label '{label}'"


def test_grafana_catalogue_is_gated_no_json_authored() -> None:
    """The Grafana catalogue is intent-only and explicitly gated."""
    text = GRAFANA_CATALOGUE.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "gated" in lowered, "Grafana catalogue must mark itself GATED"
    assert "dashboards/grafana" in text, (
        "Grafana catalogue must reference the gating dashboards/grafana dir"
    )
    assert "vision c" in lowered, "Grafana catalogue must cite the Vision C base gate"


def test_adr_009_addendum_references_observability_baseline() -> None:
    """ADR-009 has a dated addendum naming the observability expansion baseline."""
    text = ADR_009.read_text(encoding="utf-8")
    assert "Addendum" in text, "ADR-009 must carry the dated addendum heading"
    assert "2026-06-19" in text, "ADR-009 addendum must be dated 2026-06-19"
    lowered = text.lower()
    assert "baseline" in lowered, (
        "ADR-009 addendum must record the TUI-instrumentation baseline reference"
    )
    assert "observability" in lowered, (
        "ADR-009 addendum must reference the observability expansion"
    )
