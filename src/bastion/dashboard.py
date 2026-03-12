"""BASTION TUI Dashboard -- real-time monitoring via /broker/* API.

Read-only Textual TUI that polls BASTION's admin endpoints to display
GPU, queue, scheduler, circuit breaker, A2A tasks, VRAM budget ledger,
leases, and audit events. No BASTION internals imported; connects over
HTTP like any other client.

S11 enhancements:
  - A2A task panel, circuit breaker panel, VRAM budget ledger panel,
    lease/reservation panel, audit event stream panel
  - Responsive layout (narrow terminals stack vertically)
  - Two-port mode support (--admin-url)
  - Help overlay (h), A2A tasks view (a), circuit breaker details (c)
  - Connection health indicator with STALE badge and exponential backoff

Usage:
    python -m bastion.dashboard
    python -m bastion.dashboard --url http://localhost:11434 --interval 2.0
    python -m bastion.dashboard --admin-url http://localhost:9999
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, Static


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def temp_color(temp: Optional[int]) -> str:
    """Return a rich color string for GPU temperature."""
    if temp is None:
        return "dim"
    if temp < 50:
        return "green"
    if temp < 70:
        return "yellow"
    if temp < 80:
        return "dark_orange"
    return "red bold"


def usage_color(pct: Optional[float]) -> str:
    """Return a rich color string for utilization percentage."""
    if pct is None:
        return "dim"
    if pct < 50:
        return "green"
    if pct < 75:
        return "yellow"
    if pct < 90:
        return "dark_orange"
    return "red bold"


def state_color(state: str) -> str:
    """Return a rich color string for scheduler state."""
    if state == "running":
        return "green"
    if state == "draining":
        return "yellow"
    return "red"


def cb_state_color(state: str) -> str:
    """Return a rich color string for circuit breaker state."""
    if state == "closed":
        return "green"
    if state == "half_open":
        return "yellow"
    return "red bold"


def a2a_state_color(state: str) -> str:
    """Return a rich color string for A2A task state."""
    if state in ("completed",):
        return "green"
    if state in ("working", "submitted"):
        return "yellow"
    if state in ("failed", "canceled"):
        return "red"
    return "dim"


def lease_state_color(state: str) -> str:
    """Return a rich color string for lease state."""
    if state == "active":
        return "green"
    if state == "expired":
        return "red"
    return "yellow"


def format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    if not days:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_countdown(seconds: float) -> str:
    """Format a countdown in seconds to a compact string."""
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs}s"


def sparkline(values: list[float], width: int = 20) -> str:
    """Render a sparkline from a list of values using block characters."""
    if not values:
        return ""
    blocks = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    lo = min(values)
    hi = max(values)
    span = hi - lo if hi != lo else 1.0
    # Take the last `width` values
    recent = values[-width:]
    return "".join(blocks[min(int((v - lo) / span * 8), 8)] for v in recent)


def vram_bar(used_mb: Optional[int], total_mb: Optional[int], width: int = 16) -> Text:
    """Render a VRAM usage bar with color."""
    if used_mb is None or total_mb is None or total_mb == 0:
        return Text("no data", style="dim")
    pct = used_mb / total_mb
    filled = int(pct * width)
    empty = width - filled
    color = usage_color(pct * 100)
    bar = Text()
    bar.append("\u2588" * filled, style=color)
    bar.append("\u2591" * empty, style="dim")
    bar.append(f" {used_mb / 1024:.1f}/{total_mb / 1024:.0f}GB", style=color)
    return bar


def format_bytes_gb(b: Optional[int]) -> str:
    """Format bytes as GB string."""
    if b is None:
        return "n/a"
    return f"{b / (1024 * 1024 * 1024):.1f}GB"


def format_bytes_mb(b: Optional[int]) -> str:
    """Format bytes as MB string."""
    if b is None:
        return "n/a"
    return f"{b / (1024 * 1024):.0f}MB"


# ---------------------------------------------------------------------------
# Fan control constants
# ---------------------------------------------------------------------------

FAN_WRAPPER_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "gpu_fan_control_wrapper.py"
FAN_PYTHON_PATH = Path(sys.executable)


def _read_cpu_temp() -> Optional[float]:
    """Read CPU temperature from sysfs (k10temp AMD or coretemp Intel)."""
    try:
        hwmon_path = Path("/sys/class/hwmon")
        for hwmon in hwmon_path.iterdir():
            name_file = hwmon / "name"
            if name_file.exists():
                name = name_file.read_text().strip()
                if name in ("k10temp", "coretemp"):
                    temp_file = hwmon / "temp1_input"
                    if temp_file.exists():
                        return int(temp_file.read_text().strip()) / 1000
    except Exception:
        pass
    return None


def _query_gpu_processes() -> list[dict[str, str]]:
    """Query nvidia-smi for GPU compute processes with PID, name, VRAM."""
    procs: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    raw_name = parts[1]
                    short_name = Path(raw_name).name[:20] if "/" in raw_name else raw_name[:20]
                    procs.append({
                        "pid": parts[0],
                        "name": short_name,
                        "vram_mb": parts[2],
                    })
    except Exception:
        pass
    return procs


def _set_fan_speed(speed: str) -> tuple[bool, str]:
    """Set GPU fan speed via the wrapper script. Returns (success, message)."""
    try:
        result = subprocess.run(
            ["sudo", str(FAN_PYTHON_PATH), str(FAN_WRAPPER_PATH), speed],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or "unknown error"
    except subprocess.TimeoutExpired:
        return False, "fan control timed out"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class BastionClient:
    """Async HTTP client for BASTION's admin API."""

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=5.0, headers=headers)

    async def poll(self) -> Dict[str, Any]:
        """Fetch /broker/status and return parsed JSON."""
        resp = await self._client.get(f"{self.base_url}/broker/status")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def get_recent(self) -> list[dict]:
        """Fetch /broker/recent and return parsed JSON."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/recent")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return []

    async def get_queue(self) -> dict:
        """Fetch /broker/queue for stall diagnostics."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/queue")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_health(self) -> dict:
        """Fetch /broker/health for circuit breaker state."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/health")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_vram_ledger(self) -> dict:
        """Fetch /broker/vram for VRAM ledger status."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/vram")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def get_watchdog(self) -> dict:
        """Fetch /broker/watchdog for process monitor status."""
        try:
            resp = await self._client.get(f"{self.base_url}/broker/watchdog")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    async def post_preload(self, model: str) -> dict:
        """Preload a model via /broker/preload."""
        resp = await self._client.post(
            f"{self.base_url}/broker/preload",
            json={"model": model},
        )
        return resp.json()

    async def post_unload(self, model: str) -> dict:
        """Unload a model via /broker/unload."""
        resp = await self._client.post(
            f"{self.base_url}/broker/unload",
            json={"model": model},
        )
        return resp.json()

    async def post_drain(self) -> dict:
        """Toggle drain mode via /broker/drain."""
        resp = await self._client.post(f"{self.base_url}/broker/drain")
        return resp.json()

    async def post_resume(self) -> dict:
        """Resume from drain mode via /broker/resume."""
        resp = await self._client.post(f"{self.base_url}/broker/resume")
        return resp.json()


