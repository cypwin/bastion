"""Status bar widgets -- top-line summary and safety limits display."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.widgets import Static

from bastion.dashboard.helpers import state_color, temp_color, usage_color


class SafetyLimitsBar(Static):
    """Always-visible 1-line bar showing safe operating thresholds.

    Displays VRAM budget ceiling, GPU temperature safe/throttle zones,
    and CPU temperature sustained/spike limits as a dim reference line.
    """

    def __init__(
        self,
        max_vram_gb: float = 26.0,
        max_temp_c: int = 82,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.max_vram_gb: float = max_vram_gb
        self.max_temp_c: int = max_temp_c

    def update_limits(
        self,
        max_vram_gb: float | None,
        max_temp_c: int | None,
    ) -> None:
        """Update threshold values.  ``None`` or zero leaves unchanged."""
        if max_vram_gb is not None and max_vram_gb > 0:
            self.max_vram_gb = max_vram_gb
        if max_temp_c is not None and max_temp_c > 0:
            self.max_temp_c = max_temp_c

    def render(self) -> Text:  # type: ignore[override]
        """Return a dim reference line with safe operating thresholds."""
        throttle = self.max_temp_c + 5
        return Text(
            f"VRAM: <{self.max_vram_gb}GB safe"
            f" | GPU: <{self.max_temp_c}\u00b0C OK,"
            f" {throttle}\u00b0C throttle"
            " | CPU: <85\u00b0C sustained, <95\u00b0C spike",
            style="dim",
        )


class StatusBar(Static):
    """Compact top-line status bar (absorbs old ConnectionPanel).

    Shows the BASTION brand badge, connection indicator, optional STALE
    badge, GPU temp, VRAM used/total, scheduler state, layout mode tag,
    and current time.
    """

    def render_status(
        self,
        data: dict[str, Any] | None,
        connected: bool,
        stale: bool = False,
        last_ok_time: str | None = None,
        consecutive_failures: int = 0,
        layout_mode: str = "standard",
    ) -> Text:
        """Build the status line ``Text`` from current broker data."""
        now = datetime.now().strftime("%H:%M:%S")
        line = Text()

        # Brand badge
        line.append(" BASTION ", style="bold white on dark_blue")
        line.append("  ")

        # Connection indicator
        if connected:
            line.append("[*]", style="green bold")
        else:
            line.append("[X]", style="red bold")
        line.append(" ")

        # STALE badge when disconnected but have cached data
        if stale:
            line.append(" STALE ", style="bold white on red")
            line.append(" ")

        # Early exit when we have no data at all
        if not connected and data is None:
            line.append("DISCONNECTED", style="red bold")
            if consecutive_failures > 0:
                line.append(f" ({consecutive_failures} failures)", style="red")
            line.append(f"  {now}", style="dim")
            return line

        if data is None:
            line.append(f"  {now}", style="dim")
            return line

        # GPU temperature
        gpu = data.get("gpu", {})
        temp = gpu.get("temperature_c")
        if temp is not None:
            line.append(f"GPU: {temp}\u00b0C", style=temp_color(temp))
        else:
            line.append("GPU: n/a", style="dim")
        line.append("  |  ")

        # VRAM used/total
        used = gpu.get("vram_used_mb")
        total = gpu.get("vram_total_mb")
        if used is not None and total is not None:
            pct = used / total * 100 if total else None
            line.append(
                f"VRAM: {used / 1024:.1f}/{total / 1024:.0f}GB",
                style=usage_color(pct),
            )
        else:
            line.append("VRAM: n/a", style="dim")
        line.append("  |  ")

        # Scheduler state
        state = data.get("state", "?")
        line.append(f"State: {state}", style=state_color(state))
        line.append("  |  ")

        # Layout mode tag
        line.append(f"[{layout_mode}]", style="dim italic")

        # Timestamp
        line.append(f"  {now}", style="dim")

        # Last successful poll hint
        if last_ok_time:
            line.append(f"  (last OK: {last_ok_time})", style="dim")

        return line
