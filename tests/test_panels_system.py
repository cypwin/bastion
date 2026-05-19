"""Textual panel tests for ``bastion.dashboard.panels_system``.

Covers TemperaturePanel, MemoryPanel, CPUPanel, and NetworkPanel. Mounts
each panel inside a minimal Pilot harness, exercises render_data() against
deterministic fixture payloads (CPU 1/8/32 cores, swap engaged/idle,
multiple thermal zones, zero-history networking), and inspects the
returned Rich ``Table`` for expected structure.

Pins behaviour around:
* empty / partial / typical payloads (no crashes)
* small (1-core) vs medium (8-core) vs large (32-core) sparkline rendering
* memory low/high pressure + swap activation
* network zero-rate vs steady-rate
* multi-zone thermals with optional GPU temperature
"""

from __future__ import annotations

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_system import (
    CPUPanel,
    MemoryPanel,
    NetworkPanel,
    TemperaturePanel,
)
from bastion.dashboard.widgets import BastionPanel

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Harness(App[None]):
    """Minimal Textual app that mounts exactly one BastionPanel."""

    def __init__(self, panel: BastionPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


# ---------------------------------------------------------------------------
# TemperaturePanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_temperature_panel_no_sensors() -> None:
    """With no readings, renders a single ``(no sensors)`` row."""
    panel = TemperaturePanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data()
        assert isinstance(tbl, Table)
        assert tbl.row_count == 1


@pytest.mark.asyncio
async def test_temperature_panel_all_sensors_safe() -> None:
    """CPU + 2 NVMe + GPU all under warn threshold — green branch."""
    panel = TemperaturePanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            cpu_temp=45,
            nvme_temps=[40, 50],
            gpu_temp=55,
            gpu_ceiling_c=85,
        )
        # CPU + 2 NVMe + GPU = 4 rows
        assert tbl.row_count == 4


@pytest.mark.asyncio
async def test_temperature_panel_all_sensors_hot() -> None:
    """CPU >=90, NVMe >=75, GPU at ceiling — all red bold branch."""
    panel = TemperaturePanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            cpu_temp=95,
            nvme_temps=[80],
            gpu_temp=85,
            gpu_ceiling_c=85,
        )
        # 3 rows; single NVMe label is just "NVMe"
        assert tbl.row_count == 3


@pytest.mark.asyncio
async def test_temperature_panel_warn_thresholds() -> None:
    """Mid-range values exercise the yellow ``?`` branch on every sensor."""
    panel = TemperaturePanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            cpu_temp=80,
            nvme_temps=[65],
            gpu_temp=80,  # warn = ceiling - 8 = 77
            gpu_ceiling_c=85,
        )
        assert tbl.row_count == 3


@pytest.mark.asyncio
async def test_temperature_panel_only_gpu() -> None:
    """Just GPU reading present — only one row rendered."""
    panel = TemperaturePanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(gpu_temp=70, gpu_ceiling_c=85)
        assert tbl.row_count == 1


@pytest.mark.asyncio
async def test_temperature_panel_only_cpu() -> None:
    """Just CPU reading present — only one row rendered."""
    panel = TemperaturePanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(cpu_temp=50)
        assert tbl.row_count == 1


# ---------------------------------------------------------------------------
# MemoryPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_panel_none_returns_no_data() -> None:
    """``mem=None`` renders a single ``no data`` row."""
    panel = MemoryPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(None)
        assert tbl.row_count == 1


@pytest.mark.asyncio
async def test_memory_panel_low_pressure() -> None:
    """10% used — green color branch, no swap row."""
    panel = MemoryPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "used_gb": 3.2,
            "total_gb": 32.0,
            "available_gb": 28.8,
            "swap_gb": 0.0,
            "swap_total_gb": 8.0,
        })
        # RAM + Available rows only
        assert tbl.row_count == 2


@pytest.mark.asyncio
async def test_memory_panel_high_pressure_with_swap() -> None:
    """90% used + swap engaged — red bold branch + Swap row added."""
    panel = MemoryPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "used_gb": 28.8,
            "total_gb": 32.0,
            "available_gb": 3.2,
            "swap_gb": 2.5,
            "swap_total_gb": 8.0,
        })
        # RAM + Available + Swap
        assert tbl.row_count == 3
        cells = tbl.columns[0]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "Swap" in joined


@pytest.mark.asyncio
async def test_memory_panel_zero_total_safe() -> None:
    """total_gb=0 must not trigger ZeroDivisionError."""
    panel = MemoryPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "used_gb": 0.0,
            "total_gb": 0.0,
            "available_gb": 0.0,
            "swap_gb": 0.0,
        })
        assert tbl.row_count == 2


