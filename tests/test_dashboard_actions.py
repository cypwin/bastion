"""Tests for ``BastionDashboard`` action handlers.

Strategy A: mount the real Dashboard via Textual's Pilot, swap the
``BastionClient`` for an ``AsyncMock`` stub, pre-populate ``_last_data``, and
patch ``push_screen`` to invoke the modal callback synchronously with a
chosen value.  This exercises the action handler -> ``_do_*`` worker ->
client call path without driving the modal UI.

What is covered
---------------

* ``action_preload``: success / API error / empty-loaded fallback /
  no-broker-data / no-models toasts.
* ``action_unload``: success / status="failed" warning / API exception /
  no-broker-data / no-models toasts.
* ``action_drain``: drain toggle (running -> draining) and resume
  toggle (draining -> running), failure path, exception path,
  confirmation declined.
* ``action_service_restart``: success (rc=0), failure (rc=1 with stderr),
  timeout (asyncio.wait_for raises), confirmation declined.
* ``action_help``: pushes a HelpModal.
* ``action_fan_control``: toggle-auto and empty-speed early returns.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.dashboard.app import BastionDashboard
from bastion.dashboard.modals import (
    ConfirmActionModal,
    HelpModal,
    ModelSelectModal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_client() -> MagicMock:
    """Build a MagicMock with AsyncMock methods for every BastionClient call."""
    client = MagicMock()
    client.poll = AsyncMock(return_value={})
    client.get_recent = AsyncMock(return_value=[])
    client.get_queue = AsyncMock(return_value={})
    client.get_health = AsyncMock(return_value={})
    client.get_vram_ledger = AsyncMock(return_value={})
    client.get_watchdog = AsyncMock(return_value={})
    client.get_counters = AsyncMock(return_value={})
    client.get_thrashing = AsyncMock(return_value={})
    client.post_preload = AsyncMock(return_value={"status": "loaded"})
    client.post_unload = AsyncMock(return_value={"status": "unloaded"})
    client.post_drain = AsyncMock(return_value={"status": "draining"})
    client.post_resume = AsyncMock(return_value={"status": "running"})
    client.close = AsyncMock(return_value=None)
    return client


class _CallbackRunner:
    """Replacement for ``App.push_screen`` that runs callbacks immediately.

    ``app.push_screen(screen, callback=cb)`` becomes ``cb(value)`` where
    ``value`` is supplied by the caller through ``next_value`` (single use)
    or ``values`` (FIFO queue for actions that push nested modals such as
    ``action_gpu_kill``).
    """

    def __init__(self) -> None:
        self.next_value: Any = None
        self.values: list[Any] = []
        self.pushed: list[Any] = []

    def __call__(
        self, screen: Any, callback: Any = None, *args: Any, **kwargs: Any
    ) -> None:
        self.pushed.append(screen)
        if callback is not None:
            value = self.values.pop(0) if self.values else self.next_value
            callback(value)


@contextlib.asynccontextmanager
async def _mounted_dashboard(
    last_data: dict[str, Any] | None = None,
    client: MagicMock | None = None,
):
    """Yield (app, pilot, runner) with the dashboard fully mounted.

    The HTTP client is stubbed, the initial refresh tick is suppressed so
    pre-populated ``_last_data`` survives, and ``push_screen`` is wrapped
    so callbacks run synchronously.
    """

    app = BastionDashboard(url="http://test", interval=3600.0)
    app._client = client or _stub_client()
    # Suppress the periodic tick so it doesn't overwrite our _last_data.
    app.refresh_data = AsyncMock(return_value=None)  # type: ignore[method-assign]

    runner = _CallbackRunner()

    # Capture notifications for assertions.
    notifications: list[dict[str, Any]] = []

    def _notify(message: str, *, severity: str = "information", **kwargs: Any) -> None:
        notifications.append({"message": message, "severity": severity})

    app.notify = _notify  # type: ignore[assignment]

    workers: list[Any] = []

    def _run_worker(coro: Any, **kwargs: Any) -> Any:
        # Drive the worker coroutine on the current event loop and store the task
        # so the test can await its completion deterministically.
        task = asyncio.ensure_future(coro)
        workers.append(task)
        return task

    app.run_worker = _run_worker  # type: ignore[assignment]

    async with app.run_test() as pilot:
        app._last_data = last_data
        # Patch push_screen now (it's set up on the App instance after mount).
        app.push_screen = runner  # type: ignore[assignment]
        app._notifications = notifications  # type: ignore[attr-defined]
        app._workers_list = workers  # type: ignore[attr-defined]
        try:
            yield app, pilot, runner
        finally:
            # Make sure any pending workers complete before unmount.
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)


async def _drain_workers(app: Any) -> None:
    workers = getattr(app, "_workers_list", [])
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
        workers.clear()


# ---------------------------------------------------------------------------
# action_preload
# ---------------------------------------------------------------------------


async def test_action_preload_no_broker_data_notifies() -> None:
    async with _mounted_dashboard(last_data=None) as (app, _pilot, runner):
        app.action_preload()
        assert runner.pushed == []
        msgs = [n["message"] for n in app._notifications]
        assert any("No broker data" in m for m in msgs)


async def test_action_preload_no_candidates_notifies() -> None:
    async with _mounted_dashboard(
        last_data={"available_models": [], "loaded_models": []}
    ) as (app, _pilot, runner):
        app.action_preload()
        assert runner.pushed == []
        assert any(
            "No models available" in n["message"] for n in app._notifications
        )


async def test_action_preload_success_calls_client() -> None:
    client = _stub_client()
    client.post_preload = AsyncMock(return_value={"status": "loaded"})
    async with _mounted_dashboard(
        last_data={
            "available_models": ["qwen3:14b", "llama3.1:8b"],
            "loaded_models": [],
        },
        client=client,
    ) as (app, _pilot, runner):
        runner.next_value = "qwen3:14b"
        app.action_preload()
        await _drain_workers(app)
        client.post_preload.assert_awaited_once_with("qwen3:14b")
        assert any("Preload" in n["message"] for n in app._notifications)
        assert isinstance(runner.pushed[0], ModelSelectModal)


async def test_action_preload_cancelled_no_client_call() -> None:
    client = _stub_client()
    async with _mounted_dashboard(
        last_data={"available_models": ["qwen3:14b"], "loaded_models": []},
        client=client,
    ) as (app, _pilot, runner):
        runner.next_value = ""  # cancel
        app.action_preload()
        await _drain_workers(app)
        client.post_preload.assert_not_called()


async def test_do_preload_error_response_warning() -> None:
    client = _stub_client()
    client.post_preload = AsyncMock(return_value={"detail": "vram full"})
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_preload("qwen3:14b")
        msgs = app._notifications
        assert any("Preload failed" in n["message"] for n in msgs)
        assert any(n["severity"] == "warning" for n in msgs)


async def test_do_preload_exception_error_severity() -> None:
    client = _stub_client()
    client.post_preload = AsyncMock(side_effect=RuntimeError("offline"))
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_preload("qwen3:14b")
        msgs = app._notifications
        assert any("Preload failed" in n["message"] for n in msgs)
        assert any(n["severity"] == "error" for n in msgs)


# ---------------------------------------------------------------------------
# action_unload
# ---------------------------------------------------------------------------


async def test_action_unload_no_broker_data_notifies() -> None:
    async with _mounted_dashboard(last_data=None) as (app, _pilot, runner):
        app.action_unload()
        assert runner.pushed == []
        assert any("No broker data" in n["message"] for n in app._notifications)


async def test_action_unload_no_loaded_models_notifies() -> None:
    async with _mounted_dashboard(
        last_data={"loaded_models": []}
    ) as (app, _pilot, runner):
        app.action_unload()
        assert runner.pushed == []
        assert any(
            "No models loaded" in n["message"] for n in app._notifications
        )


async def test_action_unload_success_calls_client() -> None:
    client = _stub_client()
    client.post_unload = AsyncMock(return_value={"status": "unloaded"})
    async with _mounted_dashboard(
        last_data={"loaded_models": [{"name": "qwen3:14b"}]},
        client=client,
    ) as (app, _pilot, runner):
        runner.next_value = "qwen3:14b"
        app.action_unload()
        await _drain_workers(app)
        client.post_unload.assert_awaited_once_with("qwen3:14b")
        assert any("Unload" in n["message"] for n in app._notifications)


async def test_do_unload_failed_status_warning() -> None:
    """status='failed' must surface as a warning toast."""
    client = _stub_client()
    client.post_unload = AsyncMock(return_value={"status": "failed", "detail": "busy"})
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_unload("qwen3:14b")
        msgs = app._notifications
        assert any("Unload failed" in n["message"] for n in msgs)
        assert any(n["severity"] == "warning" for n in msgs)


async def test_do_unload_exception_error_severity() -> None:
    client = _stub_client()
    client.post_unload = AsyncMock(side_effect=RuntimeError("boom"))
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_unload("qwen3:14b")
        msgs = app._notifications
        assert any("Unload failed" in n["message"] for n in msgs)
        assert any(n["severity"] == "error" for n in msgs)


# ---------------------------------------------------------------------------
# action_drain
# ---------------------------------------------------------------------------


async def test_action_drain_running_calls_post_drain() -> None:
    client = _stub_client()
    client.post_drain = AsyncMock(return_value={"status": "draining"})
    async with _mounted_dashboard(
        last_data={"state": "running"},
        client=client,
    ) as (app, _pilot, runner):
        runner.next_value = True  # user confirms
        app.action_drain()
        await _drain_workers(app)
        client.post_drain.assert_awaited_once()
        client.post_resume.assert_not_called()
        assert isinstance(runner.pushed[0], ConfirmActionModal)


async def test_action_drain_draining_calls_post_resume() -> None:
    client = _stub_client()
    client.post_resume = AsyncMock(return_value={"status": "running"})
    async with _mounted_dashboard(
        last_data={"state": "draining"},
        client=client,
    ) as (app, _pilot, runner):
        runner.next_value = True
        app.action_drain()
        await _drain_workers(app)
        client.post_resume.assert_awaited_once()
        client.post_drain.assert_not_called()


async def test_action_drain_declined_no_client_call() -> None:
    client = _stub_client()
    async with _mounted_dashboard(
        last_data={"state": "running"},
        client=client,
    ) as (app, _pilot, runner):
        runner.next_value = False
        app.action_drain()
        await _drain_workers(app)
        client.post_drain.assert_not_called()


async def test_do_drain_unknown_status_warning() -> None:
    client = _stub_client()
    client.post_drain = AsyncMock(return_value={"detail": "weird"})
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_drain(current_state="running")
        msgs = app._notifications
        assert any("Drain toggle failed" in n["message"] for n in msgs)
        assert any(n["severity"] == "warning" for n in msgs)


async def test_do_drain_exception_error_severity() -> None:
    client = _stub_client()
    client.post_drain = AsyncMock(side_effect=RuntimeError("oops"))
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_drain(current_state="running")
        msgs = app._notifications
        assert any("Drain toggle failed" in n["message"] for n in msgs)
        assert any(n["severity"] == "error" for n in msgs)


# ---------------------------------------------------------------------------
# action_help — trivial smoke test
# ---------------------------------------------------------------------------


async def test_action_help_pushes_help_modal() -> None:
    async with _mounted_dashboard() as (app, _pilot, runner):
        app.action_help()
        assert len(runner.pushed) == 1
        assert isinstance(runner.pushed[0], HelpModal)


# ---------------------------------------------------------------------------
# action_fan_control
# ---------------------------------------------------------------------------


async def test_action_fan_control_empty_speed_noop() -> None:
    """Cancel from fan modal must not call set_fan_speed."""
    with patch("bastion.dashboard.app.set_fan_speed") as set_fan:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = ""
            app.action_fan_control()
            set_fan.assert_not_called()


async def test_action_fan_control_toggle_auto_flips_flag() -> None:
    """toggle-auto must flip _auto_fan_enabled and not call set_fan_speed."""
    with patch("bastion.dashboard.app.set_fan_speed") as set_fan:
        async with _mounted_dashboard() as (app, _pilot, runner):
            assert app._auto_fan_enabled is False
            runner.next_value = "toggle-auto"
            app.action_fan_control()
            assert app._auto_fan_enabled is True
            # Toggle back -- should also reset state to idle.
            app._auto_fan_state = "cooling"
            runner.next_value = "toggle-auto"
            app.action_fan_control()
            assert app._auto_fan_enabled is False
            assert app._auto_fan_state == "idle"
            set_fan.assert_not_called()


async def test_action_fan_control_speed_calls_helper() -> None:
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = "70"
            app.action_fan_control()
            set_fan.assert_called_once_with("70")


async def test_action_fan_control_failure_notifies_error() -> None:
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(False, "no sudo")
    ):
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = "100"
            app.action_fan_control()
            msgs = app._notifications
            assert any("Fan control failed" in n["message"] for n in msgs)
            assert any(n["severity"] == "error" for n in msgs)


# ---------------------------------------------------------------------------
# action_service_restart -- subprocess mocking
# ---------------------------------------------------------------------------


def _proc_stub(returncode: int, stderr: bytes = b"") -> AsyncMock:
    """Build an AsyncMock that mimics asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


