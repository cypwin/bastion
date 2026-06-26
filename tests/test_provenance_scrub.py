"""Provenance-scrub guard (F3/D1).

Asserts the tracked, user-facing GPU-tuning surfaces do NOT carry
RTX-5090-specific *crash-numeric* provenance (e.g. ">8 swaps/min crash
zone", "55-60 swaps", "crash investigation"). Those numbers were a single
card's empirical artefact; baking them into shipped code/config as if they
were universal misleads operators of other hardware. The portable framing
is a conservative floor that each operator calibrates via ``--stress-test``.

The swap-velocity circuit breaker (``swapbrake.py``) is the real system-wide
backstop; the per-agent thrashing detector is request-admission scoped and
structurally blind to a system-wide power event, so it must not advertise
itself with those crash numerics either.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The TRACKED template is config/broker.example.yaml — config/broker.yaml is
# gitignored and must NOT be scanned (it may not even exist in a fresh clone).
_TARGETS = (
    _REPO_ROOT / "src" / "bastion" / "thrashing.py",
    _REPO_ROOT / "src" / "bastion" / "gpu_profiles.py",
    _REPO_ROOT / "config" / "broker.example.yaml",
)

# Crash-numeric provenance attributed to the RTX 5090. These are the exact
# shapes scrubbed by F3/D1; they must not reappear in shipped surfaces.
_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"crash\s+investigation", re.IGNORECASE),
    re.compile(r"crash\s+zone", re.IGNORECASE),
    re.compile(r">\s*8\s*swaps?\s*/?\s*min", re.IGNORECASE),
    re.compile(r"\b55\s*[-–]\s*60\s*swaps", re.IGNORECASE),
    re.compile(r"\b8\s*swaps?\s*/\s*min", re.IGNORECASE),
)


@pytest.mark.parametrize("target", _TARGETS, ids=lambda p: p.name)
def test_no_5090_crash_numeric_provenance(target: Path) -> None:
    """No tracked tuning surface carries RTX-5090 crash-numeric provenance."""
    assert target.exists(), f"expected tracked file missing: {target}"
    text = target.read_text(encoding="utf-8")
    hits = [p.pattern for p in _FORBIDDEN_PATTERNS if p.search(text)]
    assert not hits, f"{target.name} carries scrubbed crash provenance: {hits}"


def test_broker_yaml_not_scanned_via_gitignored_path() -> None:
    """Guard: we scan the tracked template, never the gitignored live config."""
    names = {t.name for t in _TARGETS}
    assert "broker.example.yaml" in names
    assert "broker.yaml" not in names


def test_portable_floor_language_present() -> None:
    """Portable-floor framing (calibrate via --stress-test) survives the scrub."""
    corpus = "\n".join(
        t.read_text(encoding="utf-8") for t in _TARGETS if t.exists()
    )
    assert "--stress-test" in corpus, "missing --stress-test calibration pointer"
    assert re.search(r"calibrat", corpus, re.IGNORECASE), (
        "missing portable-floor 'calibrate' language"
    )


def test_thrashing_docstring_documents_division_of_labor() -> None:
    """thrashing.py docstring frames the detector as the per-agent admission scope."""
    text = (_REPO_ROOT / "src" / "bastion" / "thrashing.py").read_text(
        encoding="utf-8"
    )
    head = text[: text.index('"""', 3) + 3].lower()
    assert "per-agent" in head
    assert "system-wide" in head
    assert "brake" in head, "docstring must point at the swap-brake backstop"
