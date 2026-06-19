"""Shared, dependency-light constants and helpers for BASTION.

This module is a deliberately thin shared boundary: it must import **nothing**
from the heavier subsystems (the Textual TUI ``bastion.dashboard.app``, the
correlation engine, the server) so that both the TUI and the correlation engine
can reuse it without creating a circular import.

The auto-fan escalation curve lives here (moved out of
``bastion.dashboard.app``) so the correlation engine can derive
``ThermalCoupling.coupling_active`` from the *definitive* fan curve
(``coupling_active = cpu_temp_c is not None and _fan_band(cpu_temp_c) is not
None``) without importing the app (ADR-005: the TUI is a client, not a peer of
broker internals — the dependency must never point app -> engine or engine ->
app). See design spec 2026-06-19 Section 6.5.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Auto-fan constants (moved from dashboard/app.py — single source of truth)
# ---------------------------------------------------------------------------

# Escalation curve (operator spec 2026-06-12): the top band is exclusive
# ("over 85C"), the rest inclusive. Below 60C the fan returns to BIOS auto.
# De-escalation waits until the temperature sits _AUTO_FAN_HYSTERESIS_C below
# the hotter band's trigger, so boundary hovering doesn't oscillate the fan.
_AUTO_FAN_HYSTERESIS_C = 5.0


def _fan_band(temp_c: float) -> str | None:
    """Target fan speed (in %) for ``temp_c``; ``None`` means BIOS auto."""
    if temp_c > 85.0:
        return "100"
    if temp_c >= 80.0:
        return "90"
    if temp_c >= 70.0:
        return "50"
    if temp_c >= 60.0:
        return "30"
    return None
