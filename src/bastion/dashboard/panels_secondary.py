"""Trace, A2A task, lease, and audit stream panels."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.table import Table
from rich.text import Text

from bastion.dashboard.helpers import (
    a2a_state_color,
    format_countdown,
    lease_state_color,
)
from bastion.dashboard.widgets import BastionPanel


class TracePanel(BastionPanel):
    """Live request trace viewer showing recent requests."""

    def render_data(self, recent: list[dict]) -> Table:
        table = Table(title="Request Trace", expand=True, show_edge=False, pad_edge=False)
        table.add_column("Time", width=8)
        table.add_column("Model", ratio=2)
        table.add_column("Source", width=10)
        table.add_column("Tier", width=6)
        table.add_column("Wait", width=5, justify="right")
        table.add_column("Dur", width=5, justify="right")
        table.add_column("St", width=3, justify="right")

        if not recent:
            table.add_row(Text("(no requests)", style="dim"), "", "", "", "", "", "")
        else:
            for req in recent[:20]:  # Show last 20 in the panel
                ts = datetime.fromtimestamp(req.get("timestamp", 0)).strftime("%H:%M:%S")
                model = req.get("model", "?")
                # Truncate long model names
                if len(model) > 15:
                    model = model[:12] + "..."
                # Declared identity (X-Agent-ID) or User-Agent product token.
                source = req.get("source") or "-"
                if len(source) > 10:
                    source = source[:9] + "…"
                tier = req.get("tier", "?")[:4]
                wait = f"{req.get('queue_wait_s', 0):.1f}s"
                dur = f"{req.get('duration_s', 0):.1f}s"
                status = str(req.get("status_code", "?"))
                style = "green" if status == "200" else "red"
                table.add_row(
                    ts, model, Text(source, style="dim" if source == "-" else ""),
                    tier, wait, dur, Text(status, style=style),
                )

        return table


class A2ATaskPanel(BastionPanel):
    """Active A2A tasks display showing state and skill types."""

    def render_data(self, status_data: dict[str, Any]) -> Table:
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


class LeasePanel(BastionPanel):
    """Active model leases/reservations panel."""

    def render_data(self, status_data: dict[str, Any]) -> Table:
        table = Table(title="Leases", expand=True, show_edge=False, pad_edge=False)
        table.add_column("key", style="bold", width=12)
        table.add_column("value")

        leases = status_data.get("active_leases", [])

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


class AuditStreamPanel(BastionPanel):
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
