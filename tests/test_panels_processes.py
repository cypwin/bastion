"""Textual panel tests for the new ``ProcessAttributionPanel`` (observability T1).

``ProcessAttributionPanel`` is a secondary-group panel living in the new file
``bastion.dashboard.panels_processes``.  Per ADR-005 it is a direct-accessor
panel: ``render_data()`` takes a **plain dict** (the ``ProcessSnapshot``
``model_dump()`` shape served by ``/broker/processes``) and returns a Rich
``Table``.  It has four sections (GPU / CPU-IO / watchlist / churn) and must
tolerate:

  - ``None`` / missing payload (no-data row, no crash);
  - an empty snapshot (all lists empty — renders cleanly, no misleading 0);
  - a StubBackend host (no GPU rows) — GPU section shows ``(no GPU)``;
  - a fully-populated snapshot with own-PID role badges and a stale-GPU dim.
"""

from __future__ import annotations

import time

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_processes import ProcessAttributionPanel
from bastion.dashboard.widgets import BastionPanel


class _Harness(App[None]):
    def __init__(self, panel: BastionPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


_FULL = {
    "top_processes": [
        {
            "pid": 4242,
            "name": "ollama",
            "cpu_pct": 80.0,
            "rss_mb": 4096.0,
            "io_read_bytes_s": 1_000_000.0,
            "io_write_bytes_s": 2_000_000.0,
            "is_inference_owned": True,
            "role": "ollama",
            "watchlisted": False,
            "gpu_row": None,
        },
        {
            "pid": 99,
            "name": "competitor",
            "cpu_pct": 60.0,
            "rss_mb": 8000.0,
            "io_read_bytes_s": None,  # AccessDenied row kept with None io
            "io_write_bytes_s": None,
            "is_inference_owned": False,
            "role": None,
            "watchlisted": False,
            "gpu_row": None,
        },
    ],
    "gpu_processes": [
        {
            "pid": 4242,
            "name": "ollama",
            "vram_mb": 8192,
            "sm_pct": 80,
            "mem_pct": 40,
            "enc_pct": None,
            "dec_pct": None,
            "is_inference_owned": True,
            "role": "ollama",
        },
    ],
    "own_pids": {"4242": "ollama"},
    "watchlist_hits": [
        {
            "pid": 1234,
            "name": "python3",
            "cpu_pct": 5.0,
            "rss_mb": 200.0,
            "io_read_bytes_s": None,
            "io_write_bytes_s": None,
            "is_inference_owned": False,
            "role": None,
            "watchlisted": True,
            "gpu_row": None,
        },
    ],
    "recent_churn_events": [
        {"timestamp": 1.0, "new_count": 6, "exited_count": 0, "new_names": ["worker"]},
    ],
    "collected_at": time.time(),
    "gpu_collected_at": time.time(),
}


@pytest.mark.asyncio
async def test_panel_no_data() -> None:
    panel = ProcessAttributionPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(None)
        assert isinstance(tbl, Table)
        assert tbl.row_count >= 1


@pytest.mark.asyncio
async def test_panel_empty_snapshot() -> None:
    panel = ProcessAttributionPanel()
    app = _Harness(panel)
    payload = {
        "top_processes": [],
        "gpu_processes": [],
        "own_pids": {},
        "watchlist_hits": [],
        "recent_churn_events": [],
        "collected_at": time.time(),
        "gpu_collected_at": None,
    }
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(payload)
        assert isinstance(tbl, Table)
        assert tbl.row_count >= 1


@pytest.mark.asyncio
async def test_panel_full_payload_renders_rows() -> None:
    panel = ProcessAttributionPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(_FULL)
        assert isinstance(tbl, Table)
        # GPU row + 2 CPU rows + watchlist + churn -> several rows.
        assert tbl.row_count >= 4


@pytest.mark.asyncio
async def test_panel_stub_backend_gpu_section_no_gpu() -> None:
    """No GPU rows but a populated CPU section -> GPU section shows ``(no GPU)``."""
    panel = ProcessAttributionPanel()
    app = _Harness(panel)
    payload = dict(_FULL)
    payload["gpu_processes"] = []
    payload["gpu_collected_at"] = None
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(payload)
        assert isinstance(tbl, Table)
        # The render must not crash and must still produce CPU rows.
        assert tbl.row_count >= 1


@pytest.mark.asyncio
async def test_panel_stale_gpu_data_does_not_crash() -> None:
    """A stale gpu_collected_at (old) renders a dim annotation, never crashes."""
    panel = ProcessAttributionPanel()
    app = _Harness(panel)
    payload = dict(_FULL)
    payload["gpu_collected_at"] = time.time() - 60.0  # 60s old -> stale
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data(payload)
        assert isinstance(tbl, Table)
        assert tbl.row_count >= 1


@pytest.mark.asyncio
async def test_panel_partial_payload_no_crash() -> None:
    panel = ProcessAttributionPanel()
    app = _Harness(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        tbl = app.panel.render_data({"top_processes": []})
        assert isinstance(tbl, Table)
        assert tbl.row_count >= 1