# ---------------------------------------------------------------------------
# Panel widgets
# ---------------------------------------------------------------------------

class GPUPanel(Static):
    """GPU temperature, VRAM, and power status."""

    def render_data(self, data: Dict[str, Any]) -> Table:
        gpu = data.get("gpu", {})
        temp = gpu.get("temperature_c")
        used = gpu.get("vram_used_mb")
        total = gpu.get("vram_total_mb")
        power = gpu.get("power_draw_watts")
        pct = (used / total * 100) if used is not None and total else None

        table = Table(title="GPU", expand=True, show_header=False, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=8)
        table.add_column("value")

        temp_str = f"{temp}\u00b0C" if temp is not None else "n/a"
        table.add_row("Temp", Text(temp_str, style=temp_color(temp)))
        table.add_row("VRAM", vram_bar(used, total))
        if pct is not None:
            table.add_row("Usage", Text(f"{pct:.1f}%", style=usage_color(pct)))
        power_str = f"{power:.0f}W" if power is not None else "n/a"
        table.add_row("Power", power_str)
        safe = gpu.get("is_safe", True) if gpu else True
        safe_str = "OK" if safe else "UNSAFE"
        safe_style = "green bold" if safe else "red bold"
        table.add_row("Safety", Text(safe_str, style=safe_style))

        # Sparkline rows for VRAM and temperature history
        app = self.app
        if hasattr(app, "vram_history") and app.vram_history:
            table.add_row("VRAM  \u2581\u2582", Text(sparkline(list(app.vram_history)), style="cyan"))
        if hasattr(app, "temp_history") and app.temp_history:
            table.add_row("Temp  \u2581\u2582", Text(sparkline(list(app.temp_history)), style=temp_color(temp)))

        return table


class ModelsPanel(Static):
    """Currently loaded models in Ollama."""

    def render_data(self, data: Dict[str, Any]) -> Table:
        loaded = data.get("loaded_models", [])
        current = data.get("current_model")

        table = Table(title="Models Loaded", expand=True, show_edge=False, pad_edge=False)
        table.add_column("Model", ratio=3)
        table.add_column("VRAM", ratio=1, justify="right")

        if not loaded:
            table.add_row(Text("(none)", style="dim"), "")
        else:
            for m in loaded:
                name = m.get("name", "?")
                vram = m.get("vram_gb", 0.0)
                prefix = "* " if current and name.startswith(current.split(":")[0]) else "  "
                style = "bold cyan" if prefix.startswith("*") else ""
                table.add_row(Text(f"{prefix}{name}", style=style), f"{vram:.1f}GB")

        if current:
            table.add_row("", "")
            table.add_row(Text(f"Active: {current}", style="bold"), "")

        return table


class QueuePanel(Static):
    """Queue depth by model, scheduler state, and stall diagnostics."""

    def render_data(self, data: Dict[str, Any], queue_diag: Optional[Dict[str, Any]] = None) -> Table:
        by_model = data.get("queue_by_model", {})
        total = data.get("queue_depth", 0)
        state = data.get("state", "unknown")

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
                Text(sparkline(list(self.app.queue_history)), style="yellow"),
            )

        return table


class SchedulerPanel(Static):
    """Scheduler uptime, requests served, model swaps."""

    def render_data(self, data: Dict[str, Any]) -> Table:
        uptime = data.get("uptime_seconds", 0)
        served = data.get("total_requests_served", 0)
        swaps = data.get("total_model_swaps", 0)
        state = data.get("state", "unknown")

        table = Table(title="Scheduler", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=10)
        table.add_column("value")

        table.add_row("Uptime", format_uptime(uptime))
        table.add_row("Served", str(served))
        table.add_row("Swaps", str(swaps))
        table.add_row("State", Text(state, style=state_color(state)))

        return table


