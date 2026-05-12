"""Color helpers, formatters, sparkline, and visualization utilities."""
from __future__ import annotations

from rich.text import Text

# ---------------------------------------------------------------------------
# Sparkline / history configuration (overridable via CLI --sparkline-width)
# ---------------------------------------------------------------------------

SPARKLINE_WIDTH: int = 20      # Characters shown per sparkline
HISTORY_LEN: int = 120         # Samples kept in history deques (~2 min at 1Hz)

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def temp_color(temp: int | None) -> str:
    """Return a rich color string for GPU temperature."""
    if temp is None:
        return "dim"
    if temp < 50:
        return "green"
    if temp < 70:
        return "yellow"
    if temp < 80:
        return "yellow bold"
    return "red bold"


def usage_color(pct: float | None) -> str:
    """Return a rich color string for utilization percentage."""
    if pct is None:
        return "dim"
    if pct < 50:
        return "green"
    if pct < 75:
        return "yellow"
    if pct < 90:
        return "yellow bold"
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


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

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


def format_bytes_gb(b: int | None) -> str:
    """Format bytes as GB string."""
    if b is None:
        return "n/a"
    return f"{b / (1024 * 1024 * 1024):.1f}GB"


def format_bytes_mb(b: int | None) -> str:
    """Format bytes as MB string."""
    if b is None:
        return "n/a"
    return f"{b / (1024 * 1024):.0f}MB"


# ---------------------------------------------------------------------------
# Sparkline and VRAM bar
# ---------------------------------------------------------------------------

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


def sparkline_abs(
    values: list[float],
    lo_bound: float,
    hi_bound: float,
    width: int = 20,
) -> str:
    """Render a sparkline normalized against fixed absolute bounds.

    Unlike sparkline(), this uses (lo_bound, hi_bound) as the y-axis range
    so trend shape conveys absolute level, not local range. Values below
    lo_bound render as blank; values above hi_bound clamp to a full block.

    Use for metrics with semantic ceilings (VRAM vs total, temp vs profile
    ceiling). Use sparkline() for metrics where only local trend matters.
    """
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    span = hi_bound - lo_bound if hi_bound != lo_bound else 1.0
    recent = values[-width:]
    result = []
    for v in recent:
        clamped = max(lo_bound, min(hi_bound, v))
        idx = min(int((clamped - lo_bound) / span * 8), 8)
        result.append(blocks[idx])
    return "".join(result)


def vram_bar(used_mb: int | None, total_mb: int | None, width: int = 16) -> Text:
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


# ---------------------------------------------------------------------------
# New v2 helpers
# ---------------------------------------------------------------------------

def core_char(pct: float) -> tuple[str, str]:
    """Return (character, style) for CPU core visualization.

    Thresholds:
        >= 90%  -> '#' / red
        >= 70%  -> '=' / yellow
        >= 30%  -> '-' / green
        < 30%   -> '.' / dim
    """
    if pct >= 90:
        return "#", "red"
    if pct >= 70:
        return "=", "yellow"
    if pct >= 30:
        return "-", "green"
    return ".", "dim"


def get_rate(bytes_val: float) -> str:
    """Convert bytes/sec to human-readable rate string."""
    if bytes_val < 1024:
        return f"{bytes_val:.0f} B/s"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB/s"
    if bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB/s"
    return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB/s"


def get_size(bytes_val: float) -> str:
    """Convert bytes to human-readable size string."""
    if bytes_val < 1024:
        return f"{bytes_val:.0f} B"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    if bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"
