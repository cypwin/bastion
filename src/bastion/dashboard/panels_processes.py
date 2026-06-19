"""Process-attribution dashboard panel (observability spec 5.3 / 4.5).

``ProcessAttributionPanel`` is the always-on TUI surface that promotes the
per-process GPU/CPU/mem/IO data out of the modal kill-dialog: it shows the
individual contenders (vs the existing aggregate panels), tags inference-owned
PIDs by role badge, and pins a user watchlist and a churn log.

Per ADR-005 it is a **direct dict-accessor** panel — ``render_data`` takes the
plain ``ProcessSnapshot`` ``model_dump()`` dict served by ``/broker/processes``
and tolerates a ``None`` / partial / all-empty payload without crashing. The
GPU section is empty on a ``StubBackend`` / no-GPU host and renders
``(no GPU)`` rather than a broken section. This data is **TUI + JSON only** —
it is never a Prometheus label (Constraint #2).
"""

from __future__ import annotations

import time

from rich.table import Table
from rich.text import Text

from bastion.dashboard.widgets import BastionPanel


def _role_style(is_owned: bool, role: str | None) -> str:
    """Role-badge colour: inference cyan, competitors plain (4.5 colour code)."""
    if is_owned:
        return "cyan"
    return "white"


def _fmt_bytes_s(value: float | None) -> str:
    """Human-readable bytes/s, or ``-`` when the source denied the read."""
    if value is None:
        return "-"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f}M/s"
    if value >= 1024:
        return f"{value / 1024:.0f}K/s"
    return f"{value:.0f}B/s"


class ProcessAttributionPanel(BastionPanel):
    """Per-process attribution: GPU / CPU-IO / watchlist / churn sections."""

    # Per-section row caps (spec 5.3), matching LeasePanel/A2ATaskPanel.
    _MAX_GPU = 6
    _MAX_CPU = 8
    _MAX_WATCH = 5
    _MAX_CHURN = 3
    # GPU sub-data older than this (s) is annotated dim/stale (spec 5.3).
    _GPU_STALE_S = 20.0

    def render_data(self, snapshot: dict | None = None) -> Table:
        table = Table(title="Processes", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=14)
        table.add_column("value")

        if not snapshot:
            table.add_row(Text("(no data)", style="dim"), "")
            return table

        self._render_gpu_section(table, snapshot)
        self._render_cpu_section(table, snapshot)
        self._render_watchlist_section(table, snapshot)
        self._render_churn_section(table, snapshot)

        # If nothing at all populated (every list empty), keep a no-data marker
        # so the panel never renders as an empty box.
        if table.row_count == 0:
            table.add_row(Text("(no data)", style="dim"), "")
        return table

    # ------------------------------------------------------------------
    # GPU section — VRAM/SM% per inference-owned vs competitor PID
    # ------------------------------------------------------------------

    def _render_gpu_section(self, table: Table, snapshot: dict) -> None:
        gpu_rows = snapshot.get("gpu_processes") or []
        # Stale annotation: GPU sub-data is refreshed on the 10s slow tick; if it
        # is older than the threshold, dim the header so the operator knows.
        collected = snapshot.get("gpu_collected_at")
        stale = (
            collected is not None
            and (time.time() - float(collected)) > self._GPU_STALE_S
        )
        header = "GPU" + (" [stale]" if stale else "")
        table.add_row(Text(header, style="bold dim" if stale else "bold"), "")

        if not gpu_rows:
            # StubBackend / no-GPU host — the correct complete empty value.
            table.add_row(Text("  (no GPU)", style="dim"), "")
            return

        for row in gpu_rows[: self._MAX_GPU]:
            pid = row.get("pid", "?")
            name = (row.get("name") or "?")[:14]
            vram = row.get("vram_mb")
            sm = row.get("sm_pct")
            style = _role_style(
                bool(row.get("is_inference_owned")), row.get("role")
            )
            if stale:
                style = "dim"
            parts: list[str] = []
            if vram is not None:
                parts.append(f"{vram}MB")
            if sm is not None:
                parts.append(f"sm={sm}%")
            table.add_row(
                Text(f"  {name}", style=style),
                Text(f"pid={pid} " + " ".join(parts), style=style),
            )
        extra = len(gpu_rows) - self._MAX_GPU
        if extra > 0:
            table.add_row("", Text(f"... {extra} more", style="dim"))

    # ------------------------------------------------------------------
    # CPU/IO section — top-N by CPU+memory with io bytes/s attribution
    # ------------------------------------------------------------------

    def _render_cpu_section(self, table: Table, snapshot: dict) -> None:
        rows = snapshot.get("top_processes") or []
        if not rows:
            return
        table.add_row(Text("CPU/IO", style="bold"), "")
        for row in rows[: self._MAX_CPU]:
            pid = row.get("pid", "?")
            name = (row.get("name") or "?")[:14]
            cpu = row.get("cpu_pct")
            rss = row.get("rss_mb")
            io_r = row.get("io_read_bytes_s")
            io_w = row.get("io_write_bytes_s")
            style = _role_style(
                bool(row.get("is_inference_owned")), row.get("role")
            )
            parts: list[str] = []
            if cpu is not None:
                parts.append(f"cpu={cpu:.0f}%")
            if rss is not None:
                parts.append(f"{rss:.0f}MB")
            # IO is attribution-critical: an NVMe burst from a low-CPU process is
            # the real PCIe-stall cause. Show '-' (not 0) when the read was denied.
            parts.append(f"r={_fmt_bytes_s(io_r)}")
            parts.append(f"w={_fmt_bytes_s(io_w)}")
            table.add_row(
                Text(f"  {name}", style=style),
                Text(f"pid={pid} " + " ".join(parts), style=style),
            )
        extra = len(rows) - self._MAX_CPU
        if extra > 0:
            table.add_row("", Text(f"... {extra} more", style="dim"))

    # ------------------------------------------------------------------
    # Watchlist section — always pinned regardless of rank
    # ------------------------------------------------------------------

    def _render_watchlist_section(self, table: Table, snapshot: dict) -> None:
        hits = snapshot.get("watchlist_hits") or []
        if not hits:
            return
        table.add_row(Text("Watchlist", style="bold"), "")
        for row in hits[: self._MAX_WATCH]:
            pid = row.get("pid", "?")
            name = (row.get("name") or "?")[:14]
            cpu = row.get("cpu_pct")
            cpu_str = f"cpu={cpu:.0f}%" if cpu is not None else ""
            table.add_row(
                Text(f"  {name}", style="yellow"),
                Text(f"pid={pid} {cpu_str}", style="yellow"),
            )
        extra = len(hits) - self._MAX_WATCH
        if extra > 0:
            table.add_row("", Text(f"... {extra} more", style="dim"))

    # ------------------------------------------------------------------
    # Churn section — recent burst-spawn events
    # ------------------------------------------------------------------

    def _render_churn_section(self, table: Table, snapshot: dict) -> None:
        events = snapshot.get("recent_churn_events") or []
        if not events:
            return
        table.add_row(Text("Churn", style="bold"), "")
        # Newest last in the deque; show the most recent few.
        for ev in list(events)[-self._MAX_CHURN:]:
            new_count = ev.get("new_count", 0)
            exited = ev.get("exited_count", 0)
            names = ev.get("new_names") or []
            names_str = ",".join(n[:10] for n in names[:3])
            table.add_row(
                Text(f"  +{new_count}/-{exited}", style="magenta"),
                Text(names_str, style="dim"),
            )
