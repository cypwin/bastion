"""System-level dashboard panels: Temperature, Memory, CPU, Network."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text

from bastion.dashboard.helpers import (
    core_char,
    get_rate,
    sparkline,
    usage_color,
)
from bastion.dashboard.widgets import BastionPanel


class TemperaturePanel(BastionPanel):
    """Displays CPU, NVMe, and GPU temperatures with status indicators."""

    def render_data(
        self,
        cpu_temp: int | None = None,
        nvme_temps: list[int] | None = None,
        gpu_temp: int | None = None,
        gpu_ceiling_c: int = 85,
    ) -> Table:
        table = Table(title="Temperatures", expand=True, show_header=True)
        table.add_column("Component", style="cyan")
        table.add_column("Temp", justify="right")
        table.add_column("Status", width=3)

        has_any = False

        if cpu_temp is not None:
            has_any = True
            if cpu_temp >= 90:
                style, status = "red bold", "!"
            elif cpu_temp >= 75:
                style, status = "yellow", "?"
            else:
                style, status = "green", "ok"
            table.add_row("CPU", f"[{style}]{cpu_temp}\u00b0C[/]", f"[{style}]{status}[/]")

        if nvme_temps:
            has_any = True
            for i, t in enumerate(nvme_temps):
                if t >= 75:
                    style, status = "red bold", "!"
                elif t >= 60:
                    style, status = "yellow", "?"
                else:
                    style, status = "green", "ok"
                label = f"NVMe{i}" if len(nvme_temps) > 1 else "NVMe"
                table.add_row(label, f"[{style}]{t}\u00b0C[/]", f"[{style}]{status}[/]")

        if gpu_temp is not None:
            has_any = True
            # Crit at or above profile ceiling; warn within 8\u00b0C below it.
            crit = gpu_ceiling_c
            warn = crit - 8
            if gpu_temp >= crit:
                style, status = "red bold", "!"
            elif gpu_temp >= warn:
                style, status = "yellow bold", "?"
            else:
                style, status = "green", "ok"
            table.add_row("GPU", f"[{style}]{gpu_temp}\u00b0C[/]", f"[{style}]{status}[/]")

        if not has_any:
            table.add_row("[dim](no sensors)[/]", "", "")

        return table


class MemoryPanel(BastionPanel):
    """Displays RAM and swap usage."""

    def render_data(self, mem: dict | None = None) -> Table:
        table = Table(title="Memory", expand=True, show_header=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        if mem is None:
            table.add_row("[dim]no data[/]", "")
            return table

        used = mem.get("used_gb", 0.0)
        total = mem.get("total_gb", 0.0)
        available = mem.get("available_gb", 0.0)
        pct = (used / total * 100) if total > 0 else 0.0
        color = usage_color(pct)

        table.add_row("RAM", f"[{color}]{used:.1f} / {total:.1f} GB ({pct:.0f}%)[/]")
        table.add_row("Available", f"[{color}]{available:.1f} GB[/]")

        swap = mem.get("swap_gb", 0.0)
        if swap > 0.01:
            swap_total = mem.get("swap_total_gb", 0.0)
            swap_pct = (swap / swap_total * 100) if swap_total > 0 else 0.0
            swap_color = usage_color(swap_pct)
            table.add_row(
                "Swap", f"[{swap_color}]{swap:.1f} / {swap_total:.1f} GB[/]"
            )

        return table


class CPUPanel(BastionPanel):
    """Displays CPU usage, load average, frequency, per-core map, and top processes."""

    def render_data(
        self,
        cpu_data: dict,
        cpu_history: list[float] | None = None,
        processes: list[dict] | None = None,
    ) -> Table:
        table = Table(title="CPU / Processes", expand=True, show_header=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        # Overall CPU %
        overall = cpu_data.get("overall_pct", 0.0)
        color = usage_color(overall)
        trend = f" {sparkline(cpu_history)}" if cpu_history else ""
        table.add_row("CPU", f"[{color}]{overall:.1f}%[/]{trend}")

        # Load average
        load = cpu_data.get("load_avg")
        if load and len(load) >= 3:
            table.add_row("Load", f"{load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}")

        # Frequency
        freq = cpu_data.get("freq_mhz")
        if freq is not None:
            table.add_row("Freq", f"{freq:.0f} MHz")

        # Per-core visualization
        per_core = cpu_data.get("per_core")
        if per_core:
            cores_text = Text()
            for pct in per_core:
                ch, style = core_char(pct)
                cores_text.append(ch, style=style)
            table.add_row("Cores", cores_text)

        # Top processes
        if processes:
            table.add_row("", "")  # spacer
            for proc in processes[:6]:
                name = proc.get("name", "?")
                cpu_pct = proc.get("cpu_pct", 0.0)
                mem_mb = proc.get("mem_mb", 0.0)
                table.add_row(
                    f"  {name}", f"{cpu_pct:5.1f}%  {mem_mb:6.0f}MB"
                )

        return table


class NetworkPanel(BastionPanel):
    """Displays network throughput and totals."""

    def render_data(
        self,
        net_data: dict,
        recv_history: list[float] | None = None,
        sent_history: list[float] | None = None,
    ) -> Table:
        table = Table(title="Network", expand=True, show_header=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        # Download rate
        recv_rate = net_data.get("recv_bytes_sec", 0.0)
        recv_trend = f" {sparkline(recv_history)}" if recv_history else ""
        table.add_row(
            "Down", f"[green]{get_rate(recv_rate)}[/]{recv_trend}"
        )

        # Upload rate
        sent_rate = net_data.get("sent_bytes_sec", 0.0)
        sent_trend = f" {sparkline(sent_history)}" if sent_history else ""
        table.add_row(
            "Up", f"[yellow]{get_rate(sent_rate)}[/]{sent_trend}"
        )

        # Totals
        total_recv = net_data.get("total_recv_bytes", 0.0)
        total_sent = net_data.get("total_sent_bytes", 0.0)
        recv_gb = total_recv / (1024 * 1024 * 1024)
        sent_gb = total_sent / (1024 * 1024 * 1024)
        table.add_row("Total D/U", f"{recv_gb:.2f} / {sent_gb:.2f} GB")

        return table


class ContentionPanel(BastionPanel):
    """Host-pressure panel: PSI, swap rate, block-device IO, CPU power, OOM.

    A host/system-level panel (peer to MemoryPanel/NetworkPanel, spec 5.3) that
    renders the ``ContentionSnapshot`` (spec 4.4) served by ``/broker/contention``.
    Per ADR-005 it is a direct dict-accessor: ``render_data`` takes the plain
    ``model_dump()`` dict and tolerates a ``None`` / partial / all-``None``
    payload without crashing — every leg is independently guarded so the no-PSI
    container / no-powercap / non-NVMe host (all ``None``/``[]``) renders cleanly
    and never shows a misleading ``0`` row.
    """

    # Block-device cap so a many-drive host cannot make the panel unbounded.
    _MAX_DEVICE_ROWS = 6

    def render_data(
        self,
        contention: dict | None = None,
        psi_io_full_warn_pct: float = 5.0,
        psi_io_full_crit_pct: float = 25.0,
    ) -> Table:
        table = Table(title="Contention", expand=True, show_header=True)
        table.add_column("Signal", style="cyan")
        table.add_column("Value", justify="right")
        table.add_column("St", width=3)

        if not contention:
            table.add_row("[dim]no data[/]", "", "")
            return table

        has_any = False

        # ── PSI (some/full avg10) — io is the most load-bearing leg ──────
        def _psi_row(label: str, value: float | None, warn: float, crit: float) -> None:
            nonlocal has_any
            if value is None:
                return
            has_any = True
            if value >= crit:
                style, st = "red bold", "!"
            elif value >= warn:
                style, st = "yellow", "?"
            else:
                style, st = "green", "ok"
            table.add_row(label, f"[{style}]{value:.1f}[/]", f"[{style}]{st}[/]")

        _psi_row(
            "PSI io some",
            contention.get("psi_io_some_avg10"),
            psi_io_full_warn_pct,
            psi_io_full_crit_pct,
        )
        _psi_row(
            "PSI io full",
            contention.get("psi_io_full_avg10"),
            psi_io_full_warn_pct,
            psi_io_full_crit_pct,
        )
        _psi_row("PSI cpu some", contention.get("psi_cpu_some_avg10"), 20.0, 60.0)
        _psi_row("PSI mem some", contention.get("psi_mem_some_avg10"), 20.0, 60.0)

        # ── Swap in/out rate (MB/s): yellow > 0.1, red > 5 ───────────────
        def _swap_row(label: str, value: float | None) -> None:
            nonlocal has_any
            if value is None:
                return
            has_any = True
            if value > 5.0:
                style, st = "red bold", "!"
            elif value > 0.1:
                style, st = "yellow", "?"
            else:
                style, st = "green", "ok"
            table.add_row(label, f"[{style}]{value:.1f} MB/s[/]", f"[{style}]{st}[/]")

        _swap_row("Swap in", contention.get("swap_in_rate_mb_s"))
        _swap_row("Swap out", contention.get("swap_out_rate_mb_s"))

        # ── Block-device util% + await (red > 80, yellow > 50) ───────────
        devices = contention.get("block_devices") or []
        for dev in devices[: self._MAX_DEVICE_ROWS]:
            has_any = True
            util = dev.get("util_pct", 0.0) or 0.0
            if util > 80.0:
                style, st = "red bold", "!"
            elif util > 50.0:
                style, st = "yellow", "?"
            else:
                style, st = "green", "ok"
            write_await = dev.get("write_await_ms")
            await_txt = f" w{write_await:.0f}ms" if write_await is not None else ""
            name = dev.get("device", "?")
            table.add_row(
                name, f"[{style}]{util:.0f}%[/]{await_txt}", f"[{style}]{st}[/]"
            )
        if len(devices) > self._MAX_DEVICE_ROWS:
            extra = len(devices) - self._MAX_DEVICE_ROWS
            table.add_row(f"[dim]... {extra} more[/]", "", "")

        # ── CPU package power (host RAPL, Intel or AMD) ──────────────────
        cpu_w = contention.get("cpu_package_watts")
        if cpu_w is not None:
            has_any = True
            table.add_row("CPU pkg", f"{cpu_w:.1f} W", "")

        # ── OOM kills: red row when the rate is positive ─────────────────
        oom_rate = contention.get("oom_kill_rate")
        oom_total = contention.get("oom_kill_total")
        if oom_rate is not None and oom_rate > 0:
            has_any = True
            table.add_row(
                "OOM kills", f"[red bold]{oom_total}[/]", "[red bold]![/]"
            )
        elif oom_total is not None:
            has_any = True
            table.add_row("OOM kills", f"[green]{oom_total}[/]", "[green]ok[/]")

        if not has_any:
            table.add_row("[dim](no pressure data)[/]", "", "")

        return table