async def test_service_restart_declined_no_subprocess() -> None:
    with patch("asyncio.create_subprocess_exec") as cse:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = False  # user cancels
            app.action_service_restart()
            await _drain_workers(app)
            cse.assert_not_called()


async def test_service_restart_success_notifies() -> None:
    proc = _proc_stub(returncode=0)
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ):
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = True
            app.action_service_restart()
            await _drain_workers(app)
            assert any(
                "bastion.service restarted" in n["message"]
                for n in app._notifications
            )


async def test_service_restart_failure_notifies_stderr() -> None:
    proc = _proc_stub(returncode=1, stderr=b"not authorized")
    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ):
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = True
            app.action_service_restart()
            await _drain_workers(app)
            msgs = app._notifications
            assert any(
                "Restart failed" in n["message"] and "not authorized" in n["message"]
                for n in msgs
            )
            assert any(n["severity"] == "error" for n in msgs)


async def test_service_restart_timeout_notifies() -> None:
    proc = _proc_stub(returncode=0)
    # Make communicate hang past wait_for's timeout.
    proc.communicate = AsyncMock(side_effect=TimeoutError("timeout"))

    async def _fake_wait_for(coro: Any, timeout: float) -> Any:  # noqa: ARG001
        # Cancel the inner coroutine so no warnings leak, then raise.
        if asyncio.iscoroutine(coro):
            coro.close()
        raise TimeoutError("timeout")

    with patch(
        "asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)
    ), patch("asyncio.wait_for", new=_fake_wait_for):
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = True
            app.action_service_restart()
            await _drain_workers(app)
            assert any(
                "Restart timed out" in n["message"] for n in app._notifications
            )