class ConnectionPanel(Static):
    """BASTION reachability indicator with STALE badge and backoff info."""

    def __init__(self, url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.connected = False
        self.last_ok: Optional[str] = None
        self.last_error: Optional[str] = None
        self.consecutive_failures: int = 0
        self.next_retry_at: Optional[float] = None

    def render_data(
        self,
        connected: bool,
        error: Optional[str] = None,
        consecutive_failures: int = 0,
        next_retry_at: Optional[float] = None,
    ) -> Table:
        self.connected = connected
        self.consecutive_failures = consecutive_failures
        self.next_retry_at = next_retry_at
        if connected:
            self.last_ok = datetime.now(timezone.utc).strftime("%H:%M:%S")
            self.last_error = None
        else:
            self.last_error = error or "connection failed"

        table = Table(show_header=False, show_edge=False, pad_edge=False, expand=True)
        table.add_column("key", style="bold", width=10)
        table.add_column("value")

        if self.connected:
            table.add_row("Status", Text("CONNECTED", style="green bold"))
        else:
            table.add_row("Status", Text("DISCONNECTED", style="red bold"))
            if self.last_error:
                table.add_row("Error", Text(self.last_error[:60], style="red"))
            if self.consecutive_failures > 0:
                table.add_row("Failures", Text(str(self.consecutive_failures), style="red"))
            if self.next_retry_at is not None:
                remaining = max(0.0, self.next_retry_at - time.monotonic())
                table.add_row("Retry in", Text(format_countdown(remaining), style="yellow"))
            table.add_row("Hint", Text("Press [s] to restart service", style="yellow"))

        table.add_row("URL", self.url)
        if self.last_ok:
            table.add_row("Last OK", self.last_ok)

        return table


class AlertPanel(Static):
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

    def render_data(self, alerts: List[Dict[str, Any]]) -> Table:
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


class SafetyLimitsBar(Static):
    """VRAM budget visualization bar (26 GB budget)."""

    VRAM_BUDGET_GB = 26.0  # Total minus headroom

    def render_data(self, used_gb: float) -> Text:
        """Render a colored horizontal bar showing VRAM budget usage."""
        pct = (used_gb / self.VRAM_BUDGET_GB * 100) if self.VRAM_BUDGET_GB > 0 else 0.0
        pct = min(pct, 100.0)

        bar_width = 30
        filled = int(pct / 100 * bar_width)
        empty = bar_width - filled

        if pct < 50:
            color = "green"
        elif pct < 75:
            color = "yellow"
        elif pct < 90:
            color = "dark_orange"
        else:
            color = "red bold"

        bar = Text()
        bar.append(" VRAM Budget ", style="bold")
        bar.append("[")
        bar.append("=" * filled, style=color)
        bar.append("-" * empty, style="dim")
        bar.append("] ")
        bar.append(f"{used_gb:.1f}/{self.VRAM_BUDGET_GB:.1f} GB", style=color)
        return bar


# ---------------------------------------------------------------------------
# New panels (S11: Dashboard Modernization)
# ---------------------------------------------------------------------------

class CircuitBreakerPanel(Static):
    """Circuit breaker state, failure count, and recovery countdown."""

    def render_data(self, health_data: Dict[str, Any]) -> Table:
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


class VRAMLedgerPanel(Static):
    """VRAM budget panel showing VRAMManager's allocated/reserved ledger."""

    def render_data(self, ledger: Dict[str, Any]) -> Table:
        table = Table(title="VRAM Ledger", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=12)
        table.add_column("value")

        if not ledger:
            table.add_row(Text("(no data)", style="dim"), "")
            return table

        total = ledger.get("total_bytes")
        safety = ledger.get("safety_margin_bytes")
        allocated = ledger.get("allocated_bytes")
        reserved = ledger.get("reserved_bytes")
        available = ledger.get("available_bytes")
        active_res = ledger.get("active_reservations", 0)

        table.add_row("Total", format_bytes_gb(total))
        table.add_row("Safety", format_bytes_mb(safety))
        table.add_row("Allocated", Text(format_bytes_gb(allocated), style="cyan"))
        table.add_row("Reserved", Text(format_bytes_gb(reserved), style="yellow"))
        table.add_row("Available", Text(format_bytes_gb(available), style="green bold"))
        table.add_row("Reserv#", str(active_res))

        # Show utilization bar
        if total and total > 0:
            used = (allocated or 0) + (reserved or 0) + (safety or 0)
            pct = used / total * 100
            bar_width = 20
            filled = int(min(pct, 100) / 100 * bar_width)
            empty = bar_width - filled
            color = usage_color(pct)
            bar = Text()
            bar.append("\u2588" * filled, style=color)
            bar.append("\u2591" * empty, style="dim")
            bar.append(f" {pct:.0f}%", style=color)
            table.add_row("Usage", bar)

        # Show individual reservations
        reservations = ledger.get("reservations", [])
        for r in reservations[:5]:
            model = r.get("model", "?")
            vram_bytes = r.get("vram_bytes", 0)
            age = r.get("age_seconds", 0)
            committed = r.get("committed", False)
            status_str = "committed" if committed else "pending"
            status_style = "green" if committed else "yellow"
            table.add_row(
                Text(f"  {model[:10]}", style="dim"),
                Text(f"{format_bytes_mb(vram_bytes)} {status_str} ({age:.0f}s)", style=status_style),
            )

        return table


class A2ATaskPanel(Static):
    """Active A2A tasks display showing state and skill types."""

    def render_data(self, status_data: Dict[str, Any]) -> Table:
        table = Table(title="A2A Tasks", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=10)
        table.add_column("value")

        # A2A data comes from /broker/status if available, or synthesized
        # from what the status endpoint provides
        a2a_tasks = status_data.get("a2a_tasks", [])
        a2a_summary = status_data.get("a2a_summary", {})

        if not a2a_tasks and not a2a_summary:
            table.add_row(Text("(none)", style="dim"), "")
            table.add_row("", Text("No active A2A tasks", style="dim"))
            return table

        # Summary counts
        total = a2a_summary.get("total", len(a2a_tasks))
        working = a2a_summary.get("working", 0)
        submitted = a2a_summary.get("submitted", 0)
        completed = a2a_summary.get("completed", 0)
        failed = a2a_summary.get("failed", 0)

        table.add_row("Total", str(total))
        if working:
            table.add_row("Working", Text(str(working), style="yellow"))
        if submitted:
            table.add_row("Queued", Text(str(submitted), style="cyan"))
        if completed:
            table.add_row("Done", Text(str(completed), style="green"))
        if failed:
            table.add_row("Failed", Text(str(failed), style="red"))

        # Show individual tasks (up to 5)
        for task in a2a_tasks[:5]:
            task_id = task.get("task_id", "?")[:8]
            state = task.get("state", "?")
            skill = task.get("skill_id", "?")[:12]
            table.add_row(
                Text(f"  {task_id}", style="dim"),
                Text(f"{skill} [{state}]", style=a2a_state_color(state)),
            )

        return table


class LeasePanel(Static):
    """Active model leases/reservations panel."""

    def render_data(self, status_data: Dict[str, Any]) -> Table:
        table = Table(title="Leases", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=12)
        table.add_column("value")

        leases = status_data.get("leases", [])

        if not leases:
            table.add_row(Text("(none)", style="dim"), "")
            table.add_row("", Text("No active leases", style="dim"))
            return table

        table.add_row("Active", str(len(leases)))

        for lease in leases[:5]:
            lease_id = lease.get("lease_id", "?")[:8]
            model = lease.get("model", "?")[:12]
            remaining = lease.get("remaining_requests", 0)
            state = lease.get("state", "unknown")
            ttl = lease.get("ttl_remaining", 0)

            info_parts: list[str] = [
                f"{model}",
                f"reqs={remaining}",
            ]
            if ttl > 0:
                info_parts.append(f"TTL={format_countdown(ttl)}")

            table.add_row(
                Text(f"  {lease_id}", style="dim"),
                Text(
                    " ".join(info_parts),
                    style=lease_state_color(state),
                ),
            )

        return table


class AuditStreamPanel(Static):
    """Last N audit events panel."""

    def render_data(self, events: list[dict]) -> Table:
        table = Table(title="Audit Events", expand=True, show_edge=False, pad_edge=False)
        table.add_column("Time", width=8)
        table.add_column("Event", width=12)
        table.add_column("Details", ratio=1)

        if not events:
            table.add_row(Text("(none)", style="dim"), "", "")
        else:
            for evt in events[:10]:
                ts_raw = evt.get("timestamp", "")
                # Parse ISO timestamp to HH:MM:SS
                try:
                    if isinstance(ts_raw, str) and "T" in ts_raw:
                        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        ts = dt.strftime("%H:%M:%S")
                    else:
                        ts = str(ts_raw)[:8]
                except (ValueError, TypeError):
                    ts = str(ts_raw)[:8]

                event_type = evt.get("event", "?")
                details = evt.get("details", {})

                # Build a compact detail string
                detail_parts: list[str] = []
                if isinstance(details, dict):
                    if "model" in details:
                        detail_parts.append(details["model"][:15])
                    if "severity" in details:
                        detail_parts.append(details["severity"])
                    if "status_code" in details:
                        detail_parts.append(f"st={details['status_code']}")
                    if "vram_used_gb" in details:
                        detail_parts.append(f"vram={details['vram_used_gb']}GB")
                detail_str = " ".join(detail_parts) if detail_parts else str(details)[:30]

                # Color by event type
                if event_type == "vram_alert":
                    style = "red"
                elif event_type == "swap":
                    style = "yellow"
                elif event_type == "request_complete":
                    style = "green"
                else:
                    style = "dim"

                table.add_row(ts, Text(event_type, style=style), Text(detail_str, style="dim"))

        return table


class WatchdogPanel(Static):
    """Process monitor status: Ollama health and GPU responsiveness."""

    def render_data(self, watchdog_data: Dict[str, Any]) -> Table:
        table = Table(title="Watchdog", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=12)
        table.add_column("value")

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
                dt = datetime.fromtimestamp(last_check, tz=timezone.utc)
                table.add_row("Checked", dt.strftime("%H:%M:%S"))
            except (ValueError, OSError, TypeError):
                pass

        return table


# ---------------------------------------------------------------------------
# Modal dialogs for interactive actions (S5)
# ---------------------------------------------------------------------------

class ConfirmActionModal(ModalScreen[bool]):
    """Modal confirmation dialog for destructive actions."""

    DEFAULT_CSS = """
    ConfirmActionModal {
        align: center middle;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #confirm-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }

    Button {
        margin: 0 2;
    }
    """

    def __init__(self, action: str, details: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action_name = action
        self.action_details = details

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Confirm: {self.action_name}", id="confirm-title")
            yield Label(self.action_details, id="confirm-details")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="primary", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class ModelSelectModal(ModalScreen[str]):
    """Modal to select a model from loaded models."""

    DEFAULT_CSS = """
    ModelSelectModal {
        align: center middle;
    }

    #select-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, title: str, models: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.title_text = title
        self.model_list = models

    def compose(self) -> ComposeResult:
        with Vertical(id="select-dialog"):
            yield Label(self.title_text)
            for model in self.model_list:
                yield Button(model, id=f"model-{model}")
            yield Button("Cancel", variant="primary", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss("")
        else:
            model_name = event.button.id.replace("model-", "", 1)
            self.dismiss(model_name)


class HelpModal(ModalScreen[bool]):
    """Help overlay showing all keyboard bindings."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }

    #help-dialog {
        width: 65;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #help-title {
        text-align: center;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label("BASTION Dashboard -- Keyboard Shortcuts", id="help-title")
            yield Label("")
            yield Label("  [h]  Show this help overlay")
            yield Label("  [r]  Force refresh all panels")
            yield Label("  [f]  GPU fan control (30/50/70/90/100%/auto)")
            yield Label("  [g]  Kill a GPU process")
            yield Label("  [p]  Preload a model into VRAM")
            yield Label("  [u]  Unload a model from VRAM")
            yield Label("  [d]  Toggle drain mode (pause/resume scheduling)")
            yield Label("  [s]  Restart bastion.service (requires sudoers)")
            yield Label("  [a]  Focus A2A tasks view")
            yield Label("  [c]  Focus circuit breaker details")
            yield Label("  [q]  Quit the dashboard")
            yield Label("")
            yield Label("  Data refreshes automatically at the configured interval.")
            yield Label("  Connection indicator shows STALE when broker unreachable.")
            yield Label("")
            with Horizontal(id="confirm-buttons"):
                yield Button("Close", variant="primary", id="close-help")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(True)


# ---------------------------------------------------------------------------
# Fan control modal (S12: GPU management)
# ---------------------------------------------------------------------------

class FanControlModal(ModalScreen[str]):
    """Fan speed selection modal with auto-trigger toggle."""

    DEFAULT_CSS = """
    FanControlModal {
        align: center middle;
    }

    #fan-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #fan-title {
        text-align: center;
        text-style: bold;
    }

    #fan-row-low, #fan-row-high, #fan-row-actions {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #fan-row-low Button, #fan-row-high Button, #fan-row-actions Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        auto_fan = getattr(self.app, "_auto_fan_enabled", False)
        auto_state = getattr(self.app, "_auto_fan_state", "idle")
        auto_status = "ON" if auto_fan else "OFF"
        auto_detail = f" (80C -> 90%, {auto_state})" if auto_fan else ""

        with Vertical(id="fan-dialog"):
            yield Label("GPU Fan Control", id="fan-title")
            yield Label("Press a button to set fan speed:")
            yield Label("")
            with Horizontal(id="fan-row-low"):
                yield Button("30%", id="fan-30")
                yield Button("50%", id="fan-50")
                yield Button("70%", id="fan-70")
            with Horizontal(id="fan-row-high"):
                yield Button("90%", id="fan-90")
                yield Button("100%", id="fan-100", variant="error")
                yield Button("Auto", id="fan-auto", variant="success")
            yield Label("")
            yield Label(f"Auto-trigger: {auto_status}{auto_detail}")
            with Horizontal(id="fan-row-actions"):
                yield Button(
                    f"Auto-trigger: {auto_status}",
                    id="fan-toggle-auto",
                    variant="warning" if auto_fan else "default",
                )
                yield Button("Cancel", id="fan-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "fan-cancel":
            self.dismiss("")
        elif btn_id == "fan-toggle-auto":
            self.dismiss("toggle-auto")
        elif btn_id and btn_id.startswith("fan-"):
            speed = btn_id.replace("fan-", "")
            self.dismiss(speed)


class GPUProcessListModal(ModalScreen[str]):
    """List GPU processes and select one to kill."""

    DEFAULT_CSS = """
    GPUProcessListModal {
        align: center middle;
    }

    #gpuproc-dialog {
        width: 70;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #gpuproc-title {
        text-align: center;
        text-style: bold;
    }

    #gpuproc-dialog Button {
        margin: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._procs: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        self._procs = _query_gpu_processes()
        with Vertical(id="gpuproc-dialog"):
            yield Label("GPU Processes", id="gpuproc-title")
            if self._procs:
                yield Label("Select a process to kill:")
                yield Label("")
                for proc in self._procs[:9]:
                    label = f"{proc['name']:<20s}  PID {proc['pid']:>7s}  {proc['vram_mb']:>6s} MB"
                    yield Button(label, id=f"gpuproc-{proc['pid']}")
            else:
                yield Label("No GPU compute processes found.")
            yield Label("")
            yield Button("Cancel", id="gpuproc-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "gpuproc-cancel" or btn_id is None:
            self.dismiss("")
        elif btn_id.startswith("gpuproc-"):
            pid = btn_id.replace("gpuproc-", "")
            self.dismiss(pid)


class ConfirmGPUKillModal(ModalScreen[str]):
    """Confirm kill of a GPU process with normal and force options."""

    DEFAULT_CSS = """
    ConfirmGPUKillModal {
        align: center middle;
    }

    #gpukill-title {
        text-align: center;
        text-style: bold;
    }

    #gpukill-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #gpukill-buttons Button {
        margin: 0 1;
    }

    #gpukill-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, pid: str, name: str, vram_mb: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.proc_pid = pid
        self.proc_name = name
        self.proc_vram = vram_mb

    def compose(self) -> ComposeResult:
        with Vertical(id="gpukill-dialog"):
            yield Label("Kill GPU Process?", id="gpukill-title")
            yield Label(f"PID:  {self.proc_pid}")
            yield Label(f"Name: {self.proc_name}")
            yield Label(f"VRAM: {self.proc_vram} MB")
            yield Label("")
            with Horizontal(id="gpukill-buttons"):
                yield Button("Kill", id="kill-normal", variant="warning")
                yield Button("Force Kill (SIGKILL)", id="kill-force", variant="error")
                yield Button("Cancel", id="kill-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "kill-cancel":
            self.dismiss("")
        elif btn_id == "kill-normal":
            self.dismiss("kill")
        elif btn_id == "kill-force":
            self.dismiss("kill-9")


# ---------------------------------------------------------------------------
# Trace panel (S5)
# ---------------------------------------------------------------------------

class TracePanel(Static):
    """Live request trace viewer showing recent requests."""

    def render_data(self, recent: list[dict]) -> Table:
        table = Table(title="Request Trace", expand=True, show_edge=False, pad_edge=False)
        table.add_column("Time", width=8)
        table.add_column("Model", ratio=2)
        table.add_column("Tier", width=6)
        table.add_column("Wait", width=5, justify="right")
        table.add_column("Dur", width=5, justify="right")
        table.add_column("St", width=3, justify="right")

        if not recent:
            table.add_row(Text("(no requests)", style="dim"), "", "", "", "", "")
        else:
            for req in recent[:20]:  # Show last 20 in the panel
                ts = datetime.fromtimestamp(req.get("timestamp", 0)).strftime("%H:%M:%S")
                model = req.get("model", "?")
                # Truncate long model names
                if len(model) > 15:
                    model = model[:12] + "..."
                tier = req.get("tier", "?")[:4]
                wait = f"{req.get('queue_wait_s', 0):.1f}s"
                dur = f"{req.get('duration_s', 0):.1f}s"
                status = str(req.get("status_code", "?"))
                style = "green" if status == "200" else "red"
                table.add_row(ts, model, tier, wait, dur, Text(status, style=style))

        return table


# ---------------------------------------------------------------------------
# Status bar (top line) — S11: includes connection health indicator
# ---------------------------------------------------------------------------

class StatusBar(Static):
    """Compact top-line status summary with connection health indicator."""

    def render_status(
        self,
        data: Optional[Dict[str, Any]],
        connected: bool,
        stale: bool = False,
        last_ok_time: Optional[str] = None,
    ) -> Text:
        now = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(" BASTION Dashboard  ", style="bold white on dark_blue")
        line.append("  ")

        # Connection health indicator
        if connected:
            line.append("[*]", style="green bold")
        else:
            line.append("[X]", style="red bold")
        line.append(" ")

        if stale:
            line.append(" STALE ", style="bold white on red")
            line.append(" ")

        if not connected and data is None:
            line.append("DISCONNECTED", style="red bold")
            line.append(f"  {now}", style="dim")
            return line

        if data is None:
            line.append(f"  {now}", style="dim")
            return line

        gpu = data.get("gpu", {})
        temp = gpu.get("temperature_c")
        used = gpu.get("vram_used_mb")
        total = gpu.get("vram_total_mb")
        state = data.get("state", "?")

        if temp is not None:
            line.append(f"GPU: {temp}\u00b0C", style=temp_color(temp))
        else:
            line.append("GPU: n/a", style="dim")
        line.append("  |  ")

        if used is not None and total is not None:
            line.append(f"VRAM: {used / 1024:.1f}/{total / 1024:.0f}GB", style=usage_color(
                used / total * 100 if total else None
            ))
        else:
            line.append("VRAM: n/a", style="dim")
        line.append("  |  ")

        line.append(f"State: {state}", style=state_color(state))
        line.append(f"  {now}", style="dim")

        if last_ok_time:
            line.append(f"  (last OK: {last_ok_time})", style="dim")

        return line


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

DASHBOARD_CSS = """
Screen {
    layout: vertical;
}

StatusBar {
    height: 1;
    dock: top;
}

#main {
    layout: horizontal;
    height: 1fr;
}

#left-col {
    width: 1fr;
    height: 100%;
}

