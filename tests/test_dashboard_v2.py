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


# ---------------------------------------------------------------------------
# SystemDataCollector tests
# ---------------------------------------------------------------------------

from bastion.dashboard.collectors import SystemDataCollector


def test_collector_init() -> None:
    c = SystemDataCollector()
    assert len(c.cpu_history) == 0
    assert len(c.net_recv_history) == 0
    assert len(c.net_sent_history) == 0


def test_collector_get_cpu_data() -> None:
    c = SystemDataCollector()
    data = c.get_cpu_data()
    assert "percent" in data
    assert "per_core" in data
    assert "load_avg" in data
    assert isinstance(data["per_core"], list)


def test_collector_get_network_data() -> None:
    c = SystemDataCollector()
    data1 = c.get_network_data()
    assert "recv_rate" in data1
    assert "sent_rate" in data1
    assert "recv_total_gb" in data1
    assert "sent_total_gb" in data1


def test_collector_get_memory_data() -> None:
    c = SystemDataCollector()
    data = c.get_memory_data()
    if data is not None:
        assert "total_gb" in data
        assert "used_gb" in data
        assert "percent" in data


def test_collector_cpu_per_core_chars() -> None:
    c = SystemDataCollector()
    data = c.get_cpu_data()
    text = c.cpu_per_core_text()
    assert text is not None
    assert len(text) > 0


# ---------------------------------------------------------------------------
# Layout modes and app tests
# ---------------------------------------------------------------------------


def test_layout_modes_valid() -> None:
    from bastion.dashboard.app import LAYOUT_MODES
    assert "compact" in LAYOUT_MODES
    assert "standard" in LAYOUT_MODES
    assert "full" in LAYOUT_MODES
    assert len(LAYOUT_MODES) == 3


def test_app_creates_with_layout_modes() -> None:
    from bastion.dashboard.app import BastionDashboard
    for mode in ("compact", "standard", "full"):
        app = BastionDashboard(url="http://localhost:11434", layout_mode=mode)
        assert app._layout_mode == mode


def test_app_invalid_layout_defaults_to_standard() -> None:
    from bastion.dashboard.app import BastionDashboard
    app = BastionDashboard(url="http://localhost:11434", layout_mode="invalid")
    assert app._layout_mode == "standard"


def test_safety_bar_updates_limits() -> None:
    from bastion.dashboard.statusbar import SafetyLimitsBar
    bar = SafetyLimitsBar(max_vram_gb=26.0, max_temp_c=82)
    bar.update_limits(24.0, 80)
    assert bar._max_vram_gb == 24.0
    assert bar._max_temp_c == 80


def test_safety_bar_ignores_none() -> None:
    from bastion.dashboard.statusbar import SafetyLimitsBar
    bar = SafetyLimitsBar(max_vram_gb=26.0, max_temp_c=82)
    bar.update_limits(None, None)
    assert bar._max_vram_gb == 26.0
    assert bar._max_temp_c == 82
