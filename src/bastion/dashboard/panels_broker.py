"""Queue, scheduler, circuit breaker, watchdog, and alert panels."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rich.table import Table
from rich.text import Text

from bastion.dashboard.helpers import (
    SPARKLINE_WIDTH,
    cb_state_color,
    format_uptime,
    sparkline,
    state_color,
)
from bastion.dashboard.widgets import BastionPanel


class QueuePanel(BastionPanel):
    """Queue depth by model, scheduler state, and stall diagnostics."""

    def render_data(
        self,
        data: dict[str, Any],
        queue_diag: dict[str, Any] | None = None,
        latency_p50_history: list[float] | None = None,
        latency_p95_history: list[float] | None = None,
    ) -> Table:
        by_model = data.get("queue_by_model", {})
        total = data.get("queue_depth", 0)
        state = data.get("state", "unknown")
        w = SPARKLINE_WIDTH

        table = Table(title="Queue", expand=True, show_edge=False, pad_edge=False)
        table.add_column("Model", ratio=3)
        table.add_column("Depth", ratio=1, justify="right")

        if not by_model:
            table.add_row(Text("(empty)", style="dim"), "")
        else:
            for model, depth in sorted(by_model.items()):
                table.add_row(model, str(depth))

        table.add_row("", "")
        table.add_row(Text("Total", style="bold"), str(total))
        table.add_row(
            Text("State", style="bold"),
            Text(state, style=state_color(state)),
        )

        # Stall diagnostics from /broker/queue
        if queue_diag:
            inflight_total = queue_diag.get("inflight_total", 0)
            if inflight_total > 0:
                table.add_row(Text("In-flight", style="bold"), str(inflight_total))

            stall_reason = queue_diag.get("stall_reason", "")
            if stall_reason:
                cooldown = queue_diag.get("cooldown_remaining", 0)
                stall_text = stall_reason
                if stall_reason == "swap_cooldown" and cooldown > 0:
                    stall_text = f"{stall_reason} ({cooldown:.1f}s remaining)"
                table.add_row(
                    Text("Stall", style="bold"),
                    Text(stall_text, style="yellow"),
                )

        # Queue depth sparkline
        if hasattr(self.app, "queue_history") and self.app.queue_history:
            table.add_row(
                Text("Trend", style="bold"),
                Text(sparkline(list(self.app.queue_history), w), style="yellow"),
            )

        # Request latency sparklines (p50 / p95)
        if latency_p50_history:
            p50_val = latency_p50_history[-1]
            line = Text()
            line.append(sparkline(latency_p50_history, w), style="green")
            line.append(f" {p50_val:.1f}s", style="green")
            table.add_row(Text("p50 \u2581\u2582", style="bold"), line)
        if latency_p95_history:
            p95_val = latency_p95_history[-1]
            color = "yellow" if p95_val < 10 else "red"
            line = Text()
            line.append(sparkline(latency_p95_history, w), style=color)
            line.append(f" {p95_val:.1f}s", style=color)
            table.add_row(Text("p95 \u2581\u2582", style="bold"), line)

        return table


class SchedulerPanel(BastionPanel):
    """Scheduler uptime, requests served, model swaps, throughput and swap rate sparklines."""

    def render_data(
        self,
        data: dict[str, Any],
        throughput_history: list[float] | None = None,
        swap_rate_history: list[float] | None = None,
    ) -> Table:
        uptime = data.get("uptime_seconds", 0)
        served = data.get("total_requests_served", 0)
        swaps = data.get("total_model_swaps", 0)
        state = data.get("state", "unknown")
        w = SPARKLINE_WIDTH

        table = Table(title="Scheduler", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=10)
        table.add_column("value")

        table.add_row("Uptime", format_uptime(uptime))
        table.add_row("Served", str(served))
        table.add_row("Swaps", str(swaps))
        table.add_row("State", Text(state, style=state_color(state)))

        # Throughput sparkline (requests/min)
        if throughput_history:
            rate = throughput_history[-1]
            line = Text()
            line.append(sparkline(throughput_history, w), style="cyan")
            line.append(f" {rate:.1f}/min", style="cyan")
            table.add_row("Thru \u2581\u2582", line)

        # Swap rate sparkline (swaps/min)
        if swap_rate_history:
            rate = swap_rate_history[-1]
            color = "green" if rate < 4 else ("yellow" if rate < 6 else "red")
            line = Text()
            line.append(sparkline(swap_rate_history, w), style=color)
            line.append(f" {rate:.1f}/min", style=color)
            table.add_row("Swap \u2581\u2582", line)

        return table


class CircuitBreakerPanel(BastionPanel):
    """Circuit breaker state, failure count, and recovery countdown."""

    def render_data(self, health_data: dict[str, Any]) -> Table:
        circuit_state = health_data.get("circuit", "n/a")
        healthy = health_data.get("healthy")
        reason = health_data.get("reason", "")
        scheduler_running = health_data.get("scheduler_running", False)

        table = Table(title="Circuit Breaker", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=10)
        table.add_column("value")

        if circuit_state == "n/a":
            table.add_row("State", Text("disabled", style="dim"))
        else:
            table.add_row("State", Text(circuit_state, style=cb_state_color(circuit_state)))

        health_str = "healthy" if healthy else "unhealthy"
        health_style = "green" if healthy else "red bold"
        table.add_row("Health", Text(health_str, style=health_style))

        if reason:
            table.add_row("Reason", Text(reason[:50], style="dim"))

        sched_str = "running" if scheduler_running else "stopped"
        sched_style = "green" if scheduler_running else "red"
        table.add_row("Scheduler", Text(sched_str, style=sched_style))

        return table


class WatchdogPanel(BastionPanel):
    """Process monitor status: Ollama health and GPU responsiveness."""

    def render_data(
        self,
        watchdog_data: dict[str, Any],
        ollama_latency_history: list[float] | None = None,
    ) -> Table:
        table = Table(title="Watchdog", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=12)
        table.add_column("value")
        w = SPARKLINE_WIDTH

        if not watchdog_data:
            table.add_row(Text("(no data)", style="dim"), "")
            return table

        # Ollama state
        ollama_state = watchdog_data.get("ollama_state", "unknown")
        if ollama_state == "healthy":
            ollama_style = "green"
        elif ollama_state == "unhealthy":
            ollama_style = "red bold"
        else:
            ollama_style = "dim"
        table.add_row("Ollama", Text(ollama_state, style=ollama_style))

        # GPU state
        gpu_state = watchdog_data.get("gpu_state", "unavailable")
        if gpu_state == "responsive":
            gpu_style = "green"
        elif gpu_state == "timeout":
            gpu_style = "red bold"
        else:
            gpu_style = "dim"
        table.add_row("GPU", Text(gpu_state, style=gpu_style))

        # Latencies
        ollama_ms = watchdog_data.get("ollama_latency_ms")
        if ollama_ms is not None:
            latency_style = "green" if ollama_ms < 100 else ("yellow" if ollama_ms < 500 else "red")
            table.add_row("Ollama ms", Text(f"{ollama_ms:.0f}ms", style=latency_style))

        gpu_ms = watchdog_data.get("gpu_query_latency_ms")
        if gpu_ms is not None:
            latency_style = "green" if gpu_ms < 500 else ("yellow" if gpu_ms < 2000 else "red")
            table.add_row("GPU ms", Text(f"{gpu_ms:.0f}ms", style=latency_style))

        # Ollama latency sparkline
        if ollama_latency_history:
            ms_val = ollama_latency_history[-1]
            color = "green" if ms_val < 100 else ("yellow" if ms_val < 500 else "red")
            line = Text()
            line.append(sparkline(ollama_latency_history, w), style=color)
            line.append(f" {ms_val:.0f}ms", style=color)
            table.add_row("Lat \u2581\u2582", line)

        # Failure counts
        ollama_fails = watchdog_data.get("consecutive_ollama_failures", 0)
        gpu_timeouts = watchdog_data.get("consecutive_gpu_timeouts", 0)
        if ollama_fails > 0:
            table.add_row("Ollama fail", Text(str(ollama_fails), style="red"))
        if gpu_timeouts > 0:
            table.add_row("GPU timeout", Text(str(gpu_timeouts), style="red"))

        # Scheduler paused
        paused = watchdog_data.get("scheduler_paused", False)
        if paused:
            table.add_row("Sched", Text("PAUSED", style="red bold"))

        # Last check time
        last_check = watchdog_data.get("last_check")
        if last_check is not None:
            try:
                dt = datetime.fromtimestamp(last_check, tz=UTC)
                table.add_row("Checked", dt.strftime("%H:%M:%S"))
            except (ValueError, OSError, TypeError):
                pass

        return table


class ThrashingPanel(BastionPanel):
    """Thrashing detector: global verdict, halt counter, per-agent rows."""

    _VERDICT_STYLES: dict[str, str] = {
        "OK": "green",
        "WARNED": "yellow bold",
        "HALTED": "red bold",
    }

    def render_data(
        self,
        thrashing_data: dict[str, Any],
        halt_total: int | None = None,
        reset_epoch: str | None = None,
    ) -> Table:
        table = Table(title="Thrashing", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=10)
        table.add_column("value")

        if not thrashing_data:
            table.add_row(Text("(no data)", style="dim"), "")
            return table

        state = thrashing_data.get("detector_state", "OK")
        state_style = self._VERDICT_STYLES.get(state, "dim")
        table.add_row("State", Text(state, style=state_style))

        if halt_total is not None:
            halt_style = "red bold" if halt_total > 0 else "dim"
            table.add_row("Halts", Text(str(halt_total), style=halt_style))

        if reset_epoch:
            # Show only HH:MM:SS suffix; epoch is helpful but full ISO is noisy
            label = reset_epoch[11:19] if len(reset_epoch) >= 19 else reset_epoch
            table.add_row("Since", Text(label, style="dim"))

        agents = thrashing_data.get("agents", [])
        if not agents:
            table.add_row(Text("Agents", style="bold"), Text("(none tracked)", style="dim"))
            return table

        # Header for agent rows
        table.add_row("", "")
        table.add_row(Text("Agent", style="bold"), Text("Verdict · ratio · cooloff", style="bold"))
        for agent in agents:
            agent_id = agent.get("agent_id", "?")
            verdict = agent.get("verdict", "OK")
            ratio = agent.get("swap_ratio", 0.0)
            cooloff = agent.get("cooloff_remaining_s", 0.0)
            style = self._VERDICT_STYLES.get(verdict, "dim")
            detail = f"{verdict} · {ratio:.2f} · {cooloff:.0f}s"
            table.add_row(Text(agent_id[:18], style="dim"), Text(detail, style=style))

        return table


class AlertPanel(BastionPanel):
    """Severity-tiered alert display with auto-dismiss."""

    SEVERITY_INFO = "info"
    SEVERITY_WARN = "warn"
    SEVERITY_CRITICAL = "critical"

    # Thresholds
    VRAM_WARN_PCT = 85.0
    VRAM_CRIT_PCT = 95.0
    TEMP_WARN_C = 75
    TEMP_CRIT_C = 82
    QUEUE_WARN = 10
    QUEUE_CRIT = 50

    # Auto-dismiss durations (seconds)
    DISMISS_INFO = 30.0
    DISMISS_WARN = 60.0
    # Critical alerts persist until condition clears

    _SEVERITY_STYLES: dict[str, str] = {
        "info": "cyan",
        "warn": "yellow",
        "critical": "red bold",
    }

    _SEVERITY_LABELS: dict[str, str] = {
        "info": "INFO",
        "warn": "WARN",
        "critical": "CRIT",
    }

    def render_data(self, alerts: list[dict[str, Any]]) -> Table:
        """Render active alerts as a severity-colored table."""
        table = Table(
            title="Alerts",
            expand=True,
            show_edge=False,
            pad_edge=False,
        )
        table.add_column("Sev", width=5, style="bold")
        table.add_column("Message", ratio=1)

        if not alerts:
            table.add_row(Text("OK", style="green"), Text("No active alerts", style="dim"))
        else:
            for alert in alerts:
                severity = alert.get("severity", self.SEVERITY_INFO)
                message = alert.get("message", "")
                style = self._SEVERITY_STYLES.get(severity, "dim")
                label = self._SEVERITY_LABELS.get(severity, "?")
                table.add_row(Text(label, style=style), Text(message, style=style))

        return table
