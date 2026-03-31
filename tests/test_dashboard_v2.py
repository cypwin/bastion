"""Tests for BASTION Dashboard v2."""
from __future__ import annotations

from bastion.dashboard.helpers import (
    core_char,
    format_uptime,
    get_rate,
    sparkline,
    state_color,
    temp_color,
    usage_color,
    vram_bar,
)


def test_sparkline_empty() -> None:
    assert sparkline([]) == ""


def test_sparkline_ascending() -> None:
    result = sparkline([1.0, 2.0, 3.0, 4.0, 5.0])
    assert len(result) == 5
    for i in range(len(result) - 1):
        assert ord(result[i]) <= ord(result[i + 1])


def test_sparkline_width() -> None:
    result = sparkline(list(range(30)), width=10)
    assert len(result) == 10


def test_temp_color_green() -> None:
    assert temp_color(40) == "green"


def test_temp_color_red() -> None:
    assert "red" in temp_color(85)


def test_usage_color_green() -> None:
    assert usage_color(30) == "green"


def test_usage_color_red() -> None:
    assert "red" in usage_color(95)


def test_state_color_running() -> None:
    assert state_color("running") == "green"


def test_state_color_draining() -> None:
    assert state_color("draining") == "yellow"


def test_format_uptime_minutes() -> None:
    result = format_uptime(125)
    assert "2m" in result


def test_format_uptime_hours() -> None:
    result = format_uptime(7200)
    assert "2h" in result


def test_get_rate_bytes() -> None:
    assert "B/s" in get_rate(500)


def test_get_rate_megabytes() -> None:
    result = get_rate(5 * 1024 * 1024)
    assert "MB/s" in result


def test_core_char_idle() -> None:
    char, style = core_char(5.0)
    assert char == "."
    assert "dim" in style


def test_core_char_medium() -> None:
    char, style = core_char(50.0)
    assert char == "-"
    assert "green" in style


def test_core_char_high() -> None:
    char, style = core_char(80.0)
    assert char == "="
    assert "yellow" in style


def test_core_char_critical() -> None:
    char, style = core_char(95.0)
    assert char == "#"
    assert "red" in style