async def test_service_restart_exception_notifies() -> None:
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(side_effect=FileNotFoundError("no sudo")),
    ):
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = True
            app.action_service_restart()
            await _drain_workers(app)
            msgs = app._notifications
            assert any("Restart failed" in n["message"] for n in msgs)
            assert any(n["severity"] == "error" for n in msgs)


# ---------------------------------------------------------------------------
# Simple action smoke tests -- layout / sparkline / history
# ---------------------------------------------------------------------------


async def test_action_layout_switches() -> None:
    async with _mounted_dashboard() as (app, _pilot, _runner):
        app.action_layout_compact()
        assert app._layout_mode == "compact"
        app.action_layout_standard()
        assert app._layout_mode == "standard"
        app.action_layout_full()
        assert app._layout_mode == "full"


async def test_action_toggle_secondary_requires_full_layout() -> None:
    async with _mounted_dashboard() as (app, _pilot, _runner):
        app._layout_mode = "standard"
        app.action_toggle_secondary()
        assert any(
            "Secondary panels require" in n["message"]
            for n in app._notifications
        )
        # In full mode it should actually toggle.
        app._layout_mode = "full"
        before = app._show_secondary
        app.action_toggle_secondary()
        assert app._show_secondary is not before


async def test_action_sparkline_and_history_actions() -> None:
    from bastion.dashboard import helpers as _helpers

    saved_spark = _helpers.SPARKLINE_WIDTH
    saved_hist = _helpers.HISTORY_LEN
    try:
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app.action_sparkline_wider()
            app.action_sparkline_narrower()
            app.action_history_longer()
            app.action_history_shorter()
            # No exception is the assertion; notifications cover wider/narrower.
            assert len(app._notifications) >= 4
    finally:
        _helpers.SPARKLINE_WIDTH = saved_spark
        _helpers.HISTORY_LEN = saved_hist