#mid-col {
    width: 1fr;
    height: 100%;
}

#right-col {
    width: 1fr;
    height: 100%;
}

GPUPanel {
    height: auto;
    min-height: 8;
    border: round green;
}

ModelsPanel {
    height: auto;
    min-height: 7;
    border: round cyan;
}

ConnectionPanel {
    height: auto;
    min-height: 5;
    border: round blue;
}

QueuePanel {
    height: auto;
    min-height: 9;
    border: round yellow;
}

SchedulerPanel {
    height: auto;
    min-height: 7;
    border: round magenta;
}

AlertPanel {
    height: auto;
    min-height: 5;
    border: round red;
}

SafetyLimitsBar {
    height: auto;
    min-height: 3;
    border: round green;
}

TracePanel {
    height: auto;
    min-height: 7;
    border: round white;
}

CircuitBreakerPanel {
    height: auto;
    min-height: 6;
    border: round darkorange;
}

VRAMLedgerPanel {
    height: auto;
    min-height: 8;
    border: round green;
}

A2ATaskPanel {
    height: auto;
    min-height: 6;
    border: round blue;
}

LeasePanel {
    height: auto;
    min-height: 5;
    border: round cyan;
}

AuditStreamPanel {
    height: auto;
    min-height: 7;
    border: round magenta;
}

