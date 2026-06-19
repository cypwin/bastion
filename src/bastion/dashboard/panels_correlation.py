"""Correlation-engine dashboard panel (observability spec 6.1/6.3/6.4/6.5).

``CorrelationPanel`` is the always-on TUI surface for the correlation engine's
synthesized intelligence: the composite RiskIndex (score + dominant factor),
CPU<->GPU thermal coupling (active + minimum headroom), the most-recent discrete
contention events (the moat — a host pressure spike joined to an inference
stall), the enriched stall reason, and a short tail of the unified correlation
ring.

Per ADR-005 it is a **direct dict-accessor** panel — ``render_data`` takes the
plain ``CorrelationState`` ``model_dump()`` dict (the ``correlation`` leg of the
``MachineSnapshot`` served by ``/broker/snapshot``) and tolerates a ``None`` /
partial / all-empty payload without crashing. Every term is guarded for ``None``
so a no-GPU host (no thermal coupling) or a quiet host (no contention, no stall)
renders cleanly — never an empty box and never a misleading ``0``.

This module imports **only** ``bastion.dashboard.widgets`` (the panel base) —
never ``bastion.correlation`` and never the broker internals; the engine pushes
nothing into the TUI, the TUI polls the snapshot (ADR-005).
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from bastion.dashboard.widgets import BastionPanel

# Risk-level colours (spec 6.4 — forward-looking "risk approaching, not crash").
_RISK_LEVEL_STYLE = {
    "nominal": "green",
    "elevated": "yellow",
    "high": "orange1",
    "critical": "red",
}

# Domain colours for the ring tail (one of gpu/system/inference/scheduler).
_DOMAIN_STYLE = {
    "gpu": "magenta",
    "system": "yellow",
    "inference": "cyan",
    "scheduler": "blue",
}


def _headroom_style(headroom_c: float | None) -> str:
    """Colour the thermal-headroom value: red <=5C, yellow <=12C, else green."""
    if headroom_c is None:
        return "dim"
    if headroom_c <= 5.0:
        return "red"
    if headroom_c <= 12.0:
        return "yellow"
    return "green"


class CorrelationPanel(BastionPanel):
    """RiskIndex / thermal-coupling / contention / stall / ring sections."""

    _MAX_CONTENTIONS = 5  # last-N discrete events (spec 6.3 TUI = last 5)
    _MAX_RING = 6  # short ring tail; full ring rides /broker/snapshot?include_ring
    _MAX_COMPONENTS = 3  # top contributing RiskIndex components shown inline

    def render_data(self, state: dict | None = None) -> Table:
        table = Table(title="Correlation", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=14)
        table.add_column("value")

        if not state:
            table.add_row(Text("(no data)", style="dim"), "")
            return table

        self._render_risk(table, state)
        self._render_thermal(table, state)
        self._render_contentions(table, state)
        self._render_stall(table, state)
        self._render_ring(table, state)

        if table.row_count == 0:
            table.add_row(Text("(no data)", style="dim"), "")
        return table

    # ------------------------------------------------------------------
    # RiskIndex — composite score + dominant factor + top components
    # ------------------------------------------------------------------

    def _render_risk(self, table: Table, state: dict) -> None:
        risk = state.get("risk_index")
        if not risk:
            return
        score = risk.get("score")
        level = risk.get("level") or "nominal"
        dominant = risk.get("dominant_factor") or "-"
        style = _RISK_LEVEL_STYLE.get(str(level), "white")
        score_txt = f"{score:.2f}" if isinstance(score, (int, float)) else "-"
        table.add_row(
            Text("Risk", style="bold"),
            Text(f"{score_txt} {level} (dom: {dominant})", style=style),
        )
        # Show the top few measured components by score so the operator sees
        # what is driving the composite.
        comps = risk.get("component_scores") or {}
        if isinstance(comps, dict) and comps:
            top = sorted(comps.items(), key=lambda kv: kv[1], reverse=True)
            for name, val in top[: self._MAX_COMPONENTS]:
                if not isinstance(val, (int, float)):
                    continue
                table.add_row(
                    Text(f"  {name}", style="dim"),
                    Text(f"{val:.2f}", style="dim"),
                )

    # ------------------------------------------------------------------
    # Thermal coupling — CPU/GPU temps, coupling flag, min headroom
    # ------------------------------------------------------------------

    def _render_thermal(self, table: Table, state: dict) -> None:
        tc = state.get("thermal_coupling")
        if not tc:
            return
        headroom = tc.get("thermal_headroom_min_c")
        active = bool(tc.get("coupling_active"))
        cpu = tc.get("cpu_temp_c")
        gpu = tc.get("gpu_temp_c")
        parts: list[str] = []
        if cpu is not None:
            parts.append(f"cpu={cpu:.0f}C")
        if gpu is not None:
            parts.append(f"gpu={gpu:.0f}C")
        parts.append("coupled" if active else "decoupled")
        head_txt = (
            f"headroom={headroom:.0f}C"
            if isinstance(headroom, (int, float))
            else "headroom=-"
        )
        table.add_row(
            Text("Thermal", style="bold"),
            Text(" ".join(parts), style="white"),
        )
        table.add_row(
            Text("  headroom", style="dim"),
            Text(head_txt.split("=", 1)[1], style=_headroom_style(headroom)),
        )

    # ------------------------------------------------------------------
    # Contention events — the moat: pressure joined to an inference stall
    # ------------------------------------------------------------------

    def _render_contentions(self, table: Table, state: dict) -> None:
        events = state.get("recent_contentions") or []
        if not events:
            return
        table.add_row(Text("Contention", style="bold"), "")
        # Newest last; show the most recent few.
        for ev in list(events)[-self._MAX_CONTENTIONS:]:
            kind = ev.get("kind") or "?"
            attribution = ev.get("attribution") or ""
            stalled = ev.get("inference_was_stalled")
            style = "red" if stalled else "yellow"
            table.add_row(
                Text(f"  {kind}", style=style),
                Text(attribution[:40], style=style),
            )

    # ------------------------------------------------------------------
    # Enriched stall reason — the live-context string (auto in SchedulerPanel
    # too, but surfaced here for the correlation-focused view)
    # ------------------------------------------------------------------

    def _render_stall(self, table: Table, state: dict) -> None:
        reason = state.get("enriched_stall_reason")
        if not reason:
            return
        table.add_row(
            Text("Stall", style="bold"),
            Text(str(reason)[:46], style="yellow"),
        )

    # ------------------------------------------------------------------
    # Ring tail — short chronological window of the unified event timeline
    # ------------------------------------------------------------------

    def _render_ring(self, table: Table, state: dict) -> None:
        ring_size = state.get("ring_size") or 0
        events = state.get("recent_ring_events") or []
        header = f"Ring ({ring_size})"
        table.add_row(Text(header, style="bold"), "")
        if not events:
            table.add_row(Text("  (empty)", style="dim"), "")
            return
        # Oldest-first tail; show the most recent few.
        for ev in list(events)[-self._MAX_RING:]:
            domain = ev.get("domain") or "?"
            kind = ev.get("kind") or "?"
            style = _DOMAIN_STYLE.get(str(domain), "white")
            table.add_row(
                Text(f"  {domain}", style=style),
                Text(kind, style=style),
            )
