"""Textual panel tests for ``bastion.dashboard.panels_gpu``.

Covers GPUPanel, ModelsPanel, and VRAMLedgerPanel. Each panel is mounted
inside a minimal Pilot harness, fed deterministic fixture payloads, and the
returned Rich ``Table`` is inspected for expected structure. The tests pin
behavior around:

* empty / partial / typical payloads (no crashes)
* cold vs hot vs unavailable GPU rendering branches
* models with ``:`` and ``.`` in their names (the same gotcha that previously
  broke the ModelSelectModal — Models panel must not embed names in widget
  ids)
* VRAM ledger empty, near-budget, full reservation list rendering
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_gpu import GPUPanel, ModelsPanel, VRAMLedgerPanel
from bastion.dashboard.widgets import BastionPanel

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Harness(App[None]):
    """Minimal Textual app that mounts exactly one BastionPanel.

    Tests can read ``app.panel`` after ``run_test`` enters the event loop.
    """

    def __init__(self, panel: BastionPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _gpu_safe_payload() -> dict[str, Any]:
    """Cold GPU well below thermal ceiling."""
    return {
        "gpu": {
            "temperature_c": 55,
            "vram_used_mb": 8000,
            "vram_free_mb": 24000,
            "vram_total_mb": 32000,
            "power_draw_watts": 180.0,
            "is_safe": True,
        },
        "max_temperature_c": 85,
    }


def _gpu_hot_payload() -> dict[str, Any]:
    """Hot GPU at the ceiling — exercises red bold styling."""
    return {
        "gpu": {
            "temperature_c": 90,
            "vram_used_mb": 28000,
            "vram_free_mb": 4000,
            "vram_total_mb": 32000,
            "power_draw_watts": 500.0,
            "is_safe": False,
        },
        "max_temperature_c": 82,
    }


def _gpu_unavailable_payload() -> dict[str, Any]:
    """nvidia-smi unavailable — every field None."""
    return {"gpu": {}}


def _ledger_full() -> dict[str, Any]:
    """A near-budget ledger with both committed and pending reservations."""
    gb = 1024 * 1024 * 1024
    return {
        "total_bytes": 24 * gb,
        "safety_margin_bytes": 2 * gb,
        "allocated_bytes": 8 * gb,
        "reserved_bytes": 4 * gb,
        "available_bytes": 10 * gb,
        "active_reservations": 2,
        "reservations": [
            {"model": "qwen3:14b", "vram_bytes": 9 * gb, "age_seconds": 12.5,
             "committed": True},
            {"model": "granite4.1:8b", "vram_bytes": 4 * gb, "age_seconds": 2.0,
             "committed": False},
        ],
    }


# ---------------------------------------------------------------------------
# GPUPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_panel_empty_data_does_not_raise() -> None:
    """GPUPanel.render_data({}) must not raise on missing keys."""
    panel = GPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({})
        assert isinstance(tbl, Table)
        # Even with no data, the header rows for Temp/VRAM/Usage/Power/Safety
        # must be present (some show "n/a").
        assert tbl.row_count >= 4


@pytest.mark.asyncio
async def test_gpu_panel_cold_renders_green_branch() -> None:
    """Cold GPU (<60C) exercises the green ``temp_color`` branch."""
    panel = GPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            _gpu_safe_payload(),
            power_history=[100.0, 120.0, 180.0],
            vram_total_mb=32000,
            gpu_ceiling_c=85,
        )
        assert isinstance(tbl, Table)
        # Temp + VRAM + Usage + Power + Safety + Power-sparkline row
        assert tbl.row_count >= 5


@pytest.mark.asyncio
async def test_gpu_panel_hot_renders_red_branch() -> None:
    """Hot GPU (>=82C) exercises the red bold ``temp_color`` branch."""
    panel = GPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            _gpu_hot_payload(),
            power_history=[500.0, 510.0, 520.0],
            vram_total_mb=32000,
            gpu_ceiling_c=82,
        )
        assert isinstance(tbl, Table)
        # Should contain a Safety row marked UNSAFE.
        rendered = tbl.columns[1]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in rendered)
        assert "UNSAFE" in joined


@pytest.mark.asyncio
async def test_gpu_panel_zero_power_renders() -> None:
    """power_draw_watts == 0 must render as ``0W`` not ``n/a``."""
    panel = GPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        payload = _gpu_safe_payload()
        payload["gpu"]["power_draw_watts"] = 0
        tbl = app.panel.render_data(payload)
        cells = tbl.columns[1]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "0W" in joined


@pytest.mark.asyncio
async def test_gpu_panel_nvidia_smi_unavailable() -> None:
    """All-None GPU status renders gracefully with ``n/a`` placeholders."""
    panel = GPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_gpu_unavailable_payload())
        cells = tbl.columns[1]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "n/a" in joined


@pytest.mark.asyncio
async def test_gpu_panel_with_app_history_sparklines() -> None:
    """When the running App exposes vram_history and temp_history, the panel
    adds two sparkline rows.
    """

    class _HistApp(App[None]):
        def __init__(self, panel: BastionPanel) -> None:
            super().__init__()
            self.panel = panel
            self.vram_history = [4000.0, 6000.0, 8000.0, 12000.0]
            self.temp_history = [40, 45, 55, 60]

        def compose(self) -> ComposeResult:
            yield self.panel

    panel = GPUPanel()
    app = _HistApp(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            _gpu_safe_payload(),
            vram_total_mb=32000,
            gpu_ceiling_c=85,
        )
        # Without history rows we expect ~5 rows; with both vram+temp +6.
        assert tbl.row_count >= 7


# ---------------------------------------------------------------------------
# ModelsPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_models_panel_empty_list_shows_none_row() -> None:
    """Empty loaded list must render a single ``(none)`` placeholder row."""
    panel = ModelsPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({"loaded_models": [], "current_model": None})
        # one row: "(none)"
        assert tbl.row_count == 1


@pytest.mark.asyncio
async def test_models_panel_typical_payload() -> None:
    """Happy path: three loaded models, one is current — marked with ``*``."""
    panel = ModelsPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        data = {
            "loaded_models": [
                {"name": "qwen3:14b", "vram_gb": 9.3},
                {"name": "llama3.1:8b", "vram_gb": 4.4},
                {"name": "nomic-embed-text:latest", "vram_gb": 0.4},
            ],
            "current_model": "qwen3:14b",
        }
        tbl = app.panel.render_data(data)
        # 3 model rows + spacer + Active row
        assert tbl.row_count == 5
        cells = tbl.columns[0]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "Active" in joined
        assert "qwen3:14b" in joined


@pytest.mark.asyncio
async def test_models_panel_handles_colon_and_dot_names() -> None:
    """granite4.1:8b style names must render without raising — the panel
    must NOT embed names in widget ids (the same bug class fixed in the
    ModelSelect modal).
    """
    panel = ModelsPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        data = {
            "loaded_models": [
                {"name": "granite4.1:8b", "vram_gb": 5.6},
                {"name": "qwen2.5-coder:32b", "vram_gb": 21.0},
            ],
            "current_model": "granite4.1:8b",
        }
        tbl = app.panel.render_data(data)
        cells = tbl.columns[0]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "granite4.1:8b" in joined
        assert "qwen2.5-coder:32b" in joined


@pytest.mark.asyncio
async def test_models_panel_missing_optional_fields() -> None:
    """Missing ``vram_gb`` / ``current_model`` must not raise."""
    panel = ModelsPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        data = {"loaded_models": [{"name": "x:y"}]}
        tbl = app.panel.render_data(data)
        # 1 row only (no current_model means no Active line)
        assert tbl.row_count == 1


# ---------------------------------------------------------------------------
# VRAMLedgerPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vram_ledger_panel_empty_renders_no_data() -> None:
    """Empty ledger renders single ``(no data)`` row, no exception."""
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({})
        assert tbl.row_count == 1


@pytest.mark.asyncio
async def test_vram_ledger_panel_full_payload() -> None:
    """Full payload with reservations renders Total / Safety / Allocated /
    Reserved / Available / Reserv# / Usage rows plus per-reservation lines."""
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_ledger_full())
        # 6 metric rows + Usage bar row + 2 reservation rows = 9
        assert tbl.row_count == 9
        cells_left = tbl.columns[0]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells_left)
        assert "Total" in joined
        assert "Usage" in joined
        assert "granite4.1" in joined or "granite4" in joined