async def test_action_refresh_resets_backoff_and_calls_refresh() -> None:
    async with _mounted_dashboard() as (app, _pilot, _runner):
        app._backoff_until = 999.0
        await app.action_refresh()
        assert app._backoff_until == 0.0
        # refresh_data was stubbed in _mounted_dashboard.
        app.refresh_data.assert_awaited()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# action_gpu_kill -- exercises nested-modal flow + os.kill dispatch
# ---------------------------------------------------------------------------


async def test_action_gpu_kill_empty_pid_noop() -> None:
    """Cancel from process-list modal must not call os.kill."""
    with patch("os.kill") as os_kill:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = ""  # cancel from process-list modal
            app.action_gpu_kill()
            os_kill.assert_not_called()


async def test_action_gpu_kill_pid_not_found_warns() -> None:
    """If the PID disappears between modals, surface a warning."""
    with patch(
        "bastion.dashboard.app.SystemDataCollector.query_gpu_processes",
        return_value=[],
    ), patch("os.kill") as os_kill:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.next_value = "12345"  # user selects a PID
            app.action_gpu_kill()
            os_kill.assert_not_called()
            msgs = app._notifications
            assert any("no longer exists" in n["message"] for n in msgs)


async def test_action_gpu_kill_sigterm_sent() -> None:
    procs = [{"pid": "12345", "name": "ollama", "vram_mb": "8192"}]
    with patch(
        "bastion.dashboard.app.SystemDataCollector.query_gpu_processes",
        return_value=procs,
    ), patch("os.kill") as os_kill:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.values = ["12345", "kill"]
            app.action_gpu_kill()
            import signal as _sig
            os_kill.assert_called_once_with(12345, _sig.SIGTERM)
            assert any("Sent SIGTERM" in n["message"] for n in app._notifications)


