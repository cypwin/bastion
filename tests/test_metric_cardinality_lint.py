"""Tests for the Prometheus label-cardinality CI lint.

The lint (``scripts/check_metric_cardinality.py``) enforces Section 3 rule #2 of
the observability-expansion spec: metric ``labelnames`` lists may use only
bounded label *names*. It parses ``src/bastion/metrics.py`` with the ``ast``
module, extracts every metric definition's ``labelnames=[...]`` literal, and
fails on any label outside the permitted-set.

Contract under test (spec Section 5.6 + Section 10.x):
  - the lint exits 0 against the real, disciplined ``metrics.py``;
  - a synthetic source with ``labelnames=['pid']`` exits non-zero;
  - the spec's observatory permitted-set (Section 3) is present verbatim in the
    script — the lint validates label NAMES, never label VALUES.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_metric_cardinality.py"
_REAL_METRICS = _REPO_ROOT / "src" / "bastion" / "metrics.py"

# The observatory permitted-set exactly as defined in spec Section 3 / 5.6:
#   {model, resource, device, op, reason, kind, factor, xid_code, gpu_index}
_SPEC_OBSERVATORY_PERMITTED = frozenset(
    {"model", "resource", "device", "op", "reason", "kind", "factor", "xid_code", "gpu_index"}
)


def _load_lint() -> ModuleType:
    """Import ``scripts/check_metric_cardinality.py`` as a module."""
    spec = importlib.util.spec_from_file_location("check_metric_cardinality", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_metric_cardinality"] = module
    spec.loader.exec_module(module)
    return module


def test_script_file_exists() -> None:
    assert _SCRIPT_PATH.is_file(), f"missing lint script at {_SCRIPT_PATH}"


def test_spec_observatory_permitted_set_present_verbatim() -> None:
    """The script's permitted-set must contain the spec Section 3 set exactly."""
    lint = _load_lint()
    permitted = set(lint.PERMITTED_LABELS)
    missing = _SPEC_OBSERVATORY_PERMITTED - permitted
    assert not missing, f"spec Section 3 observatory labels absent from lint: {sorted(missing)}"


def test_lint_passes_on_real_metrics() -> None:
    """The lint MUST exit 0 against the current, disciplined metrics.py."""
    lint = _load_lint()
    violations = lint.find_violations(_REAL_METRICS.read_text(), source_name=str(_REAL_METRICS))
    assert violations == [], (
        "real metrics.py unexpectedly violates the cardinality lint — this is a "
        f"real bug, not a lint to weaken: {violations}"
    )


def test_main_returns_zero_on_real_metrics() -> None:
    """The CLI entry point returns exit code 0 against the real metrics.py."""
    lint = _load_lint()
    rc = lint.main([str(_REAL_METRICS)])
    assert rc == 0


def test_lint_fails_on_planted_pid_label(tmp_path: Path) -> None:
    """A synthetic source with labelnames=['pid'] must be reported and exit non-zero."""
    lint = _load_lint()
    synthetic = (
        "from prometheus_client import Counter\n"
        "BAD = Counter('bastion_bad_total', 'planted', labelnames=['pid'])\n"
    )
    violations = lint.find_violations(synthetic, source_name="synthetic")
    assert violations, "lint failed to flag a planted labelnames=['pid']"
    assert any(v.label == "pid" for v in violations)
    assert any("bastion_bad_total" in (v.metric or "") for v in violations)

    bad_file = tmp_path / "bad_metrics.py"
    bad_file.write_text(synthetic)
    rc = lint.main([str(bad_file)])
    assert rc != 0


@pytest.mark.parametrize("good_labels", [["gpu_index"], ["device", "op"]])
def test_lint_passes_on_permitted_labels(good_labels: list[str]) -> None:
    """Permitted observatory labels (gpu_index; device,op) must pass."""
    lint = _load_lint()
    label_literal = ", ".join(repr(label) for label in good_labels)
    synthetic = (
        "from prometheus_client import Gauge\n"
        f"OK = Gauge('bastion_ok', 'permitted', labelnames=[{label_literal}])\n"
    )
    violations = lint.find_violations(synthetic, source_name="synthetic")
    assert violations == [], f"permitted labels {good_labels} wrongly flagged: {violations}"


def test_lint_validates_names_not_values(tmp_path: Path) -> None:
    """A device='sda'/'vdb' VALUE is never rejected — only NAMES are checked."""
    lint = _load_lint()
    # The lint inspects labelnames literals only; label values never appear in
    # the metric definition, so a non-NVMe device value cannot be a violation.
    synthetic = (
        "from prometheus_client import Gauge\n"
        "G = Gauge('bastion_io', 'io', labelnames=['device'])\n"
        "G.labels(device='sda').set(1)\n"
        "G.labels(device='vdb').set(2)\n"
    )
    violations = lint.find_violations(synthetic, source_name="synthetic")
    assert violations == []