@pytest.mark.asyncio
async def test_vram_ledger_panel_no_reservations() -> None:
    """A ledger with zero reservations still renders the bar."""
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        gb = 1024 * 1024 * 1024
        ledger = {
            "total_bytes": 24 * gb,
            "safety_margin_bytes": 2 * gb,
            "allocated_bytes": 0,
            "reserved_bytes": 0,
            "available_bytes": 22 * gb,
            "active_reservations": 0,
            "reservations": [],
        }
        tbl = app.panel.render_data(ledger)
        # 6 stat rows + usage bar; no reservation lines
        assert tbl.row_count == 7


@pytest.mark.asyncio
async def test_vram_ledger_panel_near_budget() -> None:
    """Near-budget ledger (>90% used) drives the red-bold bar branch."""
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        gb = 1024 * 1024 * 1024
        ledger = {
            "total_bytes": 24 * gb,
            "safety_margin_bytes": 2 * gb,
            "allocated_bytes": 18 * gb,
            "reserved_bytes": 4 * gb,
            "available_bytes": 0,
            "active_reservations": 1,
            "reservations": [
                {"model": "qwen3:14b", "vram_bytes": 18 * gb,
                 "age_seconds": 30.0, "committed": True},
            ],
        }
        tbl = app.panel.render_data(ledger)
        # Should render usage bar; assert "Usage" appears.
        cells = tbl.columns[0]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "Usage" in joined


@pytest.mark.asyncio
async def test_vram_ledger_truncates_to_five_reservations() -> None:
    """Only the first five reservations are rendered."""
    panel = VRAMLedgerPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        gb = 1024 * 1024 * 1024
        reservations = [
            {"model": f"m{i}:x", "vram_bytes": gb, "age_seconds": i * 1.0,
             "committed": (i % 2 == 0)}
            for i in range(8)
        ]
        ledger = {
            "total_bytes": 24 * gb,
            "safety_margin_bytes": 2 * gb,
            "allocated_bytes": 8 * gb,
            "reserved_bytes": 0,
            "available_bytes": 14 * gb,
            "active_reservations": 8,
            "reservations": reservations,
        }
        tbl = app.panel.render_data(ledger)
        # 6 stat + 1 usage + max 5 reservations = 12.
        assert tbl.row_count == 12
