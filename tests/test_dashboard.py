"""Tests for BASTION TUI dashboard (S5: Dashboard Evolution)."""

from __future__ import annotations

import time

from rich.text import Text

from bastion.dashboard.helpers import (
    format_uptime,
    sparkline,
    state_color,
    temp_color,
    usage_color,
    vram_bar,
)
from bastion.dashboard.panels_broker import AlertPanel
from bastion.dashboard.statusbar import SafetyLimitsBar

# ---------------------------------------------------------------------------
# sparkline tests
# ---------------------------------------------------------------------------


def test_sparkline_empty() -> None:
    """sparkline([]) returns empty string."""
    assert sparkline([]) == ""


def test_sparkline_single_value() -> None:
    """sparkline([5.0]) returns a single block character."""
    result = sparkline([5.0])
    assert len(result) == 1
    # Single value: span=1.0, (5.0 - 5.0)/1.0*8 = 0 => blocks[0] = " "
    assert result == " "


def test_sparkline_uniform() -> None:
    """sparkline with all same values returns all same chars."""
    result = sparkline([5.0, 5.0, 5.0])
    assert len(result) == 3
    # All values equal => span=1.0, all map to index 0 => " "
    assert result[0] == result[1] == result[2]


def test_sparkline_ascending() -> None:
    """sparkline with ascending values produces monotonically non-decreasing chars."""
    result = sparkline([1.0, 2.0, 3.0, 4.0, 5.0])
    assert len(result) == 5
    for i in range(len(result) - 1):
        assert ord(result[i]) <= ord(result[i + 1])


def test_sparkline_width() -> None:
    """sparkline with width=10 returns exactly 10 characters."""
    result = sparkline(list(range(30)), width=10)
    assert len(result) == 10


def test_sparkline_negative_values() -> None:
    """sparkline handles negative values correctly."""
    result = sparkline([-3.0, -1.0, 0.0, 2.0])
    assert len(result) == 4
    # Should be ascending since values are ascending
    for i in range(len(result) - 1):
        assert ord(result[i]) <= ord(result[i + 1])


# ---------------------------------------------------------------------------
# temp_color tests
# ---------------------------------------------------------------------------


def test_temp_color_green() -> None:
    """temp_color(40) returns green (below 50)."""
    assert temp_color(40) == "green"


def test_temp_color_yellow() -> None:
    """temp_color(60) returns yellow (50-69 range)."""
    assert temp_color(60) == "yellow"


def test_temp_color_orange() -> None:
    """temp_color(75) returns yellow bold (70-79 range)."""
    assert temp_color(75) == "yellow bold"


def test_temp_color_red() -> None:
    """temp_color(85) returns red bold (>= 80)."""
    assert temp_color(85) == "red bold"


def test_temp_color_none() -> None:
    """temp_color(None) returns dim."""
    assert temp_color(None) == "dim"


# ---------------------------------------------------------------------------
# usage_color tests
# ---------------------------------------------------------------------------


def test_usage_color_green() -> None:
    """usage_color(30.0) returns green (below 50)."""
    assert usage_color(30.0) == "green"


def test_usage_color_yellow() -> None:
    """usage_color(60.0) returns yellow (50-74 range)."""
    assert usage_color(60.0) == "yellow"


def test_usage_color_orange() -> None:
    """usage_color(80.0) returns yellow bold (75-89 range)."""
    assert usage_color(80.0) == "yellow bold"


def test_usage_color_red() -> None:
    """usage_color(95.0) returns red bold (>= 90)."""
    assert usage_color(95.0) == "red bold"


def test_usage_color_none() -> None:
    """usage_color(None) returns dim."""
    assert usage_color(None) == "dim"


# ---------------------------------------------------------------------------
# format_uptime tests
# ---------------------------------------------------------------------------


def test_format_uptime_seconds() -> None:
    """format_uptime(45) returns '0m 45s'."""
    assert format_uptime(45) == "0m 45s"


def test_format_uptime_minutes() -> None:
    """format_uptime(125) returns '2m 5s'."""
    assert format_uptime(125) == "2m 5s"


def test_format_uptime_hours() -> None:
    """format_uptime(3725) returns '1h 2m 5s'."""
    assert format_uptime(3725) == "1h 2m 5s"


def test_format_uptime_days() -> None:
    """format_uptime(90061) returns '1d 1h 1m' (seconds omitted when days > 0)."""
    assert format_uptime(90061) == "1d 1h 1m"