async def test_action_gpu_kill_sigkill_sent_on_force() -> None:
    procs = [{"pid": "67890", "name": "python", "vram_mb": "2048"}]
    with patch(
        "bastion.dashboard.app.SystemDataCollector.query_gpu_processes",
        return_value=procs,
    ), patch("os.kill") as os_kill:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.values = ["67890", "kill-9"]
            app.action_gpu_kill()
            import signal as _sig
            os_kill.assert_called_once_with(67890, _sig.SIGKILL)


async def test_action_gpu_kill_permission_error_notifies() -> None:
    procs = [{"pid": "12345", "name": "ollama", "vram_mb": "8192"}]
    with patch(
        "bastion.dashboard.app.SystemDataCollector.query_gpu_processes",
        return_value=procs,
    ), patch("os.kill", side_effect=PermissionError("denied")):
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.values = ["12345", "kill"]
            app.action_gpu_kill()
            msgs = app._notifications
            assert any("Kill failed" in n["message"] for n in msgs)
            assert any(n["severity"] == "error" for n in msgs)


async def test_action_gpu_kill_inner_cancel_no_call() -> None:
    """User cancels the confirm modal -> os.kill must not be called."""
    procs = [{"pid": "12345", "name": "ollama", "vram_mb": "8192"}]
    with patch(
        "bastion.dashboard.app.SystemDataCollector.query_gpu_processes",
        return_value=procs,
    ), patch("os.kill") as os_kill:
        async with _mounted_dashboard() as (app, _pilot, runner):
            runner.values = ["12345", ""]
            app.action_gpu_kill()
            os_kill.assert_not_called()


# ---------------------------------------------------------------------------
# _check_auto_fan
# ---------------------------------------------------------------------------


async def test_check_auto_fan_disabled_does_nothing() -> None:
    """Auto-fan disabled -> immediate return, no fan call."""
    with patch("bastion.dashboard.app.set_fan_speed") as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = False
            app._check_auto_fan({})
            set_fan.assert_not_called()


async def test_check_auto_fan_no_temp_no_action() -> None:
    """Missing temperature reading -> no fan action."""
    with patch("bastion.dashboard.app.set_fan_speed") as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._collector.read_cpu_temp = MagicMock(return_value=None)
            app._check_auto_fan({})
            set_fan.assert_not_called()


@pytest.mark.parametrize(
    "temp,expected_speed",
    [
        (59.9, None),   # below the curve — stays on BIOS auto
        (60.0, "30"),
        (69.9, "30"),
        (70.0, "50"),
        (80.0, "90"),
        (85.0, "90"),   # boundary: 100% only OVER 85C (operator spec)
        (85.1, "100"),
    ],
)
async def test_check_auto_fan_escalation_curve(temp, expected_speed) -> None:
    """From idle, each curve band applies its speed (60/70/80/85+)."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._collector.read_cpu_temp = MagicMock(return_value=temp)
            app._check_auto_fan({})
            if expected_speed is None:
                set_fan.assert_not_called()
                assert app._auto_fan_state == "idle"
            else:
                set_fan.assert_called_once_with(expected_speed)
                assert app._auto_fan_speed == expected_speed
                assert app._auto_fan_state == "cooling"


async def test_check_auto_fan_escalates_from_lower_band() -> None:
    """Already at 30%, temperature jumps into the 80C band -> 90%."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "30"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=81.0)
            app._check_auto_fan({})
            set_fan.assert_called_once_with("90")
            assert app._auto_fan_speed == "90"


