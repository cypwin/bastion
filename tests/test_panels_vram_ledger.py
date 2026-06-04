"""VRAMLedgerPanel: Measured + Δ rows and margin-excluded Usage bar (2026-06).

Mirrors the mounted-harness style of test_panels_gpu.py. The panel gains a
``measured_used_mb`` parameter (nvidia-smi physical VRAM) and renders a
``Measured`` row plus a ``Δ overhead`` row (= measured − allocated − reserved).
When the caller passes nothing, the panel falls back to ``app.vram_history``.
The Usage bar now excludes the safety margin from "used".
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from bastion.dashboard.panels_gpu import VRAMLedgerPanel
from bastion.dashboard.widgets import BastionPanel

GB = 1024 * 1024 * 1024


class _Harness(App[None]):
    def __init__(self, panel: BastionPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


class _HarnessWithHistory(_Harness):
    def __init__(self, panel: BastionPanel, history: list[float]) -> None:
        super().__init__(panel)
        self.vram_history = history


def _ledger() -> dict:
    return {
        "total_bytes": 32 * GB,
        "safety_margin_bytes": int(3.2 * GB),
        "allocated_bytes": 10 * GB,
        "reserved_bytes": 2 * GB,
        "available_bytes": int(16.8 * GB),
        "active_reservations": 0,
        "reservations": [],
    }


def _all_text(tbl) -> str:
    parts: list[str] = []
    for col in tbl.columns:
        parts.extend(str(c) for c in col._cells)  # type: ignore[attr-defined]
    return " ".join(parts)


@pytest.mark.asyncio
async def test_measured_and_delta_rows_present() -> None:
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        # measured 18 GB; tracked = 10 + 2 = 12 GB; Δ = +6.0 GB
        tbl = app.panel.render_data(_ledger(), measured_used_mb=18 * 1024)
        text = _all_text(tbl)
        assert "Measured" in text
        assert "18.0GB" in text
        assert "Δ overhead" in text
        assert "+6.0GB" in text


@pytest.mark.asyncio
async def test_rows_absent_without_measured() -> None:
    panel = VRAMLedgerPanel()
    app = _Harness(panel)  # no vram_history on this app
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_ledger(), measured_used_mb=None)
        text = _all_text(tbl)
        assert "Measured" not in text
        assert "Δ overhead" not in text


@pytest.mark.asyncio
async def test_usage_excludes_safety_margin() -> None:
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_ledger(), measured_used_mb=None)
        text = _all_text(tbl)
        # used = allocated + reserved = 12/32 = 38% (NOT 47% with the margin)
        assert "38%" in text
        assert "47%" not in text


@pytest.mark.asyncio
async def test_falls_back_to_app_vram_history() -> None:
    panel = VRAMLedgerPanel()
    app = _HarnessWithHistory(panel, history=[18 * 1024])  # 18 GB in MB
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_ledger())  # measured_used_mb omitted
        text = _all_text(tbl)
        assert "Measured" in text
        assert "18.0GB" in text
        assert "+6.0GB" in text


@pytest.mark.asyncio
async def test_negative_delta_renders_signed() -> None:
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        # measured 10 GB < tracked 12 GB → Δ = -2.0 GB (estimates running high)
        tbl = app.panel.render_data(_ledger(), measured_used_mb=10 * 1024)
        text = _all_text(tbl)
        assert "-2.0GB" in text
