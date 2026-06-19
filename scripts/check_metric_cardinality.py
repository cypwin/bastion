#!/usr/bin/env python3
"""CI lint: enforce the Prometheus label permitted-set (cardinality discipline).

Enforces Section 3 rule #2 of the observability-expansion spec
(``docs/design/specs/2026-06-19-observability-expansion.md``): metric
``labelnames`` lists may use only *bounded* label **names**. Per-PID,
per-request-id, per-task-id, and per-context-id labels are forbidden because
they unbound Prometheus series cardinality and can OOM the scrape target.

The lint parses ``src/bastion/metrics.py`` with the :mod:`ast` module (no import,
no prometheus_client needed), extracts every metric definition's literal
``labelnames=[...]`` list, and checks each label name against the permitted-set.
It validates label **names only**, never label **values** — so a
``device="sda"`` or ``device="vdb"`` series is exactly as valid as
``device="nvme0n1"`` (spec rev. 3). It prints every offending ``label`` with its
metric and exits non-zero on any violation; exit 0 when all labels are bounded.

Usage (standalone / CI)::

    python scripts/check_metric_cardinality.py                 # lints src/bastion/metrics.py
    python scripts/check_metric_cardinality.py path/to/other.py [more.py ...]

The repository CI workflow (``.github/workflows/ci.yml``) invokes this as a
dedicated step so a regression (e.g. a planted ``labelnames=['pid']``) fails the
build.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Permitted-set
# ---------------------------------------------------------------------------
#
# Observatory permitted-set, verbatim from spec Section 3 (rule #2) and the
# Section 5.6 catalogue row:
#
#   {model, resource, device, op, reason, kind, factor, xid_code, gpu_index}
#
# These are the bounded label NAMES the observability expansion introduces /
# sanctions. ``resource`` ∈ {cpu, memory, io}; ``device`` = dynamically
# discovered base storage device (nvme*/sd*/vd*/mmcblk*/hd*, 1-8 per host);
# ``reason``/``kind``/``factor`` are fixed enums (<=5); ``xid_code`` is the <=15
# known NVIDIA codes + ``unknown``; ``gpu_index`` is single-GPU "0" today, the
# seam for a non-breaking multi-GPU future.
SPEC_OBSERVATORY_PERMITTED: frozenset[str] = frozenset(
    {
        "model",
        "resource",
        "device",
        "op",
        "reason",
        "kind",
        "factor",
        "xid_code",
        "gpu_index",
    }
)

# Pre-existing bounded labels that predate the observatory work and are
# sanctioned by ``metrics.py``'s own header ("Tier 1 (always safe)") — request,
# scheduler, Vision C, and A2A metrics. These are all bounded enums or
# model-derived names of the same low-cardinality class as ``model``; they are
# NOT per-id labels. Listing them explicitly keeps the lint exit-0 on the
# disciplined real ``metrics.py`` without weakening the per-id blacklist below.
#
#   endpoint / status_code / tier  -> request metrics (bounded)
#   from_model / to_model          -> model-swap (same class as ``model``)
#   priority                       -> {interactive, agent, pipeline, background}
#   agent_id / verdict             -> Vision C thrashing (registered name or
#                                     /24 IP prefix; NEVER a task UUID — Risk R3)
#   skill / state                  -> A2A bounded enums
#   method / error_code            -> A2A bounded enums
LEGACY_BOUNDED_LABELS: frozenset[str] = frozenset(
    {
        "endpoint",
        "status_code",
        "tier",
        "from_model",
        "to_model",
        "priority",
        "agent_id",
        "verdict",
        "skill",
        "state",
        "method",
        "error_code",
    }
)

# Full set of label NAMES the lint accepts.
PERMITTED_LABELS: frozenset[str] = SPEC_OBSERVATORY_PERMITTED | LEGACY_BOUNDED_LABELS

# Explicit deny-list of unbounded-cardinality label names. Anything matching is
# rejected even though the permitted-set check already would, so the failure
# message is unambiguous about *why*. This is the concrete teeth of Section 3
# rule #2.
FORBIDDEN_LABELS: frozenset[str] = frozenset(
    {
        "pid",
        "request_id",
        "task_id",
        "context_id",
        "trace_id",
        "span_id",
        "session_id",
        "agent_pid",
        "process_id",
    }
)

# Metric constructor callables we inspect (matched by trailing attribute / name).
_METRIC_CTORS: frozenset[str] = frozenset({"Counter", "Gauge", "Histogram", "Summary"})

_DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "src" / "bastion" / "metrics.py"


@dataclass(frozen=True)
class Violation:
    """A single offending label on a metric definition."""

    label: str
    metric: str | None
    source: str
    lineno: int
    reason: str

    def render(self) -> str:
        where = f"{self.source}:{self.lineno}"
        metric = self.metric or "<unknown-metric>"
        return f"{where}: metric '{metric}' uses forbidden label '{self.label}' ({self.reason})"


def _ctor_name(func: ast.expr) -> str | None:
    """Return the trailing callable name for ``Counter(...)`` / ``x.Counter(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _metric_name(call: ast.Call) -> str | None:
    """Best-effort extraction of the metric name (first positional string arg)."""
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for kw in call.keywords:
        if kw.arg != "name" or not isinstance(kw.value, ast.Constant):
            continue
        if isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _iter_labelnames(call: ast.Call) -> list[ast.Constant] | None:
    """Return the literal ``labelnames`` list/tuple elements, or None if absent."""
    for kw in call.keywords:
        if kw.arg == "labelnames" and isinstance(kw.value, (ast.List, ast.Tuple)):
            return [el for el in kw.value.elts if isinstance(el, ast.Constant)]
    return None


def find_violations(source: str, source_name: str = "<source>") -> list[Violation]:
    """Parse ``source`` and return every label-name cardinality violation.

    Parameters
    ----------
    source:
        Python source text of a metrics module.
    source_name:
        Display name used in violation messages (typically the file path).

    Returns
    -------
    list[Violation]
        One entry per offending label; empty when every label is bounded.
    """
    tree = ast.parse(source, filename=source_name)
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _ctor_name(node.func) not in _METRIC_CTORS:
            continue
        elements = _iter_labelnames(node)
        if elements is None:
            continue
        metric = _metric_name(node)
        for el in elements:
            if not isinstance(el.value, str):
                # Non-string label literal — flag as unparseable/illegal.
                violations.append(
                    Violation(
                        label=repr(el.value),
                        metric=metric,
                        source=source_name,
                        lineno=el.lineno,
                        reason="non-string label literal",
                    )
                )
                continue
            label = el.value
            if label in FORBIDDEN_LABELS:
                violations.append(
                    Violation(
                        label=label,
                        metric=metric,
                        source=source_name,
                        lineno=el.lineno,
                        reason="unbounded per-id cardinality (deny-listed)",
                    )
                )
            elif label not in PERMITTED_LABELS:
                violations.append(
                    Violation(
                        label=label,
                        metric=metric,
                        source=source_name,
                        lineno=el.lineno,
                        reason="not in permitted-set",
                    )
                )

    return violations


def lint_path(path: Path) -> list[Violation]:
    """Lint a single file path, returning its violations."""
    return find_violations(path.read_text(), source_name=str(path))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code (0 = clean, 1 = violations)."""
    parser = argparse.ArgumentParser(
        description="Enforce the Prometheus label permitted-set (cardinality lint).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[_DEFAULT_TARGET],
        help="Metric source file(s) to lint (default: src/bastion/metrics.py).",
    )
    args = parser.parse_args(argv)
    paths: list[Path] = list(args.paths) or [_DEFAULT_TARGET]

    all_violations: list[Violation] = []
    for path in paths:
        if not path.is_file():
            print(f"error: not a file: {path}", file=sys.stderr)
            return 2
        all_violations.extend(lint_path(path))

    if all_violations:
        print("Prometheus label-cardinality violations found:", file=sys.stderr)
        for v in all_violations:
            print(f"  {v.render()}", file=sys.stderr)
        print(
            f"\n{len(all_violations)} violation(s). Permitted label names: "
            f"{sorted(PERMITTED_LABELS)}",
            file=sys.stderr,
        )
        return 1

    targets = ", ".join(str(p) for p in paths)
    print(f"OK: all metric labelnames are within the permitted-set ({targets}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