@pytest.mark.asyncio
async def test_memory_panel_swap_zero_total() -> None:
    """Swap row with zero swap_total_gb stays safe (no ZeroDivisionError)."""
    panel = MemoryPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "used_gb": 16.0,
            "total_gb": 32.0,
            "available_gb": 16.0,
            "swap_gb": 1.0,
            "swap_total_gb": 0.0,
        })
        assert tbl.row_count == 3


# ---------------------------------------------------------------------------
# CPUPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cpu_panel_minimal_payload() -> None:
    """Only ``overall_pct`` present — single CPU row, no extras."""
    panel = CPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({"overall_pct": 12.0})
        # Just the CPU row
        assert tbl.row_count == 1


@pytest.mark.asyncio
async def test_cpu_panel_one_core() -> None:
    """Single-core box: per_core list of length 1 still renders Cores row."""
    panel = CPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "overall_pct": 22.0,
            "per_core": [22.0],
            "load_avg": (0.3, 0.4, 0.5),
            "freq_mhz": 3500.0,
        })
        # CPU + Load + Freq + Cores
        assert tbl.row_count == 4


@pytest.mark.asyncio
async def test_cpu_panel_eight_cores_with_history() -> None:
    """8-core typical desktop, with CPU history sparkline."""
    panel = CPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            {
                "overall_pct": 65.0,
                "per_core": [50.0, 75.0, 30.0, 90.0, 60.0, 20.0, 45.0, 85.0],
                "load_avg": (1.5, 1.2, 1.0),
                "freq_mhz": 4200.0,
            },
            cpu_history=[40.0, 50.0, 55.0, 60.0, 65.0],
        )
        # CPU + Load + Freq + Cores = 4
        assert tbl.row_count == 4


@pytest.mark.asyncio
async def test_cpu_panel_thirty_two_cores_with_processes() -> None:
    """32-core box with top-process list."""
    panel = CPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            {
                "overall_pct": 92.0,
                "per_core": [90.0 + (i % 10) for i in range(32)],
                "load_avg": (28.0, 20.0, 12.0),
                "freq_mhz": 5000.0,
            },
            cpu_history=[80.0, 85.0, 90.0, 92.0],
            processes=[
                {"name": "ollama", "cpu_pct": 88.5, "mem_mb": 12000.0},
                {"name": "python", "cpu_pct": 12.0, "mem_mb": 800.0},
            ],
        )
        # CPU + Load + Freq + Cores + spacer + 2 procs = 7
        assert tbl.row_count == 7


@pytest.mark.asyncio
async def test_cpu_panel_truncates_processes_to_six() -> None:
    """``processes`` list is truncated to the first six."""
    panel = CPUPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        procs = [
            {"name": f"p{i}", "cpu_pct": 1.0 * i, "mem_mb": 100.0 * i}
            for i in range(10)
        ]
        tbl = app.panel.render_data(
            {"overall_pct": 1.0},
            processes=procs,
        )
        # CPU + spacer + 6 procs = 8
        assert tbl.row_count == 8


# ---------------------------------------------------------------------------
# NetworkPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_panel_zero_rate_no_history() -> None:
    """Fresh start: zero rates, no history => still produces 3 rows."""
    panel = NetworkPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({})
        # Down + Up + Total D/U
        assert tbl.row_count == 3
        cells = tbl.columns[1]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "B/s" in joined


@pytest.mark.asyncio
async def test_network_panel_steady_rate_with_history() -> None:
    """Steady-state rates + history sparklines."""
    panel = NetworkPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(
            {
                "recv_bytes_sec": 5_242_880.0,   # 5 MB/s
                "sent_bytes_sec": 1_048_576.0,   # 1 MB/s
                "total_recv_bytes": 50.0 * 1024**3,
                "total_sent_bytes": 10.0 * 1024**3,
            },
            recv_history=[1.0, 2.0, 3.0, 5.0],
            sent_history=[0.5, 0.7, 0.9, 1.0],
        )
        assert tbl.row_count == 3
        cells = tbl.columns[1]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "MB/s" in joined
        assert "50.00" in joined  # total recv GB


@pytest.mark.asyncio
async def test_network_panel_gigabit_burst() -> None:
    """Saturated 1+ GB/s link — exercises the GB/s branch of ``get_rate``."""
    panel = NetworkPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "recv_bytes_sec": 2.5 * 1024**3,
            "sent_bytes_sec": 1.2 * 1024**3,
            "total_recv_bytes": 0.0,
            "total_sent_bytes": 0.0,
        })
        cells = tbl.columns[1]._cells  # type: ignore[attr-defined]
        joined = " ".join(str(c) for c in cells)
        assert "GB/s" in joined


@pytest.mark.asyncio
async def test_network_panel_missing_optional_keys() -> None:
    """Render with only the rate fields, no totals."""
    panel = NetworkPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({
            "recv_bytes_sec": 100.0,
            "sent_bytes_sec": 50.0,
        })
        # Still 3 rows; Totals default to 0.
        assert tbl.row_count == 3