async def test_check_auto_fan_holds_band_within_hysteresis() -> None:
    """At 90% (80C band), 76C is within the 5C hysteresis -> no change."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "90"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=76.0)
            app._check_auto_fan({})
            set_fan.assert_not_called()
            assert app._auto_fan_speed == "90"


async def test_check_auto_fan_steps_down_past_hysteresis() -> None:
    """At 90%, 74C (>5C below the 80C trigger) -> step down to 50%."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "90"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=74.0)
            app._check_auto_fan({})
            set_fan.assert_called_once_with("50")
            assert app._auto_fan_speed == "50"
            assert app._auto_fan_state == "cooling"


async def test_check_auto_fan_returns_to_bios_auto_when_cool() -> None:
    """At 30% (60C band), 54C (>5C below 60) -> back to BIOS auto + idle."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "30"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=54.0)
            app._check_auto_fan({})
            set_fan.assert_called_once_with("auto")
            assert app._auto_fan_speed is None
            assert app._auto_fan_state == "idle"


async def test_check_auto_fan_gpu_floor_raises_engagement_speed() -> None:
    """CPU triggers at 62C (30% band) but GPU sits at 84C -> engage at 90%,
    never below what the GPU's own band demands (its firmware curve is
    suspended while we override)."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._collector.read_cpu_temp = MagicMock(return_value=62.0)
            app._check_auto_fan({"gpu": {"temperature_c": 84.0}})
            set_fan.assert_called_once_with("90")


async def test_check_auto_fan_gpu_floor_escalates_active_override() -> None:
    """Override active at 30% (CPU band), GPU climbs over 85C -> 100%."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "30"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=62.0)
            app._check_auto_fan({"gpu": {"temperature_c": 86.0}})
            set_fan.assert_called_once_with("100")


async def test_check_auto_fan_gpu_floor_holds_within_hysteresis() -> None:
    """At 90% on the GPU floor, GPU 76C is within hysteresis -> no change."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "90"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=62.0)
            app._check_auto_fan({"gpu": {"temperature_c": 76.0}})
            set_fan.assert_not_called()


async def test_check_auto_fan_cpu_release_wins_over_hot_gpu() -> None:
    """CPU fully below the curve -> release to BIOS auto even with a hot
    GPU: GPUFanControlState=0 resumes the firmware's own (finer) curve."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._auto_fan_speed = "90"
            app._auto_fan_state = "cooling"
            app._collector.read_cpu_temp = MagicMock(return_value=50.0)
            app._check_auto_fan({"gpu": {"temperature_c": 84.0}})
            set_fan.assert_called_once_with("auto")
            assert app._auto_fan_speed is None
            assert app._auto_fan_state == "idle"


async def test_check_auto_fan_hot_gpu_alone_does_not_engage() -> None:
    """GPU hot but CPU below the curve and no override active -> do nothing;
    the GPU firmware curve is in control."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(True, "ok")
    ) as set_fan, patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._collector.read_cpu_temp = MagicMock(return_value=45.0)
            app._check_auto_fan({"gpu": {"temperature_c": 88.0}})
            set_fan.assert_not_called()


async def test_check_auto_fan_failed_set_keeps_state() -> None:
    """A failed set_fan_speed must not update the tracked speed/state."""
    with patch(
        "bastion.dashboard.app.set_fan_speed", return_value=(False, "err")
    ), patch(
        "bastion.dashboard.app.fan_control_available", return_value=True
    ):
        async with _mounted_dashboard() as (app, _pilot, _runner):
            app._auto_fan_enabled = True
            app._collector.read_cpu_temp = MagicMock(return_value=82.0)
            app._check_auto_fan({})
            assert app._auto_fan_speed is None  # retried next tick
            assert app._auto_fan_state == "idle"


# ---------------------------------------------------------------------------
# Worker direct tests -- _do_preload / _do_unload happy + edge
# ---------------------------------------------------------------------------


async def test_do_preload_status_empty_string_warns() -> None:
    """Empty status string is treated as 'unknown response'."""
    client = _stub_client()
    client.post_preload = AsyncMock(return_value={"status": ""})
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_preload("qwen3:14b")
        # status falsy -> falls into else branch
        msgs = [n["message"] for n in app._notifications]
        assert any("Preload failed" in m for m in msgs)


@pytest.mark.parametrize(
    "current_state,expected_method",
    [
        ("running", "post_drain"),
        ("draining", "post_resume"),
    ],
)
async def test_do_drain_dispatch_by_state(
    current_state: str, expected_method: str
) -> None:
    client = _stub_client()
    async with _mounted_dashboard(client=client) as (app, _pilot, _runner):
        await app._do_drain(current_state=current_state)
        getattr(client, expected_method).assert_awaited()
