"""Main BASTION dashboard application with switchable layout modes."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections import deque
from datetime import datetime
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll

from bastion.dashboard.client import BastionClient
from bastion.dashboard.collectors import SystemDataCollector
from bastion.dashboard.modals import (
    ConfirmActionModal,
    ConfirmGPUKillModal,
    FanControlModal,
    GPUProcessListModal,
    HelpModal,
    ModelSelectModal,
    fan_control_available,
    set_fan_speed,
)
from bastion.dashboard.panels_broker import (
    AlertPanel,
    CircuitBreakerPanel,
    QueuePanel,
    SchedulerPanel,
    WatchdogPanel,
)
from bastion.dashboard.panels_gpu import GPUPanel, ModelsPanel, VRAMLedgerPanel
from bastion.dashboard.panels_secondary import (
    A2ATaskPanel,
    AuditStreamPanel,
    LeasePanel,
    TracePanel,
)
from bastion.dashboard.panels_system import (
    CPUPanel,
    MemoryPanel,
    NetworkPanel,
    TemperaturePanel,
)
from bastion.dashboard.statusbar import SafetyLimitsBar, StatusBar

# ---------------------------------------------------------------------------
# Layout modes
# ---------------------------------------------------------------------------

LAYOUT_MODES: set[str] = {"compact", "standard", "full"}

# ---------------------------------------------------------------------------
# Auto-fan constants
# ---------------------------------------------------------------------------

_AUTO_FAN_TRIGGER_C = 80
_AUTO_FAN_SPEED = "90"
_AUTO_FAN_RESET_C = 70


class BastionDashboard(App):
    """BASTION TUI dashboard with switchable layout modes."""

    TITLE = "BASTION Dashboard"

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: top;
    }

    SafetyLimitsBar {
        height: 2;
        dock: top;
        background: $boost;
    }

    #main {
        height: 1fr;
    }

    #left-col, #right-col, #third-col {
        width: 1fr;
    }

    Static {
        border: solid $primary-background;
        height: auto;
        min-height: 5;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "preload", "Preload"),
        Binding("u", "unload", "Unload"),
        Binding("d", "drain", "Drain"),
        Binding("s", "service_restart", "Service"),
        Binding("f", "fan_control", "Fan"),
        Binding("g", "gpu_kill", "GPU Kill"),
        Binding("h", "help", "Help"),
        Binding("t", "toggle_secondary", "Secondary"),
        Binding("1", "layout_compact", "Compact"),
        Binding("2", "layout_standard", "Standard"),
        Binding("3", "layout_full", "Full"),
        Binding("plus,equal", "sparkline_wider", "+Spark", show=False),
        Binding("minus,underscore", "sparkline_narrower", "-Spark", show=False),
        Binding("right_square_bracket", "history_longer", "+Hist", show=False),
        Binding("left_square_bracket", "history_shorter", "-Hist", show=False),
    ]

    def __init__(
        self,
        url: str = "http://localhost:11434",
        interval: float = 2.0,
        api_key: str | None = None,
        layout_mode: str = "standard",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)

        # Validate layout mode
        if layout_mode not in LAYOUT_MODES:
            layout_mode = "standard"
        self._layout_mode: str = layout_mode

        self._url = url
        self._interval = interval
        self._client = BastionClient(url, api_key=api_key)
        self._collector = SystemDataCollector()

        # History deques (length configurable via helpers.HISTORY_LEN)
        from bastion.dashboard.helpers import HISTORY_LEN
        self.vram_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.temp_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.queue_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.power_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.throughput_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.swap_rate_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.latency_p50_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.latency_p95_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.ollama_latency_history: deque[float] = deque(maxlen=HISTORY_LEN)
        self.alert_history: deque[dict[str, Any]] = deque(maxlen=100)

        # Previous counters for rate computation (delta between polls)
        self._prev_requests_served: int | None = None
        self._prev_model_swaps: int | None = None

        # Connection tracking
        self._connected: bool = False
        self._consecutive_failures: int = 0
        self._last_ok_time: str | None = None
        self._last_data: dict[str, Any] | None = None
        self._backoff_until: float = 0.0

        # Auto-fan state
        self._auto_fan_enabled: bool = False
        self._auto_fan_state: str = "idle"  # idle | triggered | cooling

        # Secondary panel toggle
        self._show_secondary: bool = False

        # Refresh timer handle
        self._refresh_timer: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")
        yield SafetyLimitsBar(id="safety-bar")
        with Horizontal(id="main"):
            with VerticalScroll(id="left-col"):
                yield GPUPanel(id="gpu")
                yield ModelsPanel(id="models")
                yield QueuePanel(id="queue")
                yield VRAMLedgerPanel(id="vram-ledger")
            with VerticalScroll(id="right-col"):
                yield TemperaturePanel(id="temperatures")
                yield MemoryPanel(id="memory")
                yield NetworkPanel(id="network")
                yield CPUPanel(id="cpu")
                yield AlertPanel(id="alerts")
            with VerticalScroll(id="third-col"):
                yield SchedulerPanel(id="scheduler")
                yield WatchdogPanel(id="watchdog")
                yield CircuitBreakerPanel(id="circuit-breaker")
                yield TracePanel(id="trace")
                yield A2ATaskPanel(id="a2a-tasks")
                yield LeasePanel(id="leases")
                yield AuditStreamPanel(id="audit-stream")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        """Apply initial layout and start the refresh loop."""
        self._apply_layout()
        self.set_interval(self._interval, self._tick)

    async def _tick(self) -> None:
        """Timer callback that triggers a data refresh."""
        await self.refresh_data()

    async def on_unmount(self) -> None:
        """Clean up the HTTP client on exit."""
        await self._client.close()

    # ------------------------------------------------------------------
    # Layout management
    # ------------------------------------------------------------------

    def _apply_layout(self) -> None:
        """Show/hide columns and panels based on current layout mode."""
        right_col = self.query_one("#right-col")
        third_col = self.query_one("#third-col")

        # Secondary panels: hidden by default, shown with [t] toggle
        secondary_ids = {"a2a-tasks", "leases", "audit-stream"}
        # Always visible in full mode (trace is request history — essential)
        non_secondary_ids = {"scheduler", "watchdog", "circuit-breaker", "trace"}

        if self._layout_mode == "compact":
            right_col.display = False
            third_col.display = False
        elif self._layout_mode == "standard":
            right_col.display = True
            third_col.display = False
        else:
            # full
            right_col.display = True
            third_col.display = True

            # Toggle secondary vs non-secondary panels within third-col
            for panel_id in secondary_ids:
                with contextlib.suppress(Exception):
                    self.query_one(f"#{panel_id}").display = self._show_secondary

            for panel_id in non_secondary_ids:
                with contextlib.suppress(Exception):
                    self.query_one(f"#{panel_id}").display = not self._show_secondary

        # Update status bar with layout mode
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update(
            status_bar.render_status(
                self._last_data,
                self._connected,
                stale=not self._connected and self._last_data is not None,
                last_ok_time=self._last_ok_time,
                consecutive_failures=self._consecutive_failures,
                layout_mode=self._layout_mode,
            )
        )

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    async def refresh_data(self) -> None:
        """Poll the broker and update all panels."""
        now = time.monotonic()

        # Backoff check
        if now < self._backoff_until:
            return

        # Poll broker status
        try:
            data = await self._client.poll()
            self._connected = True
            self._consecutive_failures = 0
            self._last_ok_time = datetime.now().strftime("%H:%M:%S")
            self._last_data = data
        except Exception:
            self._connected = False
            self._consecutive_failures += 1
            # Exponential backoff: 2s, 4s, 8s, max 30s
            backoff = min(2 ** self._consecutive_failures, 30)
            self._backoff_until = now + backoff
            data = self._last_data  # Use cached data

        if data is None:
            # Update status bar even without data
            status_bar = self.query_one("#status-bar", StatusBar)
            status_bar.update(
                status_bar.render_status(
                    None,
                    self._connected,
                    consecutive_failures=self._consecutive_failures,
                    layout_mode=self._layout_mode,
                )
            )
            return

        # Update histories
        gpu = data.get("gpu", {})
        vram_used = gpu.get("vram_used_mb")
        if vram_used is not None:
            self.vram_history.append(float(vram_used))

        temp = gpu.get("temperature_c")
        if temp is not None:
            self.temp_history.append(float(temp))

        power = gpu.get("power_draw_watts")
        if power is not None:
            self.power_history.append(float(power))

        queue_depth = data.get("queue_depth", 0)
        self.queue_history.append(float(queue_depth))

        # Throughput rate (requests/poll interval → requests/min)
        served = data.get("total_requests_served", 0)
        if self._prev_requests_served is not None:
            delta = served - self._prev_requests_served
            if delta >= 0:  # skip on broker restart (counter reset)
                rate_per_min = delta * (60.0 / self._interval) if self._interval > 0 else 0
                self.throughput_history.append(rate_per_min)
        self._prev_requests_served = served

        # Swap rate (swaps/poll interval → swaps/min)
        swaps = data.get("total_model_swaps", 0)
        if self._prev_model_swaps is not None:
            delta = swaps - self._prev_model_swaps
            if delta >= 0:  # skip on broker restart (counter reset)
                rate_per_min = delta * (60.0 / self._interval) if self._interval > 0 else 0
                self.swap_rate_history.append(rate_per_min)
        self._prev_model_swaps = swaps

        # Auto-fan and alert evaluation
        self._check_auto_fan(data)
        alerts = self._evaluate_alerts(data)

        # Fetch supplemental data in parallel
        health_data, vram_ledger, watchdog_data, queue_diag, recent = (
            await asyncio.gather(
                self._client.get_health(),
                self._client.get_vram_ledger(),
                self._client.get_watchdog(),
                self._client.get_queue(),
                self._client.get_recent(),
                return_exceptions=True,
            )
        )

        # Normalize exceptions to empty dicts/lists
        if isinstance(health_data, BaseException):
            health_data = {}
        if isinstance(vram_ledger, BaseException):
            vram_ledger = {}
        if isinstance(watchdog_data, BaseException):
            watchdog_data = {}
        if isinstance(queue_diag, BaseException):
            queue_diag = {}
        if isinstance(recent, BaseException):
            recent = []

        # Compute latency percentiles from recent requests
        if isinstance(recent, list) and recent:
            durations = sorted(
                r.get("duration_s", 0) for r in recent if isinstance(r, dict)
            )
            if durations:
                n = len(durations)
                p50 = durations[n // 2]
                p95 = durations[min(int(n * 0.95), n - 1)]
                self.latency_p50_history.append(p50)
                self.latency_p95_history.append(p95)

        # Track Ollama response latency from watchdog
        if isinstance(watchdog_data, dict):
            ollama_ms = watchdog_data.get("ollama_latency_ms")
            if ollama_ms is not None:
                self.ollama_latency_history.append(float(ollama_ms))

        # Update safety limits from broker config if available
        safety_bar = self.query_one("#safety-bar", SafetyLimitsBar)
        config = data.get("config", {})
        safety_bar.update_limits(
            config.get("max_vram_gb"),
            config.get("max_temp_c"),
        )

        # Collect system data
        cpu_data = self._collector.get_cpu_data()
        net_data = self._collector.get_network_data()
        mem_data = self._collector.get_memory_data()
        cpu_temp = self._collector.read_cpu_temp()
        nvme_temps_raw = self._collector.read_nvme_temps()
        nvme_temps = [int(t) for _, t in nvme_temps_raw] if nvme_temps_raw else None

        # Update status bar
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.update(
            status_bar.render_status(
                data,
                self._connected,
                stale=not self._connected and self._last_data is not None,
                last_ok_time=self._last_ok_time,
                consecutive_failures=self._consecutive_failures,
                layout_mode=self._layout_mode,
            )
        )

        # Update left-col panels
        gpu_panel = self.query_one("#gpu", GPUPanel)
        gpu_panel.update(gpu_panel.render_data(
            data,
            power_history=list(self.power_history),
        ))

        models_panel = self.query_one("#models", ModelsPanel)
        models_panel.update(models_panel.render_data(data))

        queue_panel = self.query_one("#queue", QueuePanel)
        queue_panel.update(queue_panel.render_data(
            data,
            queue_diag=queue_diag,
            latency_p50_history=list(self.latency_p50_history),
            latency_p95_history=list(self.latency_p95_history),
        ))

        vram_panel = self.query_one("#vram-ledger", VRAMLedgerPanel)
        vram_panel.update(vram_panel.render_data(vram_ledger))

        # Update right-col panels
        temp_panel = self.query_one("#temperatures", TemperaturePanel)
        gpu_ceiling = data.get("max_temperature_c", 85)
        temp_panel.update(
            temp_panel.render_data(
                cpu_temp=int(cpu_temp) if cpu_temp is not None else None,
                nvme_temps=nvme_temps,
                gpu_temp=temp,
                gpu_ceiling_c=gpu_ceiling,
            )
        )

        mem_panel = self.query_one("#memory", MemoryPanel)
        mem_panel.update(mem_panel.render_data(mem_data))

        net_panel = self.query_one("#network", NetworkPanel)
        net_panel.update(
            net_panel.render_data(
                {
                    "recv_bytes_sec": net_data.get("recv_rate", 0.0),
                    "sent_bytes_sec": net_data.get("sent_rate", 0.0),
                    "total_recv_bytes": net_data.get("recv_total_gb", 0.0) * (1024 ** 3),
                    "total_sent_bytes": net_data.get("sent_total_gb", 0.0) * (1024 ** 3),
                },
                recv_history=list(self._collector.net_recv_history),
                sent_history=list(self._collector.net_sent_history),
            )
        )

        cpu_panel = self.query_one("#cpu", CPUPanel)
        cpu_panel.update(
            cpu_panel.render_data(
                {
                    "overall_pct": cpu_data.get("percent", 0.0),
                    "load_avg": cpu_data.get("load_avg"),
                    "freq_mhz": cpu_data.get("freq_mhz"),
                    "per_core": cpu_data.get("per_core"),
                },
                cpu_history=list(self._collector.cpu_history),
            )
        )

        # Update third-col panels
        sched_panel = self.query_one("#scheduler", SchedulerPanel)
        sched_panel.update(sched_panel.render_data(
            data,
            throughput_history=list(self.throughput_history),
            swap_rate_history=list(self.swap_rate_history),
        ))

        wd_panel = self.query_one("#watchdog", WatchdogPanel)
        wd_panel.update(wd_panel.render_data(
            watchdog_data,
            ollama_latency_history=list(self.ollama_latency_history),
        ))

        cb_panel = self.query_one("#circuit-breaker", CircuitBreakerPanel)
        cb_panel.update(cb_panel.render_data(health_data))

        alert_panel = self.query_one("#alerts", AlertPanel)
        alert_panel.update(alert_panel.render_data(alerts))

        trace_panel = self.query_one("#trace", TracePanel)
        trace_panel.update(trace_panel.render_data(recent))

        a2a_panel = self.query_one("#a2a-tasks", A2ATaskPanel)
        a2a_panel.update(a2a_panel.render_data(data))

        lease_panel = self.query_one("#leases", LeasePanel)
        lease_panel.update(lease_panel.render_data(data))

        audit_panel = self.query_one("#audit-stream", AuditStreamPanel)
        audit_events = data.get("recent_audit_events", [])
        audit_panel.update(audit_panel.render_data(audit_events))

    # ------------------------------------------------------------------
    # Alert evaluation
    # ------------------------------------------------------------------

    def _evaluate_alerts(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Check thresholds and return current active alerts."""
        alerts: list[dict[str, Any]] = []
        now = time.time()

        gpu = data.get("gpu", {})
        vram_used = gpu.get("vram_used_mb")
        vram_hw_total = gpu.get("vram_total_mb")
        temp = gpu.get("temperature_c")
        queue_depth = data.get("queue_depth", 0)

        # Use configured budget as the alert denominator, not the hardware total.
        # The broker refuses loads at max_vram_gb; alerts must fire at or before
        # that boundary, not 3 GB past it.
        cfg_budget_gb = data.get("max_vram_gb")
        if cfg_budget_gb is not None:
            vram_budget = cfg_budget_gb * 1024
        else:
            vram_budget = vram_hw_total

        # VRAM alerts
        if vram_used is not None and vram_budget and vram_budget > 0:
            pct = vram_used / vram_budget * 100
            if pct >= AlertPanel.VRAM_CRIT_PCT:
                alerts.append({
                    "severity": AlertPanel.SEVERITY_CRITICAL,
                    "message": f"VRAM critical: {pct:.0f}%",
                    "time": now,
                })
            elif pct >= AlertPanel.VRAM_WARN_PCT:
                alerts.append({
                    "severity": AlertPanel.SEVERITY_WARN,
                    "message": f"VRAM high: {pct:.0f}%",
                    "time": now,
                })

        # Temperature alerts
        if temp is not None:
            if temp >= AlertPanel.TEMP_CRIT_C:
                alerts.append({
                    "severity": AlertPanel.SEVERITY_CRITICAL,
                    "message": f"GPU temp critical: {temp}\u00b0C",
                    "time": now,
                })
            elif temp >= AlertPanel.TEMP_WARN_C:
                alerts.append({
                    "severity": AlertPanel.SEVERITY_WARN,
                    "message": f"GPU temp high: {temp}\u00b0C",
                    "time": now,
                })

        # Queue alerts
        if queue_depth >= AlertPanel.QUEUE_CRIT:
            alerts.append({
                "severity": AlertPanel.SEVERITY_CRITICAL,
                "message": f"Queue overloaded: {queue_depth} pending",
                "time": now,
            })
        elif queue_depth >= AlertPanel.QUEUE_WARN:
            alerts.append({
                "severity": AlertPanel.SEVERITY_WARN,
                "message": f"Queue depth high: {queue_depth} pending",
                "time": now,
            })

        # Connection loss alert
        if not self._connected:
            alerts.append({
                "severity": AlertPanel.SEVERITY_CRITICAL,
                "message": f"Broker unreachable ({self._consecutive_failures} failures)",
                "time": now,
            })

        # Store in history
        for alert in alerts:
            self.alert_history.append(alert)

        return alerts

    # ------------------------------------------------------------------
    # Auto-fan control
    # ------------------------------------------------------------------

    def _check_auto_fan(self, data: dict[str, Any]) -> None:
        """Check CPU temperature and trigger fan speed escalation if needed."""
        if not self._auto_fan_enabled or not fan_control_available():
            return

        cpu_temp = self._collector.read_cpu_temp()
        if cpu_temp is None:
            return

        if self._auto_fan_state == "idle" and cpu_temp >= _AUTO_FAN_TRIGGER_C:
            self._auto_fan_state = "triggered"
            success, _msg = set_fan_speed(_AUTO_FAN_SPEED)
            if success:
                self._auto_fan_state = "cooling"
        elif self._auto_fan_state == "cooling" and cpu_temp < _AUTO_FAN_RESET_C:
            success, _msg = set_fan_speed("auto")
            if success:
                self._auto_fan_state = "idle"

    # ------------------------------------------------------------------
    # Action methods
    # ------------------------------------------------------------------

    def action_layout_compact(self) -> None:
        """Switch to compact layout."""
        self._layout_mode = "compact"
        self._apply_layout()

    def action_layout_standard(self) -> None:
        """Switch to standard layout."""
        self._layout_mode = "standard"
        self._apply_layout()

    def action_layout_full(self) -> None:
        """Switch to full layout."""
        self._layout_mode = "full"
        self._apply_layout()

    def action_toggle_secondary(self) -> None:
        """Toggle secondary panels (only effective in [3] full layout)."""
        if self._layout_mode != "full":
            self.notify(
                "Secondary panels require [3] full layout",
                severity="warning",
            )
            return
        self._show_secondary = not self._show_secondary
        self._apply_layout()

    def action_sparkline_wider(self) -> None:
        """Increase sparkline width by 5 chars."""
        from bastion.dashboard import helpers
        helpers.SPARKLINE_WIDTH = min(helpers.SPARKLINE_WIDTH + 5, 60)
        self.notify(f"Sparkline width: {helpers.SPARKLINE_WIDTH} chars")

    def action_sparkline_narrower(self) -> None:
        """Decrease sparkline width by 5 chars."""
        from bastion.dashboard import helpers
        helpers.SPARKLINE_WIDTH = max(helpers.SPARKLINE_WIDTH - 5, 5)
        self.notify(f"Sparkline width: {helpers.SPARKLINE_WIDTH} chars")

    def action_history_longer(self) -> None:
        """Increase history length by 30 samples."""
        from bastion.dashboard import helpers
        helpers.HISTORY_LEN = min(helpers.HISTORY_LEN + 30, 600)
        self._resize_histories(helpers.HISTORY_LEN)
        secs = helpers.HISTORY_LEN * self._interval
        self.notify(f"History: {helpers.HISTORY_LEN} samples (~{secs:.0f}s)")

    def action_history_shorter(self) -> None:
        """Decrease history length by 30 samples."""
        from bastion.dashboard import helpers
        helpers.HISTORY_LEN = max(helpers.HISTORY_LEN - 30, 30)
        self._resize_histories(helpers.HISTORY_LEN)
        secs = helpers.HISTORY_LEN * self._interval
        self.notify(f"History: {helpers.HISTORY_LEN} samples (~{secs:.0f}s)")

    def _resize_histories(self, new_maxlen: int) -> None:
        """Resize all history deques to a new maxlen, preserving data."""
        for attr in (
            "vram_history", "temp_history", "queue_history",
            "power_history", "throughput_history", "swap_rate_history",
            "latency_p50_history", "latency_p95_history",
            "ollama_latency_history",
        ):
            old = getattr(self, attr)
            new = deque(old, maxlen=new_maxlen)
            setattr(self, attr, new)
        # Also resize collector histories
        for attr in ("cpu_history", "net_recv_history", "net_sent_history"):
            old = getattr(self._collector, attr)
            new = deque(old, maxlen=new_maxlen)
            setattr(self._collector, attr, new)

    async def action_refresh(self) -> None:
        """Force an immediate data refresh."""
        self._backoff_until = 0.0
        await self.refresh_data()

    def action_help(self) -> None:
        """Show the help modal."""
        self.push_screen(HelpModal())

    def action_fan_control(self) -> None:
        """Open fan control modal."""

        def _handle_fan(speed: str) -> None:
            if not speed:
                return
            if speed == "toggle-auto":
                self._auto_fan_enabled = not self._auto_fan_enabled
                if not self._auto_fan_enabled:
                    self._auto_fan_state = "idle"
                return
            success, msg = set_fan_speed(speed)
            if not success:
                self.notify(f"Fan control failed: {msg}", severity="error")

        self.push_screen(FanControlModal(), callback=_handle_fan)

    def action_gpu_kill(self) -> None:
        """Open GPU process list for killing."""

        def _handle_process_select(pid: str) -> None:
            if not pid:
                return
            # Find process details for confirmation
            procs = SystemDataCollector.query_gpu_processes()
            proc = next((p for p in procs if p["pid"] == pid), None)
            if proc is None:
                self.notify(f"Process {pid} no longer exists", severity="warning")
                return

            def _handle_kill_confirm(action: str) -> None:
                if not action:
                    return
                try:
                    sig = signal.SIGKILL if action == "kill-9" else signal.SIGTERM
                    os.kill(int(pid), sig)
                    self.notify(f"Sent {sig.name} to PID {pid}")
                except (ProcessLookupError, PermissionError) as exc:
                    self.notify(f"Kill failed: {exc}", severity="error")

            self.push_screen(
                ConfirmGPUKillModal(
                    pid=proc["pid"],
                    name=proc["name"],
                    vram_mb=proc["vram_mb"],
                ),
                callback=_handle_kill_confirm,
            )

        self.push_screen(GPUProcessListModal(), callback=_handle_process_select)

    def action_preload(self) -> None:
        """Open model selection for preloading."""
        if self._last_data is None:
            self.notify("No broker data available", severity="warning")
            return

        available = self._last_data.get("available_models", [])
        loaded_names = [
            m.get("name", "") for m in self._last_data.get("loaded_models", [])
        ]
        # Show models not already loaded
        candidates = [m for m in available if m not in loaded_names] or available

        if not candidates:
            self.notify("No models available to preload", severity="warning")
            return

        def _handle_model(model: str) -> None:
            if model:
                self.run_worker(self._do_preload(model))

        self.push_screen(
            ModelSelectModal("Select model to preload", candidates),
            callback=_handle_model,
        )

    async def _do_preload(self, model: str) -> None:
        """Execute preload request."""
        try:
            result = await self._client.post_preload(model)
            self.notify(f"Preload: {result.get('status', 'ok')}")
        except Exception as exc:
            self.notify(f"Preload failed: {exc}", severity="error")

    def action_unload(self) -> None:
        """Open model selection for unloading."""
        if self._last_data is None:
            self.notify("No broker data available", severity="warning")
            return

        loaded = self._last_data.get("loaded_models", [])
        names = [m.get("name", "") for m in loaded if m.get("name")]

        if not names:
            self.notify("No models loaded to unload", severity="warning")
            return

        def _handle_model(model: str) -> None:
            if model:
                self.run_worker(self._do_unload(model))

        self.push_screen(
            ModelSelectModal("Select model to unload", names),
            callback=_handle_model,
        )

    async def _do_unload(self, model: str) -> None:
        """Execute unload request."""
        try:
            result = await self._client.post_unload(model)
            self.notify(f"Unload: {result.get('status', 'ok')}")
        except Exception as exc:
            self.notify(f"Unload failed: {exc}", severity="error")

    def action_drain(self) -> None:
        """Toggle drain mode."""
        state = self._last_data.get("state", "") if self._last_data else ""
        if state == "draining":
            action_name = "Resume Scheduling"
            details = "Resume the scheduler from drain mode?"
        else:
            action_name = "Drain Mode"
            details = "Pause the scheduler? No new requests will be dispatched."

        def _handle_confirm(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(self._do_drain(state))

        self.push_screen(
            ConfirmActionModal(action_name, details),
            callback=_handle_confirm,
        )

    async def _do_drain(self, current_state: str) -> None:
        """Execute drain/resume toggle."""
        try:
            if current_state == "draining":
                result = await self._client.post_resume()
            else:
                result = await self._client.post_drain()
            self.notify(f"Drain toggle: {result.get('status', 'ok')}")
        except Exception as exc:
            self.notify(f"Drain toggle failed: {exc}", severity="error")

    def action_service_restart(self) -> None:
        """Restart bastion.service via systemctl."""

        def _handle_confirm(confirmed: bool) -> None:
            if not confirmed:
                return
            import subprocess

            try:
                result = subprocess.run(
                    ["sudo", "systemctl", "restart", "bastion.service"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    self.notify("bastion.service restarted")
                else:
                    self.notify(
                        f"Restart failed: {result.stderr.strip()}",
                        severity="error",
                    )
            except Exception as exc:
                self.notify(f"Restart failed: {exc}", severity="error")

        self.push_screen(
            ConfirmActionModal(
                "Restart Service",
                "Restart bastion.service via systemctl? Requires sudo.",
            ),
            callback=_handle_confirm,
        )
