"""Coverage tests for the six panels in ``bastion.dashboard.panels_broker``.

Each panel is exercised via ``render_data(...)`` to guard against the class of
bug that recently slipped review (the ``[u]`` BadIdentifier crash): a Textual
panel that *composes* but explodes on real payloads.

Two harnesses are used:

* Direct: ``Panel.__new__(Panel).render_data(...)`` — bypasses Textual entirely
  for panels whose ``render_data`` does not touch ``self.app``.  Matches the
  pattern already established in ``tests/test_dashboard.py``.
* Pilot:  a minimal ``_PanelHarness(App)`` mounts the queue panel so the
  ``self.app.queue_history`` branch can be exercised.

The tests pin behavior, not implementation: we verify a ``rich.table.Table``
comes back and contains at least one row.  Specific colors/styles are not
asserted (brittle to theme changes).
"""

from __future__ import annotations

from typing import Any

import pytest
from rich.table import Table
from textual.app import App, ComposeResult

from bastion.dashboard.panels_broker import (
    AlertPanel,
    CircuitBreakerPanel,
    QueuePanel,
    SchedulerPanel,
    ThrashingPanel,
    WatchdogPanel,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _make(panel_cls: type) -> Any:
    """Instantiate a panel without going through Textual's widget machinery.

    ``render_data`` is logic-only on every panel except QueuePanel's
    sparkline branch; for the rest, ``__new__`` is enough and avoids the
    cost of spinning up an App.
    """
    return panel_cls.__new__(panel_cls)


class _QueueHarness(App[None]):
    """Mounts a single QueuePanel so ``self.app.queue_history`` is reachable.

    QueuePanel.render_data reads ``self.app.queue_history`` — without an
    active Textual app context the ``self.app`` property raises rather than
    short-circuiting the hasattr guard.  Mounting one here gives every queue
    test a real app to bind to.
    """

    def __init__(self, queue_history: list[float] | None = None) -> None:
        super().__init__()
        self.queue_history: list[float] = queue_history or []

    def compose(self) -> ComposeResult:
        yield QueuePanel(id="queue")




# ---------------------------------------------------------------------------
# QueuePanel
# ---------------------------------------------------------------------------


class TestQueuePanel:
    """QueuePanel renders queue depth, state, stall diagnostics, sparklines.

    QueuePanel.render_data dereferences ``self.app.queue_history`` so every
    test mounts a real (headless) Textual app via the Pilot harness.
    """

    async def test_render_empty_data(self) -> None:
        """Empty status dict must not raise; renders the '(empty)' placeholder."""
        app = _QueueHarness()
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            table = panel.render_data({})
        assert isinstance(table, Table)
        assert table.row_count >= 1

    async def test_render_typical_payload(self) -> None:
        """A realistic /broker/status payload renders without raising."""
        app = _QueueHarness()
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            data: dict[str, Any] = {
                "queue_by_model": {"qwen3:14b": 3, "llama3.1:8b": 1},
                "queue_depth": 4,
                "state": "running",
            }
            table = panel.render_data(data)
        # 2 model rows, 1 spacer, Total row, State row -> 5 rows minimum.
        assert table.row_count >= 5

    async def test_render_with_queue_diag_inflight(self) -> None:
        """In-flight count from /broker/queue is surfaced."""
        app = _QueueHarness()
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            data = {"queue_by_model": {}, "queue_depth": 0, "state": "running"}
            diag = {"inflight_total": 2, "stall_reason": ""}
            table = panel.render_data(data, queue_diag=diag)
        assert isinstance(table, Table)

    async def test_render_with_stall_swap_cooldown(self) -> None:
        """A swap_cooldown stall reason is rendered with seconds remaining."""
        app = _QueueHarness()
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            data = {"queue_by_model": {"qwen3:14b": 1}, "queue_depth": 1, "state": "draining"}
            diag = {
                "inflight_total": 0,
                "stall_reason": "swap_cooldown",
                "cooldown_remaining": 1.5,
            }
            table = panel.render_data(data, queue_diag=diag)
        assert isinstance(table, Table)

    async def test_render_with_stall_other_reason(self) -> None:
        """Stall reason without cooldown numbers also renders."""
        app = _QueueHarness()
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            data = {"queue_depth": 0, "state": "running"}
            diag = {"inflight_total": 0, "stall_reason": "no_model_loaded"}
            table = panel.render_data(data, queue_diag=diag)
        assert isinstance(table, Table)

    async def test_render_with_latency_histories(self) -> None:
        """p50/p95 latency history rows are appended (red p95 branch hit)."""
        app = _QueueHarness()
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            data = {"queue_depth": 1, "state": "running", "queue_by_model": {"m": 1}}
            table = panel.render_data(
                data,
                queue_diag=None,
                latency_p50_history=[0.1, 0.2, 0.3],
                latency_p95_history=[1.0, 12.0, 15.0],  # >10 => red branch
            )
        assert isinstance(table, Table)

    async def test_render_with_queue_history_sparkline(self) -> None:
        """The ``self.app.queue_history`` sparkline branch fires."""
        app = _QueueHarness(queue_history=[1.0, 2.0, 3.0, 4.0, 5.0])
        async with app.run_test():
            panel = app.query_one("#queue", QueuePanel)
            data = {"queue_depth": 5, "state": "running", "queue_by_model": {"m": 5}}
            table = panel.render_data(data)
        assert isinstance(table, Table)


# ---------------------------------------------------------------------------
# SchedulerPanel
# ---------------------------------------------------------------------------


class TestSchedulerPanel:
    """SchedulerPanel renders uptime, served/swap counters, and rate sparklines."""

    def test_render_empty_data(self) -> None:
        """Empty dict must render the four base rows (Uptime/Served/Swaps/State)."""
        panel = _make(SchedulerPanel)
        table = panel.render_data({})
        assert isinstance(table, Table)
        assert table.row_count == 4

    def test_render_typical_payload(self) -> None:
        """A realistic scheduler status renders all base rows."""
        panel = _make(SchedulerPanel)
        data = {
            "uptime_seconds": 3725,
            "total_requests_served": 42,
            "total_model_swaps": 7,
            "state": "running",
        }
        table = panel.render_data(data)
        assert table.row_count == 4

    def test_render_with_throughput_history(self) -> None:
        """Throughput sparkline is appended when history is provided."""
        panel = _make(SchedulerPanel)
        table = panel.render_data(
            {"uptime_seconds": 60, "state": "running"},
            throughput_history=[1.0, 2.0, 3.0],
        )
        assert table.row_count == 5

    def test_render_with_swap_rate_green(self) -> None:
        """A low swap rate (<4/min) renders without raising."""
        panel = _make(SchedulerPanel)
        table = panel.render_data({"state": "running"}, swap_rate_history=[0.5, 1.0, 2.0])
        assert isinstance(table, Table)

    def test_render_with_swap_rate_yellow(self) -> None:
        """A medium swap rate (4-6/min) renders without raising."""
        panel = _make(SchedulerPanel)
        table = panel.render_data({"state": "running"}, swap_rate_history=[4.5, 5.0, 5.5])
        assert isinstance(table, Table)

    def test_render_with_swap_rate_red(self) -> None:
        """A high swap rate (>=6/min) renders without raising."""
        panel = _make(SchedulerPanel)
        table = panel.render_data({"state": "running"}, swap_rate_history=[7.0, 8.0, 9.0])
        assert isinstance(table, Table)

    def test_render_with_both_histories(self) -> None:
        """Throughput + swap rate sparklines both render."""
        panel = _make(SchedulerPanel)
        table = panel.render_data(
            {"uptime_seconds": 120, "state": "draining"},
            throughput_history=[10.0, 20.0],
            swap_rate_history=[1.0, 2.0],
        )
        # 4 base rows + Thru + Swap = 6
        assert table.row_count == 6


# ---------------------------------------------------------------------------
# CircuitBreakerPanel
# ---------------------------------------------------------------------------


class TestCircuitBreakerPanel:
    """CircuitBreakerPanel renders state, healthy flag, reason, scheduler running."""

    def test_render_empty(self) -> None:
        """Empty dict means circuit 'n/a' (disabled) and unhealthy default."""
        panel = _make(CircuitBreakerPanel)
        table = panel.render_data({})
        assert isinstance(table, Table)
        # State + Health + Scheduler = 3 rows (no reason)
        assert table.row_count == 3

    def test_render_closed_healthy(self) -> None:
        """closed/healthy/scheduler_running renders all four rows."""
        panel = _make(CircuitBreakerPanel)
        table = panel.render_data({
            "circuit": "closed",
            "healthy": True,
            "scheduler_running": True,
        })
        assert table.row_count == 3

    def test_render_open_with_reason(self) -> None:
        """An open circuit with a failure reason renders Reason row."""
        panel = _make(CircuitBreakerPanel)
        table = panel.render_data({
            "circuit": "open",
            "healthy": False,
            "reason": "consecutive backend failures",
            "scheduler_running": False,
        })
        # State + Health + Reason + Scheduler = 4 rows
        assert table.row_count == 4

    def test_render_half_open(self) -> None:
        """half_open state renders without raising."""
        panel = _make(CircuitBreakerPanel)
        table = panel.render_data({
            "circuit": "half_open",
            "healthy": True,
            "scheduler_running": True,
        })
        assert isinstance(table, Table)

    def test_render_long_reason_truncated(self) -> None:
        """A reason longer than 50 chars is truncated, not crashed."""
        panel = _make(CircuitBreakerPanel)
        table = panel.render_data({
            "circuit": "open",
            "healthy": False,
            "reason": "x" * 200,
            "scheduler_running": False,
        })
        assert isinstance(table, Table)


# ---------------------------------------------------------------------------
# WatchdogPanel
# ---------------------------------------------------------------------------


class TestWatchdogPanel:
    """WatchdogPanel renders Ollama/GPU health, latencies, failure counts."""

    def test_render_no_data(self) -> None:
        """Empty watchdog dict shows the '(no data)' placeholder."""
        panel = _make(WatchdogPanel)
        table = panel.render_data({})
        assert isinstance(table, Table)
        assert table.row_count == 1

    def test_render_healthy(self) -> None:
        """Healthy state with low latency renders all green rows."""
        panel = _make(WatchdogPanel)
        data = {
            "ollama_state": "healthy",
            "gpu_state": "responsive",
            "ollama_latency_ms": 50,
            "gpu_query_latency_ms": 200,
        }
        table = panel.render_data(data)
        # Ollama + GPU + Ollama ms + GPU ms = 4
        assert table.row_count == 4

    def test_render_unhealthy(self) -> None:
        """Unhealthy/timeout state with failure counters renders without raising."""
        panel = _make(WatchdogPanel)
        data = {
            "ollama_state": "unhealthy",
            "gpu_state": "timeout",
            "ollama_latency_ms": 800,
            "gpu_query_latency_ms": 3000,
            "consecutive_ollama_failures": 4,
            "consecutive_gpu_timeouts": 2,
            "scheduler_paused": True,
            "last_check": 1_700_000_000.0,
        }
        table = panel.render_data(data)
        # Ollama + GPU + Ollama ms + GPU ms + Ollama fail + GPU timeout + Sched + Checked = 8
        assert table.row_count == 8

    def test_render_latency_thresholds(self) -> None:
        """Yellow-band latencies render without raising."""
        panel = _make(WatchdogPanel)
        data = {
            "ollama_state": "healthy",
            "gpu_state": "responsive",
            "ollama_latency_ms": 250,  # yellow band
            "gpu_query_latency_ms": 1000,  # yellow band
        }
        table = panel.render_data(data)
        assert isinstance(table, Table)

    def test_render_with_latency_history(self) -> None:
        """Ollama latency sparkline appears when history is provided."""
        panel = _make(WatchdogPanel)
        data = {"ollama_state": "healthy", "gpu_state": "responsive"}
        table = panel.render_data(
            data,
            ollama_latency_history=[50.0, 75.0, 600.0],  # last is >500 -> red
        )
        # 2 base rows + Lat = 3
        assert table.row_count == 3

    def test_render_unknown_state(self) -> None:
        """Unknown state strings fall back to the dim style without raising."""
        panel = _make(WatchdogPanel)
        data = {"ollama_state": "weirdstate", "gpu_state": "unavailable"}
        table = panel.render_data(data)
        assert isinstance(table, Table)

    def test_render_bad_last_check(self) -> None:
        """A non-numeric ``last_check`` is swallowed, not raised."""
        panel = _make(WatchdogPanel)
        data = {
            "ollama_state": "healthy",
            "gpu_state": "responsive",
            "last_check": "not-a-timestamp",
        }
        table = panel.render_data(data)
        assert isinstance(table, Table)


# ---------------------------------------------------------------------------
# ThrashingPanel
# ---------------------------------------------------------------------------


class TestThrashingPanel:
    """ThrashingPanel renders global verdict, halt count, per-agent rows."""

    def test_render_no_data(self) -> None:
        """Empty dict shows '(no data)' placeholder."""
        panel = _make(ThrashingPanel)
        table = panel.render_data({})
        assert isinstance(table, Table)
        assert table.row_count == 1

    def test_render_ok_no_agents(self) -> None:
        """OK verdict with no agents shows the '(none tracked)' row."""
        panel = _make(ThrashingPanel)
        table = panel.render_data({"detector_state": "OK", "agents": []})
        # State + Agents-empty = 2
        assert table.row_count == 2

    @pytest.mark.parametrize("verdict", ["OK", "WARNED", "HALTED"])
    def test_render_each_verdict(self, verdict: str) -> None:
        """Every documented verdict color maps cleanly through render_data."""
        panel = _make(ThrashingPanel)
        table = panel.render_data({"detector_state": verdict, "agents": []})
        assert isinstance(table, Table)

    def test_render_unknown_verdict_fallback(self) -> None:
        """An unknown verdict falls back to the dim style without raising."""
        panel = _make(ThrashingPanel)
        table = panel.render_data({"detector_state": "MYSTERY", "agents": []})
        assert isinstance(table, Table)

    def test_render_with_halts_and_reset_epoch(self) -> None:
        """Halt counter and reset epoch are surfaced when supplied."""
        panel = _make(ThrashingPanel)
        table = panel.render_data(
            {"detector_state": "WARNED", "agents": []},
            halt_total=3,
            reset_epoch="2026-05-19T12:34:56Z",
        )
        # State + Halts + Since + Agents-empty = 4
        assert table.row_count == 4

    def test_render_short_reset_epoch(self) -> None:
        """A reset_epoch shorter than 19 chars is rendered verbatim."""
        panel = _make(ThrashingPanel)
        table = panel.render_data(
            {"detector_state": "OK", "agents": []},
            reset_epoch="short",
        )
        assert isinstance(table, Table)

    def test_render_with_agents(self) -> None:
        """Per-agent rows render with verdict, ratio, cooloff."""
        panel = _make(ThrashingPanel)
        agents = [
            {
                "agent_id": "agent-1",
                "verdict": "OK",
                "swap_ratio": 0.12,
                "cooloff_remaining_s": 0.0,
            },
            {
                "agent_id": "agent-2",
                "verdict": "WARNED",
                "swap_ratio": 0.55,
                "cooloff_remaining_s": 30.0,
            },
            {
                "agent_id": "agent-3",
                "verdict": "HALTED",
                "swap_ratio": 0.92,
                "cooloff_remaining_s": 120.0,
            },
        ]
        table = panel.render_data(
            {"detector_state": "HALTED", "agents": agents},
            halt_total=1,
        )
        # State + Halts + spacer + header + 3 agent rows = 7
        assert table.row_count == 7

    def test_render_agent_with_missing_fields(self) -> None:
        """Agent rows with missing fields fall back to defaults instead of crashing."""
        panel = _make(ThrashingPanel)
        agents: list[dict[str, Any]] = [{}, {"agent_id": "x"}]
        table = panel.render_data({"detector_state": "OK", "agents": agents})
        assert isinstance(table, Table)

    def test_render_long_agent_id_truncated(self) -> None:
        """A long agent id is truncated to 18 chars, not crashed."""
        panel = _make(ThrashingPanel)
        agents = [{
            "agent_id": "a" * 64,
            "verdict": "OK",
            "swap_ratio": 0.0,
            "cooloff_remaining_s": 0.0,
        }]
        table = panel.render_data({"detector_state": "OK", "agents": agents})
        assert isinstance(table, Table)


# ---------------------------------------------------------------------------
# AlertPanel
# ---------------------------------------------------------------------------


class TestAlertPanel:
    """AlertPanel renders severity-labeled alert rows."""

    def test_render_empty(self) -> None:
        """No alerts -> single 'No active alerts' row."""
        panel = _make(AlertPanel)
        table = panel.render_data([])
        assert isinstance(table, Table)
        assert table.row_count == 1

    def test_render_single_info(self) -> None:
        """A single info alert renders one row."""
        panel = _make(AlertPanel)
        table = panel.render_data([{"severity": "info", "message": "loaded model"}])
        assert table.row_count == 1

    def test_render_single_warn(self) -> None:
        """A single warn alert renders one row."""
        panel = _make(AlertPanel)
        table = panel.render_data([{"severity": "warn", "message": "VRAM at 85%"}])
        assert table.row_count == 1

    def test_render_single_critical(self) -> None:
        """A single critical alert renders one row."""
        panel = _make(AlertPanel)
        table = panel.render_data([{"severity": "critical", "message": "VRAM at 95%"}])
        assert table.row_count == 1

    def test_render_many_alerts(self) -> None:
        """All three severities together render the correct row count."""
        panel = _make(AlertPanel)
        alerts = [
            {"severity": "info", "message": "model loaded"},
            {"severity": "warn", "message": "queue depth at 12"},
            {"severity": "critical", "message": "temp at 84C"},
            {"severity": "info", "message": "scheduler resumed"},
        ]
        table = panel.render_data(alerts)
        assert table.row_count == 4

    def test_render_alert_with_missing_fields(self) -> None:
        """Alerts missing severity/message fall back to defaults without raising."""
        panel = _make(AlertPanel)
        table = panel.render_data([{}])
        assert table.row_count == 1

    def test_render_alert_unknown_severity(self) -> None:
        """Unknown severity strings fall back to dim style without raising."""
        panel = _make(AlertPanel)
        table = panel.render_data([{"severity": "panic", "message": "?"}])
        assert table.row_count == 1

    def test_thresholds_unchanged(self) -> None:
        """The alert threshold constants are part of the public contract."""
        assert AlertPanel.VRAM_WARN_PCT == 85.0
        assert AlertPanel.VRAM_CRIT_PCT == 95.0
        assert AlertPanel.TEMP_WARN_C == 75
        assert AlertPanel.TEMP_CRIT_C == 82
        assert AlertPanel.QUEUE_WARN == 10
        assert AlertPanel.QUEUE_CRIT == 50


class TestAlertPanelTimestamps:
    def test_alert_rows_show_raise_time(self) -> None:
        """Alerts carry epoch 'time'; the panel renders it as HH:MM:SS."""
        import time as _time
        from datetime import datetime as _dt

        panel = _make(AlertPanel)
        raised = _time.time()
        table = panel.render_data(
            [{"severity": "warn", "message": "VRAM at 85%", "time": raised}]
        )
        expected = _dt.fromtimestamp(raised).strftime("%H:%M:%S")
        first_cells = list(table.columns[0].cells)
        assert first_cells == [expected]

    def test_alert_without_time_renders_blank_cell(self) -> None:
        panel = _make(AlertPanel)
        table = panel.render_data([{"severity": "info", "message": "x"}])
        assert list(table.columns[0].cells) == [""]
