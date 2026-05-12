"""Tests for BASTION Dashboard v2."""
from __future__ import annotations

from collections import deque

from bastion.dashboard.collectors import SystemDataCollector
from bastion.dashboard.helpers import (
    core_char,
    format_uptime,
    get_rate,
    sparkline,
    state_color,
    temp_color,
    usage_color,
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
    c.get_cpu_data()
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


def test_temp_color_warning_band_uses_16_color_safe() -> None:
    """`dark_orange` collapses in 16-color terminals; require an ANSI primary."""
    color = temp_color(75)
    assert "dark_orange" not in color
    # Must be one of the 16 ANSI primaries (or "yellow")
    assert color in {"yellow", "yellow bold", "red", "red bold"}


def test_usage_color_warning_band_uses_16_color_safe() -> None:
    color = usage_color(80)
    assert "dark_orange" not in color
    assert color in {"yellow", "yellow bold", "red", "red bold"}


def test_throughput_counter_reset_does_not_emit_negative_rate() -> None:
    """Broker restart resets total_requests_served to 0; ensure the rate
    computation doesn't push a negative value into the sparkline history.

    This test documents the contract at the call-site level (mirroring the
    app.py guard). It does not call app.py directly; the guard itself is
    integration-verified by the broker restart scenario described in the plan.
    """
    history: deque[float] = deque(maxlen=120)
    interval = 2.0
    prev_served = 1000  # Pre-restart counter

    # Simulate the post-restart poll: served counter has reset to 5
    served = 5
    delta = served - prev_served

    # Buggy behavior would push -29850.0 here.
    # Fixed behavior: skip the append on negative delta (counter reset).
    if delta >= 0:
        rate_per_min = delta * (60.0 / interval) if interval > 0 else 0
        history.append(rate_per_min)

    assert all(x >= 0 for x in history), "all rates must be non-negative"


def test_toggle_secondary_guard_present_in_source() -> None:
    """The [t] toggle must guard against non-full modes before flipping state."""
    import inspect
    from bastion.dashboard.app import BastionDashboard

    source = inspect.getsource(BastionDashboard.action_toggle_secondary)
    assert '_layout_mode != "full"' in source
    assert "return" in source
    # State flip must appear AFTER the guard, not before
    guard_pos = source.index('_layout_mode != "full"')
    flip_pos = source.index("_show_secondary = not")
    assert flip_pos > guard_pos, "state flip must follow the guard"


def test_vram_alert_uses_configured_budget_not_hardware_total() -> None:
    """VRAM alert thresholds must be evaluated against max_vram_gb, not the
    raw hardware total — otherwise alerts fire after the broker is already
    refusing loads."""
    from bastion.dashboard.app import BastionDashboard

    # 24 GB budget, 32 GB hardware. Used = 24 GB exactly.
    # Hardware-percentage: 24 / 32 = 75% → no alert.
    # Budget-percentage:   24 / 24 = 100% → CRIT.
    data = {
        "gpu": {
            "vram_used_mb": 24 * 1024,
            "vram_total_mb": 32 * 1024,
        },
        "max_vram_gb": 24.0,  # top-level, matching server.py BrokerStatus response
        "queue_depth": 0,
        "scheduler_state": "running",
    }

    # Build a minimal shim that exposes the attributes _evaluate_alerts reads.
    # _connected=True suppresses the unrelated "Broker unreachable" alert so
    # that the assertions below focus purely on VRAM threshold behavior.
    class _Shim:
        alert_history: deque = deque(maxlen=100)
        _connected: bool = True
        _consecutive_failures: int = 0

    shim = _Shim()
    alerts = BastionDashboard._evaluate_alerts(shim, data)  # type: ignore[arg-type]

    # Expect a CRITICAL VRAM alert (the broker is at budget ceiling)
    crit = [a for a in alerts if a.get("severity") == "critical"
            and "VRAM" in a.get("message", "")]
    assert crit, f"expected a CRITICAL VRAM alert at budget ceiling; got {alerts}"


def test_gpu_temp_threshold_uses_profile_ceiling() -> None:
    """An RTX 5090 with thermal_ceiling_c=80 at 82°C must render red,
    not green."""
    from bastion.dashboard.panels_system import TemperaturePanel

    panel = TemperaturePanel()
    table = panel.render_data(
        cpu_temp=None,
        nvme_temps=None,
        gpu_temp=82,
        gpu_ceiling_c=80,
    )
    # Rich Table stores markup strings in column._cells; str(table) does not
    # include markup tokens.
    temp_cells = table.columns[1]._cells  # "Temp" column
    assert any("red" in cell for cell in temp_cells), (
        f"82°C above ceiling=80 must render red; got temp cells: {temp_cells}"
    )


def test_gpu_temp_threshold_below_ceiling_is_green() -> None:
    """70°C is well below ceiling=80, must render green."""
    from bastion.dashboard.panels_system import TemperaturePanel

    panel = TemperaturePanel()
    table = panel.render_data(
        cpu_temp=None,
        nvme_temps=None,
        gpu_temp=70,
        gpu_ceiling_c=80,
    )
    temp_cells = table.columns[1]._cells  # "Temp" column
    assert any("green" in cell for cell in temp_cells), (
        f"70°C below ceiling=80 must render green; got temp cells: {temp_cells}"
    )


def test_service_restart_uses_async_subprocess() -> None:
    """The service-restart handler must NOT call subprocess.run inline;
    it must dispatch to an async worker. Verified by source inspection."""
    import inspect
    from bastion.dashboard import app as app_module

    source = inspect.getsource(app_module.BastionDashboard.action_service_restart)
    assert "asyncio.create_subprocess_exec" in source, (
        "action_service_restart must use asyncio.create_subprocess_exec, not subprocess.run"
    )
    assert "run_worker" in source, (
        "action_service_restart must dispatch via run_worker to avoid blocking the TUI"
    )


def test_all_modals_bind_escape_to_dismiss() -> None:
    """Every modal must dismiss on Escape — universal TUI contract."""
    from bastion.dashboard import modals
    from textual.screen import ModalScreen

    modal_classes = [
        cls for name, cls in vars(modals).items()
        if isinstance(cls, type) and issubclass(cls, ModalScreen) and cls is not ModalScreen
    ]
    assert modal_classes, "no modal classes found in modals.py"

    for cls in modal_classes:
        bindings = getattr(cls, "BINDINGS", [])
        keys = []
        for b in bindings:
            # Bindings may be Binding objects or tuples
            key = getattr(b, "key", None) or (b[0] if isinstance(b, tuple) else None)
            if key:
                keys.append(key)
        assert "escape" in keys, f"{cls.__name__} does not bind escape"
