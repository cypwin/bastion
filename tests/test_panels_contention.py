"""Textual panel tests for the new ``ContentionPanel`` (observability T6).

``ContentionPanel`` is a host/system-level panel (peer to MemoryPanel /
NetworkPanel, spec 5.3 panel-file assignment) living in
``bastion.dashboard.panels_system``.  Per ADR-005 it is a direct-accessor
panel: ``render_data()`` takes a **plain dict** (the ``ContentionSnapshot``
``model_dump()`` shape served by ``/broker/contention``) and returns a Rich
``Table``.  It must tolerate:

  - ``None`` / missing payload (no data row, no crash);
  - all-``None`` legs (PSI/swap/RAPL/OOM absent — the no-PSI container case);
  - a fully-populated payload with several block devices.
"""

from __future__ import annotations

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_system import ContentionPanel
from bastion.dashboard.widgets import BastionPanel


class _Harness(App[None]):
    """Minimal Textual app that mounts exactly one BastionPanel."""

    def __init__(self, panel: BastionPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


_FULL_PAYLOAD = {
    "psi_cpu_some_avg10": 12.5,
    "psi_cpu_full_avg10": 1.0,
    "psi_mem_some_avg10": 0.0,
    "psi_mem_full_avg10": 0.0,
    "psi_io_some_avg10": 30.0,
    "psi_io_full_avg10": 26.0,  # above default crit (25.0) -> red branch
    "swap_in_rate_mb_s": 0.0,
    "swap_out_rate_mb_s": 6.0,  # above 5 MB/s -> red branch
    "block_devices": [
        {
            "device": "nvme0n1",
            "util_pct": 85.0,  # > 80 -> red
            "read_await_ms": 1.2,
            "write_await_ms": 4.4,
            "read_rate_mb_s": 12.0,
            "write_rate_mb_s": 210.0,
        },
        {
            "device": "sda",
            "util_pct": 12.0,
            "read_await_ms": None,
            "write_await_ms": None,
            "read_rate_mb_s": 0.0,
            "write_rate_mb_s": 0.0,
        },
    ],
    "cpu_package_watts": 95.5,
    "gpu_board_watts": None,
    "oom_kill_total": 3,
    "oom_kill_rate": 1.0,  # > 0 -> red OOM row
    "sampled_at": 1234567890.0,
}


@pytest.mark.asyncio
async def test_contention_panel_no_data() -> None:
    """``None`` payload renders a single no-data row, never crashes."""
    panel = ContentionPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(None)
        assert isinstance(tbl, Table)
        assert tbl.row_count >= 1


@pytest.mark.asyncio
async def test_contention_panel_all_none_legs() -> None:
    """An all-None ContentionSnapshot (no PSI/swap/RAPL/OOM) renders cleanly.

    This is the no-PSI container / no-powercap host — a TESTED DEFAULT: every
    leg is None and the panel must not emit a misleading 0 row or crash.
    """
    panel = ContentionPanel()
    app = _Harness(panel)
    payload = {
        "psi_cpu_some_avg10": None,
        "psi_cpu_full_avg10": None,
        "psi_mem_some_avg10": None,
        "psi_mem_full_avg10": None,
        "psi_io_some_avg10": None,
        "psi_io_full_avg10": None,
        "swap_in_rate_mb_s": None,
        "swap_out_rate_mb_s": None,
        "block_devices": [],
        "cpu_package_watts": None,
        "gpu_board_watts": None,
        "oom_kill_total": None,
        "oom_kill_rate": None,
        "sampled_at": 1.0,
    }
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(payload)
        assert isinstance(tbl, Table)
        # No exception; a placeholder "no data" row is acceptable.
        assert tbl.row_count >= 1


@pytest.mark.asyncio
async def test_contention_panel_full_payload() -> None:
    """A fully-populated payload renders PSI, swap, devices, power and OOM."""
    panel = ContentionPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_FULL_PAYLOAD)
        assert isinstance(tbl, Table)
        # At least: a few PSI rows + 2 device rows + swap + power + OOM.
        assert tbl.row_count >= 5


@pytest.mark.asyncio
async def test_contention_panel_partial_payload_no_crash() -> None:
    """Missing keys (a partial dict) must not raise — dict-accessor contract."""
    panel = ContentionPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Only a couple of keys present; everything else absent.
        tbl = app.panel.render_data({"psi_io_some_avg10": 5.0})
        assert isinstance(tbl, Table)
        assert tbl.row_count >= 1
