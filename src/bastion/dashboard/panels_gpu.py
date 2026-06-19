"""GPU, models, and VRAM ledger panels."""
from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text

from bastion.dashboard.helpers import (
    SPARKLINE_WIDTH,
    format_bytes_gb,
    format_bytes_mb,
    sparkline,
    sparkline_abs,
    temp_color,
    usage_color,
    vram_bar,
)
from bastion.dashboard.widgets import BastionPanel


class GPUPanel(BastionPanel):
    """GPU temperature, VRAM, and power status."""

    def render_data(
        self,
        data: dict[str, Any],
        power_history: list[float] | None = None,
        vram_total_mb: float | None = None,
        gpu_ceiling_c: int = 85,
    ) -> Table:
        gpu = data.get("gpu", {})
        temp = gpu.get("temperature_c")
        used = gpu.get("vram_used_mb")
        total = gpu.get("vram_total_mb")
        power = gpu.get("power_draw_watts")
        pct = (used / total * 100) if used is not None and total else None
        w = SPARKLINE_WIDTH

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

        # Sparkline rows
        app = self.app
        if hasattr(app, "vram_history") and app.vram_history:
            vram_spark = (
                sparkline_abs(list(app.vram_history), lo_bound=0, hi_bound=vram_total_mb, width=w)
                if vram_total_mb is not None
                else sparkline(list(app.vram_history), w)
            )
            table.add_row(
                "VRAM  \u2581\u2582",
                Text(vram_spark, style="cyan"),
            )
        if hasattr(app, "temp_history") and app.temp_history:
            table.add_row(
                "Temp  \u2581\u2582",
                Text(
                    sparkline_abs(
                        list(app.temp_history), lo_bound=20, hi_bound=gpu_ceiling_c, width=w
                    ),
                    style=temp_color(temp),
                ),
            )
        if power_history:
            table.add_row(
                "Power \u2581\u2582",
                Text(sparkline(power_history, w), style="yellow"),
            )

        return table


class ModelsPanel(BastionPanel):
    """Currently loaded models in Ollama."""

    def render_data(self, data: dict[str, Any]) -> Table:
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


class VRAMLedgerPanel(BastionPanel):
    """VRAM budget panel showing VRAMManager's allocated/reserved ledger."""

    def _measured_used_mb_from_app(self) -> float | None:
        """Latest nvidia-smi VRAM (MB) from app state, or None if unavailable.

        Mirrors how :class:`GPUPanel` reads ``app.vram_history``; lets the panel
        show the Measured/Δ rows in production without the refresh loop having to
        thread the value in explicitly. Guarded so unit tests (unmounted panels,
        or a harness without history) simply get ``None``.
        """
        try:
            app = self.app
        except Exception:
            return None
        history = getattr(app, "vram_history", None)
        if history:
            return history[-1]
        return None

    def render_data(
        self, ledger: dict[str, Any], measured_used_mb: float | None = None
    ) -> Table:
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

        # Measured (nvidia-smi) vs reserved (ledger) — makes the gap explicit.
        if measured_used_mb is None:
            measured_used_mb = self._measured_used_mb_from_app()
        if measured_used_mb is not None:
            measured_bytes = int(measured_used_mb * 1024 * 1024)
            table.add_row("Measured", Text(format_bytes_gb(measured_bytes), style="cyan"))
            tracked = (allocated or 0) + (reserved or 0)
            delta = measured_bytes - tracked
            sign = "+" if delta >= 0 else "-"
            table.add_row(
                "Δ overhead",
                Text(f"{sign}{format_bytes_gb(int(abs(delta)))}", style="yellow"),
            )

        # Show utilization bar (safety margin shown separately, not counted here)
        if total and total > 0:
            used = (allocated or 0) + (reserved or 0)
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
                Text(
                    f"{format_bytes_mb(vram_bytes)} {status_str} ({age:.0f}s)",
                    style=status_style,
                ),
            )

        return table