WatchdogPanel {
    height: auto;
    min-height: 6;
    border: round darkorange;
}
"""


class BastionDashboard(App):
    """Textual TUI for monitoring BASTION."""

    CSS = DASHBOARD_CSS
    TITLE = "BASTION Dashboard"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("p", "preload", "Preload"),
        ("u", "unload", "Unload"),
        ("d", "drain", "Drain"),
        ("s", "service_restart", "Restart"),
        ("f", "fan_control", "Fan"),
        ("g", "gpu_kill", "GPU Kill"),
        ("h", "help", "Help"),
        ("a", "focus_a2a", "A2A"),
        ("c", "focus_circuit", "Circuit"),
    ]

    def __init__(
        self, url: str, interval: float = 2.0, api_key: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.client = BastionClient(url, api_key=api_key)
        self.interval = interval
        self.last_data: Optional[Dict[str, Any]] = None
        self.connected = False
        self.vram_history: deque[float] = deque(maxlen=60)
        self.temp_history: deque[float] = deque(maxlen=60)
        self.queue_history: deque[float] = deque(maxlen=60)
        self.alert_history: deque[dict] = deque(maxlen=100)
        self.audit_events: deque[dict] = deque(maxlen=50)
        # Connection health tracking (B5)
        self._consecutive_failures: int = 0
        self._next_retry_at: Optional[float] = None
        self._backoff_base: float = 2.0
        self._backoff_max: float = 60.0
        self._last_ok_time: Optional[str] = None
        # Auto-fan: CPU-triggered GPU fan escalation (S12)
        self._auto_fan_enabled: bool = True
        self._auto_fan_state: str = "idle"  # "idle" or "active"
        self._auto_fan_threshold: int = 80  # trigger temp (C)
        self._auto_fan_hysteresis: int = 5  # release at threshold - hysteresis
        self._auto_fan_speed: str = "90"  # fan speed when triggered

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")
        with Horizontal(id="main"):
            with VerticalScroll(id="left-col"):
                yield GPUPanel(id="gpu")
                yield ModelsPanel(id="models")
                yield SafetyLimitsBar(id="safety-bar")
                yield AlertPanel(id="alerts")
            with VerticalScroll(id="mid-col"):
                yield QueuePanel(id="queue")
                yield SchedulerPanel(id="scheduler")
                yield CircuitBreakerPanel(id="circuit-breaker")
                yield WatchdogPanel(id="watchdog")
                yield VRAMLedgerPanel(id="vram-ledger")
            with VerticalScroll(id="right-col"):
                yield TracePanel(id="trace")
                yield A2ATaskPanel(id="a2a-tasks")
                yield LeasePanel(id="leases")
                yield AuditStreamPanel(id="audit-stream")
                yield ConnectionPanel(self.client.base_url, id="connection")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(self.interval, self.refresh_data)
        # Immediately fire one poll
        self.call_later(self.refresh_data)

    def _compute_backoff(self) -> float:
        """Compute exponential backoff interval based on consecutive failures."""
        if self._consecutive_failures <= 0:
            return self.interval
        exponent = min(self._consecutive_failures, 6)  # Cap at 2^6 = 64
        backoff = self._backoff_base ** exponent
        return min(backoff, self._backoff_max)

    async def refresh_data(self) -> None:
        """Poll BASTION and update all panels."""
        error_msg: Optional[str] = None
        health_data: dict = {}
        vram_ledger: dict = {}
        watchdog_data: dict = {}
        queue_diag: dict = {}

        # Check if we should skip this poll (exponential backoff)
        if self._next_retry_at is not None and time.monotonic() < self._next_retry_at:
            # Still in backoff — update stale display but skip polling
            self._update_panels_stale()
            return

        try:
            data = await self.client.poll()
            self.last_data = data
            self.connected = True
            self._consecutive_failures = 0
            self._next_retry_at = None
            self._last_ok_time = datetime.now(timezone.utc).strftime("%H:%M:%S")

            # Accumulate history for sparklines
            gpu = data.get("gpu", {})
            if gpu.get("vram_used_mb") is not None:
                self.vram_history.append(gpu["vram_used_mb"])
            if gpu.get("temperature_c") is not None:
                self.temp_history.append(gpu["temperature_c"])
            self.queue_history.append(float(data.get("queue_depth", 0)))

            # Auto-fan: escalate GPU fan when CPU overheats
            self._check_auto_fan()

            # Evaluate alert thresholds (pass queue_diag for stall reason)
            self._evaluate_alerts(data, queue_diag=queue_diag)

            # Fetch supplemental data (circuit breaker, VRAM ledger, watchdog, queue diag)
            health_data, vram_ledger, watchdog_data, queue_diag = await asyncio.gather(
                self.client.get_health(),
                self.client.get_vram_ledger(),
                self.client.get_watchdog(),
                self.client.get_queue(),
            )

        except Exception as exc:
            self.connected = False
            self._consecutive_failures += 1
            backoff = self._compute_backoff()
            self._next_retry_at = time.monotonic() + backoff
            data = self.last_data  # Show stale data if available
            error_msg = str(exc)

        # Update panels
        stale = not self.connected and data is not None
        status_bar: StatusBar = self.query_one("#status-bar", StatusBar)
        status_bar.update(status_bar.render_status(
            data, self.connected, stale=stale, last_ok_time=self._last_ok_time,
        ))

        gpu_panel: GPUPanel = self.query_one("#gpu", GPUPanel)
        if data:
            gpu_panel.update(gpu_panel.render_data(data))

        models_panel: ModelsPanel = self.query_one("#models", ModelsPanel)
        if data:
            models_panel.update(models_panel.render_data(data))

        queue_panel: QueuePanel = self.query_one("#queue", QueuePanel)
        if data:
            queue_panel.update(queue_panel.render_data(data, queue_diag=queue_diag))

        scheduler_panel: SchedulerPanel = self.query_one("#scheduler", SchedulerPanel)
        if data:
            scheduler_panel.update(scheduler_panel.render_data(data))

        conn_panel: ConnectionPanel = self.query_one("#connection", ConnectionPanel)
        conn_panel.update(conn_panel.render_data(
            self.connected,
            error_msg if not self.connected else None,
            consecutive_failures=self._consecutive_failures,
            next_retry_at=self._next_retry_at,
        ))

        # Update alert panel
        alert_panel: AlertPanel = self.query_one("#alerts", AlertPanel)
        alert_panel.update(alert_panel.render_data(list(self.alert_history)))

        # Update safety limits bar
        safety_bar: SafetyLimitsBar = self.query_one("#safety-bar", SafetyLimitsBar)
        if data:
            gpu_data = data.get("gpu", {})
            used_mb = gpu_data.get("vram_used_mb")
            if used_mb is not None:
                safety_bar.update(safety_bar.render_data(used_mb / 1024))

        # Update request trace panel
        trace_panel: TracePanel = self.query_one("#trace", TracePanel)
        recent = await self.client.get_recent()
        trace_panel.update(trace_panel.render_data(recent))

        # Update circuit breaker panel
        cb_panel: CircuitBreakerPanel = self.query_one("#circuit-breaker", CircuitBreakerPanel)
        cb_panel.update(cb_panel.render_data(health_data))

        # Update watchdog panel
        wd_panel: WatchdogPanel = self.query_one("#watchdog", WatchdogPanel)
        wd_panel.update(wd_panel.render_data(watchdog_data))

        # Update VRAM ledger panel
        vram_panel: VRAMLedgerPanel = self.query_one("#vram-ledger", VRAMLedgerPanel)
        vram_panel.update(vram_panel.render_data(vram_ledger))

        # Update A2A tasks panel (from status data)
        a2a_panel: A2ATaskPanel = self.query_one("#a2a-tasks", A2ATaskPanel)
        a2a_panel.update(a2a_panel.render_data(data or {}))

        # Update leases panel (from status data)
        lease_panel: LeasePanel = self.query_one("#leases", LeasePanel)
        lease_panel.update(lease_panel.render_data(data or {}))

        # Update audit stream panel (from recent requests as proxy for audit events)
        audit_panel: AuditStreamPanel = self.query_one("#audit-stream", AuditStreamPanel)
        # Build pseudo-audit events from recent requests
        audit_events = self._build_audit_events(recent)
        audit_panel.update(audit_panel.render_data(audit_events))

    def _update_panels_stale(self) -> None:
        """Update display with stale data during backoff period."""
        data = self.last_data
        stale = data is not None

        status_bar: StatusBar = self.query_one("#status-bar", StatusBar)
        status_bar.update(status_bar.render_status(
            data, self.connected, stale=stale, last_ok_time=self._last_ok_time,
        ))

        conn_panel: ConnectionPanel = self.query_one("#connection", ConnectionPanel)
        conn_panel.update(conn_panel.render_data(
            self.connected,
            "connection lost (backoff)",
            consecutive_failures=self._consecutive_failures,
            next_retry_at=self._next_retry_at,
        ))

    def _build_audit_events(self, recent: list[dict]) -> list[dict]:
        """Build audit-like events from recent requests for the audit panel."""
        events: list[dict] = []
        for req in recent[:10]:
            ts = req.get("timestamp", 0)
            try:
                iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            except (ValueError, OSError, TypeError):
                iso_ts = str(ts)
            events.append({
                "timestamp": iso_ts,
                "event": "request_complete",
                "details": {
                    "model": req.get("model", "?"),
                    "status_code": req.get("status_code", "?"),
                },
            })
        return events

    def _evaluate_alerts(self, data: Dict[str, Any], queue_diag: Optional[Dict[str, Any]] = None) -> None:
        """Check thresholds and manage alert lifecycle."""
        now = time.monotonic()

        # Auto-dismiss expired non-critical alerts
        active: list[dict] = []
        for alert in self.alert_history:
            severity = alert.get("severity", AlertPanel.SEVERITY_INFO)
            age = now - alert.get("timestamp", now)
            if severity == AlertPanel.SEVERITY_INFO and age > AlertPanel.DISMISS_INFO:
                continue
            if severity == AlertPanel.SEVERITY_WARN and age > AlertPanel.DISMISS_WARN:
                continue
            active.append(alert)

        # Collect current alert keys to avoid duplicates
        active_keys = {a.get("key") for a in active}

        gpu = data.get("gpu", {})
        used_mb = gpu.get("vram_used_mb")
        total_mb = gpu.get("vram_total_mb")
        temp = gpu.get("temperature_c")
        queue_depth = data.get("queue_depth", 0)

        # VRAM threshold checks
        if used_mb is not None and total_mb and total_mb > 0:
            vram_pct = used_mb / total_mb * 100
            if vram_pct >= AlertPanel.VRAM_CRIT_PCT and "vram_crit" not in active_keys:
                active.append({
                    "severity": AlertPanel.SEVERITY_CRITICAL,
                    "message": f"VRAM critical: {vram_pct:.1f}% used",
                    "timestamp": now,
                    "key": "vram_crit",
                })
            elif vram_pct >= AlertPanel.VRAM_WARN_PCT and vram_pct < AlertPanel.VRAM_CRIT_PCT and "vram_warn" not in active_keys:
                active.append({
                    "severity": AlertPanel.SEVERITY_WARN,
                    "message": f"VRAM high: {vram_pct:.1f}% used",
                    "timestamp": now,
                    "key": "vram_warn",
                })
            else:
                # Condition cleared: remove VRAM alerts
                active = [a for a in active if a.get("key") not in ("vram_crit", "vram_warn")]

        # Temperature threshold checks
        if temp is not None:
            if temp >= AlertPanel.TEMP_CRIT_C and "temp_crit" not in active_keys:
                active.append({
                    "severity": AlertPanel.SEVERITY_CRITICAL,
                    "message": f"GPU temp critical: {temp} C",
                    "timestamp": now,
                    "key": "temp_crit",
                })
            elif temp >= AlertPanel.TEMP_WARN_C and temp < AlertPanel.TEMP_CRIT_C and "temp_warn" not in active_keys:
                active.append({
                    "severity": AlertPanel.SEVERITY_WARN,
                    "message": f"GPU temp high: {temp} C",
                    "timestamp": now,
                    "key": "temp_warn",
                })
            else:
                active = [a for a in active if a.get("key") not in ("temp_crit", "temp_warn")]

        # Queue depth threshold checks (include stall reason when available)
        stall_suffix = ""
        if queue_diag:
            stall_reason = queue_diag.get("stall_reason", "")
            if stall_reason:
                cooldown = queue_diag.get("cooldown_remaining", 0)
                if stall_reason == "swap_cooldown" and cooldown > 0:
                    stall_suffix = f" [{stall_reason} ({cooldown:.1f}s remaining)]"
                else:
                    stall_suffix = f" [{stall_reason}]"

        if queue_depth >= AlertPanel.QUEUE_CRIT and "queue_crit" not in active_keys:
            active.append({
                "severity": AlertPanel.SEVERITY_CRITICAL,
                "message": f"Queue critically deep: {queue_depth} pending{stall_suffix}",
                "timestamp": now,
                "key": "queue_crit",
            })
        elif queue_depth >= AlertPanel.QUEUE_WARN and queue_depth < AlertPanel.QUEUE_CRIT and "queue_warn" not in active_keys:
            active.append({
                "severity": AlertPanel.SEVERITY_WARN,
                "message": f"Queue growing: {queue_depth} pending{stall_suffix}",
                "timestamp": now,
                "key": "queue_warn",
            })
        else:
            active = [a for a in active if a.get("key") not in ("queue_crit", "queue_warn")]

        # Connection loss alert
        if not self.connected and "conn_lost" not in active_keys:
            active.append({
                "severity": AlertPanel.SEVERITY_CRITICAL,
                "message": "Connection to BASTION lost",
                "timestamp": now,
                "key": "conn_lost",
            })
        elif self.connected:
            active = [a for a in active if a.get("key") != "conn_lost"]

        # Replace alert history with pruned active list
        self.alert_history.clear()
        self.alert_history.extend(active)

    def action_refresh(self) -> None:
        """Manual refresh on 'r' key -- resets backoff."""
        self._consecutive_failures = 0
        self._next_retry_at = None
        asyncio.ensure_future(self.refresh_data())

    def action_help(self) -> None:
        """Show help overlay on 'h' key."""
        self.push_screen(HelpModal())

    def action_focus_a2a(self) -> None:
        """Scroll to A2A tasks panel on 'a' key."""
        try:
            a2a_panel = self.query_one("#a2a-tasks")
            a2a_panel.scroll_visible()
        except Exception:
            self.notify("A2A tasks panel not found", severity="warning")

    def action_focus_circuit(self) -> None:
        """Scroll to circuit breaker panel on 'c' key."""
        try:
            cb_panel = self.query_one("#circuit-breaker")
            cb_panel.scroll_visible()
        except Exception:
            self.notify("Circuit breaker panel not found", severity="warning")

    def _check_auto_fan(self) -> None:
        """Check CPU temp and auto-escalate GPU fan if needed."""
        if not self._auto_fan_enabled:
            return
        cpu_temp = _read_cpu_temp()
        if cpu_temp is None:
            return
        if cpu_temp >= self._auto_fan_threshold and self._auto_fan_state != "active":
            ok, msg = _set_fan_speed(self._auto_fan_speed)
            self._auto_fan_state = "active"
            self.notify(
                f"Auto-fan: CPU {cpu_temp:.0f}C >= {self._auto_fan_threshold}C "
                f"-> GPU fan {self._auto_fan_speed}%",
                severity="warning",
            )
        elif (
            cpu_temp < self._auto_fan_threshold - self._auto_fan_hysteresis
            and self._auto_fan_state == "active"
        ):
            ok, msg = _set_fan_speed("auto")
            self._auto_fan_state = "idle"
            self.notify(
                f"Auto-fan: CPU {cpu_temp:.0f}C cooled -> GPU fan auto",
                severity="information",
            )

    def action_fan_control(self) -> None:
        """Open fan control modal."""

        def handle_fan_result(speed: str) -> None:
            if not speed:
                return
            if speed == "toggle-auto":
                self._auto_fan_enabled = not self._auto_fan_enabled
                status = "ON" if self._auto_fan_enabled else "OFF"
                self.notify(f"Auto-fan trigger: {status}")
                # If disabling while active, reset fan to auto
                if not self._auto_fan_enabled and self._auto_fan_state == "active":
                    _set_fan_speed("auto")
                    self._auto_fan_state = "idle"
                return
            ok, msg = _set_fan_speed(speed)
            if ok:
                self.notify(f"Fan set to {speed}", severity="information")
            else:
                self.notify(f"Fan control failed: {msg}", severity="error")

        self.push_screen(FanControlModal(), handle_fan_result)

    def action_gpu_kill(self) -> None:
        """Open GPU process kill modal."""

        def handle_proc_selected(pid_str: str) -> None:
            if not pid_str:
                return
            # Find the process details for the confirmation modal
            procs = _query_gpu_processes()
            proc = next((p for p in procs if p["pid"] == pid_str), None)
            if proc is None:
                self.notify(f"PID {pid_str} no longer running", severity="warning")
                return

            def handle_kill_action(action: str) -> None:
                if not action:
                    return
                try:
                    if action == "kill":
                        subprocess.run(["kill", pid_str], check=True, timeout=5)
                        self.notify(
                            f"Killed {proc['name']} (PID {pid_str})",
                            severity="warning",
                        )
                    elif action == "kill-9":
                        subprocess.run(["kill", "-9", pid_str], check=True, timeout=5)
                        self.notify(
                            f"Force killed {proc['name']} (PID {pid_str})",
                            severity="warning",
                        )
                except subprocess.CalledProcessError:
                    self.notify(f"Failed to kill PID {pid_str}", severity="error")
                except subprocess.TimeoutExpired:
                    self.notify(f"Kill timed out for PID {pid_str}", severity="error")

            self.push_screen(
                ConfirmGPUKillModal(
                    pid=proc["pid"],
                    name=proc["name"],
                    vram_mb=proc["vram_mb"],
                ),
                handle_kill_action,
            )

        self.push_screen(GPUProcessListModal(), handle_proc_selected)

    def action_preload(self) -> None:
        """Open preload model dialog."""
        models: list[str] = []
        if self.last_data:
            for m in self.last_data.get("loaded_models", []):
                models.append(m.get("name", ""))
        # Also add some common models if available
        known = ["qwen3:8b", "qwen3:14b", "qwen3:30b-a3b", "gemma3:27b", "mistral-nemo:12b"]
        for m in known:
            if m not in models:
                models.append(m)

        async def handle_preload(model: str) -> None:
            if model:
                result = await self.client.post_preload(model)
                self.notify(f"Preload {model}: {result.get('status', 'unknown')}")

        self.push_screen(ModelSelectModal("Select model to preload:", models), handle_preload)

    def action_unload(self) -> None:
        """Open unload model dialog."""
        models: list[str] = []
        if self.last_data:
            for m in self.last_data.get("loaded_models", []):
                name = m.get("name", "")
                if name:
                    models.append(name)

        if not models:
            self.notify("No models loaded to unload", severity="warning")
            return

        async def handle_unload(model: str) -> None:
            if model:
                result = await self.client.post_unload(model)
                self.notify(f"Unload {model}: {result.get('status', 'unknown')}")

        self.push_screen(ModelSelectModal("Select model to unload:", models), handle_unload)

    def action_drain(self) -> None:
        """Toggle drain mode with confirmation."""
        is_draining = self.last_data and self.last_data.get("state") == "draining"
        action = "Resume scheduling" if is_draining else "Enter drain mode"
        details = (
            "Resume normal request scheduling?"
            if is_draining
            else "Stop accepting new requests and drain the queue?"
        )

        async def handle_drain(confirmed: bool) -> None:
            if confirmed:
                if is_draining:
                    result = await self.client.post_resume()
                else:
                    result = await self.client.post_drain()
                self.notify(f"{action}: {result.get('status', 'unknown')}")

        self.push_screen(ConfirmActionModal(action, details), handle_drain)

    def action_service_restart(self) -> None:
        """Restart bastion.service via systemctl (requires sudoers rule)."""
        action = "Restart BASTION Service"
        details = (
            "This will restart bastion.service via systemctl.\n"
            "The dashboard will lose connection briefly.\n"
            "Requires sudoers rule: see systemd/bastion-sudoers"
        )

        async def handle_restart(confirmed: bool) -> None:
            if not confirmed:
                return
            self.notify("Restarting bastion.service...", severity="warning")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo", "systemctl", "restart", "bastion.service",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=15.0,
                )
                if proc.returncode == 0:
                    self.notify("bastion.service restarted successfully")
                else:
                    error = stderr.decode().strip() if stderr else "unknown error"
                    self.notify(
                        f"Restart failed (rc={proc.returncode}): {error}",
                        severity="error",
                    )
            except asyncio.TimeoutError:
                self.notify("Restart timed out after 15s", severity="error")
            except Exception as exc:
                self.notify(f"Restart failed: {exc}", severity="error")

        self.push_screen(ConfirmActionModal(action, details), handle_restart)

    async def on_unmount(self) -> None:
        await self.client.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _detect_admin_url(base_url: str) -> str:
    """Attempt to auto-detect the admin URL from /broker/status.

    In two-port mode, the proxy port (11434) does not serve /broker/*.
    If /broker/status fails on the base URL, try the conventional admin
    port 9999 on the same host.
    """
    import httpx as _httpx

    try:
        resp = _httpx.get(f"{base_url}/broker/health", timeout=2.0)
        if resp.status_code == 200:
            return base_url
    except Exception:
        pass

    # Try conventional admin port
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    admin_url = f"{parsed.scheme}://{parsed.hostname}:9999"
    try:
        resp = _httpx.get(f"{admin_url}/broker/health", timeout=2.0)
        if resp.status_code == 200:
            return admin_url
    except Exception:
        pass

    # Fall back to original URL
    return base_url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BASTION TUI Dashboard -- real-time monitoring",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:11434",
        help="BASTION base URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--admin-url",
        default=None,
        help="Admin API URL for two-port mode (default: auto-detect from --url)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Poll interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for authentication (optional, reads from BASTION_API_KEY env if not provided)",
    )
    args = parser.parse_args()

    # Determine the admin URL for polling
    if args.admin_url:
        url = args.admin_url
    else:
        url = _detect_admin_url(args.url)

    # Get API key from args or environment
    import os
    api_key = args.api_key or os.environ.get("BASTION_API_KEY")

    app = BastionDashboard(url=url, interval=args.interval, api_key=api_key)
    app.run()


if __name__ == "__main__":
    main()
