"""Root conftest — suite-wide determinism shims.

Added 2026-06 alongside the nvidia-smi admission-gate backstop in
``VRAMManager.reserve()`` (see docs/superpowers/specs/2026-06-04-vram-ledger-honesty-design.md).

The backstop calls ``bastion.vram.get_vram_free_gb()`` (real nvidia-smi) on
every reservation. Without neutralization, every pre-existing test that drives
``reserve()`` — directly (test_vram_manager, test_vram_state_unknown_extra) or
via the scheduler swap path (test_scheduler) — would suddenly depend on the
*live host GPU's* free VRAM and flake whenever the card is busy.

This autouse fixture forces the backstop fail-open (``get_vram_free_gb -> None``)
for the whole suite EXCEPT:

  * ``test_vram.py`` — the can_load_model hardware-gate tests, which exercise the
    real gate via patched ``query_gpu_status`` and must see it evaluate.
  * ``test_vram_backstop_reconcile.py`` — the new backstop tests, which patch
    ``get_vram_free_gb`` per-test to specific values.

Tests that patch ``bastion.vram.get_vram_free_gb`` themselves (e.g. the
convergence tests) still override this default within their own ``with`` block.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

_BACKSTOP_SELF_MANAGED = ("test_vram.py", "test_vram_backstop_reconcile.py")


@pytest.fixture(autouse=True)
def _neutralize_vram_backstop(request, monkeypatch):
    nodeid = request.node.nodeid
    if any(name in nodeid for name in _BACKSTOP_SELF_MANAGED):
        yield
        return
    monkeypatch.setattr("bastion.vram.get_vram_free_gb", AsyncMock(return_value=None))
    yield