# ---------------------------------------------------------------------------
# vram_bar tests
# ---------------------------------------------------------------------------


def test_vram_bar_no_data() -> None:
    """vram_bar(None, None) returns Text with 'no data'."""
    result = vram_bar(None, None)
    assert isinstance(result, Text)
    assert "no data" in result.plain


def test_vram_bar_zero_total() -> None:
    """vram_bar(1000, 0) returns Text with 'no data'."""
    result = vram_bar(1000, 0)
    assert isinstance(result, Text)
    assert "no data" in result.plain


# ---------------------------------------------------------------------------
# state_color tests
# ---------------------------------------------------------------------------


def test_state_color() -> None:
    """state_color returns correct colors for each state."""
    assert state_color("running") == "green"
    assert state_color("draining") == "yellow"
    assert state_color("unknown") == "red"


# ---------------------------------------------------------------------------
# AlertPanel threshold tests
# ---------------------------------------------------------------------------


def test_alert_panel_thresholds() -> None:
    """Verify AlertPanel threshold constants."""
    assert AlertPanel.VRAM_WARN_PCT == 85.0
    assert AlertPanel.VRAM_CRIT_PCT == 95.0
    assert AlertPanel.TEMP_WARN_C == 75
    assert AlertPanel.TEMP_CRIT_C == 82
    assert AlertPanel.QUEUE_WARN == 10
    assert AlertPanel.QUEUE_CRIT == 50


def test_alert_panel_render_empty() -> None:
    """AlertPanel.render_data([]) shows 'No active alerts'."""
    panel = AlertPanel.__new__(AlertPanel)
    table = panel.render_data([])
    # Table should have one row with "No active alerts"
    assert table.row_count == 1


def test_alert_panel_render_alerts() -> None:
    """AlertPanel.render_data with an alert renders correctly."""
    panel = AlertPanel.__new__(AlertPanel)
    alerts = [{"severity": "warn", "message": "test warning"}]
    table = panel.render_data(alerts)
    assert table.row_count == 1


# ---------------------------------------------------------------------------
# SafetyLimitsBar tests
# ---------------------------------------------------------------------------


def test_safety_bar_default_thresholds() -> None:
    """SafetyLimitsBar defaults to 26.0 GB VRAM and 82°C temp."""
    bar = SafetyLimitsBar()
    assert bar._max_vram_gb == 26.0
    assert bar._max_temp_c == 82


def test_safety_bar_render_returns_text() -> None:
    """SafetyLimitsBar.render() returns a Text object with threshold info."""
    bar = SafetyLimitsBar()
    result = bar.render()
    assert isinstance(result, Text)
    assert "26.0GB" in result.plain
    assert "82\u00b0C" in result.plain


def test_safety_bar_custom_thresholds() -> None:
    """SafetyLimitsBar accepts custom VRAM and temp thresholds."""
    bar = SafetyLimitsBar(max_vram_gb=20.0, max_temp_c=75)
    result = bar.render()
    assert "20.0GB" in result.plain
    assert "75\u00b0C" in result.plain


def test_safety_bar_update_limits() -> None:
    """update_limits() changes the displayed thresholds."""
    bar = SafetyLimitsBar()
    bar.update_limits(max_vram_gb=24.0, max_temp_c=80)
    assert bar._max_vram_gb == 24.0
    assert bar._max_temp_c == 80
    result = bar.render()
    assert "24.0GB" in result.plain


# ---------------------------------------------------------------------------
# record_recent_request test
# ---------------------------------------------------------------------------


def test_record_recent_request() -> None:
    """record_recent_request appends to the ring buffer correctly."""
    from bastion.server import _recent_requests, record_recent_request

    # Clear any existing entries
    _recent_requests.clear()

    record_recent_request(
        model="qwen3:8b",
        endpoint="/api/generate",
        tier="interactive",
        queue_wait_s=0.5,
        duration_s=1.234,
        status_code=200,
    )

    assert len(_recent_requests) == 1
    entry = _recent_requests[0]
    assert entry["model"] == "qwen3:8b"
    assert entry["endpoint"] == "/api/generate"
    assert entry["tier"] == "interactive"
    assert entry["queue_wait_s"] == 0.5
    assert entry["duration_s"] == 1.234
    assert entry["status_code"] == 200
    assert "timestamp" in entry
    # Timestamp should be recent (within last 5 seconds)
    assert abs(entry["timestamp"] - time.time()) < 5.0

    # Clean up
    _recent_requests.clear()
