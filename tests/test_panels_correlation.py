"""Textual panel tests for the new ``CorrelationPanel`` (observability T5).

``CorrelationPanel`` is a secondary-group panel living in the new file
``bastion.dashboard.panels_correlation``.  Per ADR-005 it is a direct-accessor
panel: ``render_data()`` takes a **plain dict** — the ``correlation`` leg of the
``MachineSnapshot`` ``model_dump()`` (``CorrelationState`` shape, spec 4.7) — and
returns a Rich ``Table``.  It surfaces the RiskIndex (score + dominant factor),
thermal coupling (active + headroom), recent contention events, the enriched
stall reason, and a tail of the correlation ring.  It must tolerate:

  - ``None`` / missing payload (no-data row, no crash);
  - an empty CorrelationState (all None / empty lists — renders cleanly, no
    misleading 0);
  - a fully-populated state.
"""

from __future__ import annotations

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_correlation import CorrelationPanel
from bastion.dashboard.widgets import BastionPanel


class _Harness(App[None]):
    def __init__(self, panel: BastionPanel) -> None:
        super().__init__()
        self.panel = panel

    def compose(self) -> ComposeResult:
        yield self.panel


_FULL = {
    "risk_index": {
        "score": 0.73,
        "level": "high",
        "component_scores": {"vram_headroom": 0.8, "thermal_headroom": 0.4},
        "dominant_factor": "vram_headroom",
    },
    "thermal_coupling": {
        "cpu_temp_c": 72.0,
        "gpu_temp_c": 68.0,
        "fan_speed_pct": 55,
        "coupling_active": True,
        "thermal_headroom_min_c": 13.0,
    },
    "recent_contentions": [
        {
            "ts_monotonic": 1234.5,
            "ts_wall": 1700000000.0,
            "domain": "system",
            "kind": "nvme_burst",
            "payload": {"write_rate_mb_s": 240.0},
            "attribution": "nvme0n1 write 240MB/s",
            "inference_was_stalled": True,
            "stall_reason_at_time": "swap_cooldown",
        }
    ],
    "enriched_stall_reason": "swap_cooldown [mem-PSI some=18.3, nvme0n1 94% util]",
    "ring_size": 7,
    "recent_ring_events": [
        {
            "ts_monotonic": 1234.0,
            "ts_wall": 1700000000.0,
            "domain": "gpu",
            "kind": "throttle",
            "payload": {"reason": "sw_thermal_slowdown"},
        },
        {
            "ts_monotonic": 1235.0,
            "ts_wall": 1700000001.0,
            "domain": "inference",
            "kind": "request_complete",
            "payload": {"model": "llama3", "decode_tps": 42.0},
        },
    ],
}


def _render(panel: CorrelationPanel, data: object) -> Table:
    out = panel.render_data(data)
    assert isinstance(out, Table)
    return out


class TestCorrelationPanelRendersFromDict:
    @pytest.mark.asyncio
    async def test_full_payload_renders(self) -> None:
        panel = CorrelationPanel(id="correlation")
        async with _Harness(panel).run_test():
            table = _render(panel, _FULL)
            assert table.row_count > 0

    @pytest.mark.asyncio
    async def test_none_payload_no_crash(self) -> None:
        panel = CorrelationPanel(id="correlation")
        async with _Harness(panel).run_test():
            table = _render(panel, None)
            assert table.row_count >= 1  # a (no data) marker

    @pytest.mark.asyncio
    async def test_empty_state_renders_cleanly(self) -> None:
        panel = CorrelationPanel(id="correlation")
        empty = {
            "risk_index": None,
            "thermal_coupling": None,
            "recent_contentions": [],
            "enriched_stall_reason": None,
            "ring_size": 0,
            "recent_ring_events": [],
        }
        async with _Harness(panel).run_test():
            table = _render(panel, empty)
            # Never an empty box: at least a no-data / zero-ring marker row.
            assert table.row_count >= 1

    @pytest.mark.asyncio
    async def test_partial_state_risk_only(self) -> None:
        # Thermal/contention absent (no GPU / no stall) — must not crash and
        # must still show the risk row.
        panel = CorrelationPanel(id="correlation")
        partial = {
            "risk_index": {
                "score": 0.1,
                "level": "nominal",
                "component_scores": {},
                "dominant_factor": "vram_headroom",
            },
            "thermal_coupling": None,
            "recent_contentions": [],
            "enriched_stall_reason": None,
            "ring_size": 0,
            "recent_ring_events": [],
        }
        async with _Harness(panel).run_test():
            table = _render(panel, partial)
            assert table.row_count > 0

    def test_is_bastion_panel_subclass(self) -> None:
        assert issubclass(CorrelationPanel, BastionPanel)
