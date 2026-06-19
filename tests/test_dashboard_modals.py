"""Regression tests for BASTION dashboard modals.

Originally added after [u] (unload) crashed the dashboard with BadIdentifier
because real Ollama model names like ``granite4.1:8b`` contain ``:`` and ``.``,
which Textual rejects when used inside a widget ``id``. The fix uses index-based
button ids and dereferences the model name on click.

Extended (2026-05-19) with coverage for every other modal: HelpModal,
ConfirmActionModal, GPUProcessListModal, ConfirmGPUKillModal, FanControlModal.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import patch

import pytest
from textual.app import App

from bastion.dashboard.modals import (
    ConfirmActionModal,
    ConfirmGPUKillModal,
    FanControlModal,
    GPUProcessListModal,
    HelpModal,
    ModelSelectModal,
)

# Textual identifier rule: ASCII letter/underscore start, then letters,
# digits, underscore, or hyphen.
_TEXTUAL_ID = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")

REAL_MODEL_NAMES = [
    "granite4.1:8b",
    "llama3.2:3b",
    "qwen2.5-coder:32b",
    "nomic-embed-text:latest",
]


class _Harness(App[str]):
    """Minimal Textual app that pushes a ModelSelectModal and stores the result."""

    def __init__(self, models: list[str]) -> None:
        super().__init__()
        self._models = models
        self.result: str | None = None

    def on_mount(self) -> None:
        def _cb(value: str | None) -> None:
            self.result = value or ""

        self.push_screen(
            ModelSelectModal("Pick a model", self._models),
            callback=_cb,
        )


# ---------------------------------------------------------------------------
# compose() regression — model names with ':' and '.'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_composes_with_colon_and_dot_names() -> None:
    """compose() must not raise BadIdentifier on real Ollama model names."""
    app = _Harness(REAL_MODEL_NAMES)
    async with app.run_test() as pilot:
        await pilot.pause()
        # If compose raised, the modal would not be on the stack.
        assert any(isinstance(s, ModelSelectModal) for s in app.screen_stack)


# ---------------------------------------------------------------------------
# Button IDs must satisfy Textual's identifier rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_button_ids_are_textual_safe() -> None:
    """Every Button id must match Textual's identifier rule."""
    import re

    textual_id = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")

    app = _Harness(REAL_MODEL_NAMES)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(s for s in app.screen_stack if isinstance(s, ModelSelectModal))
        for btn in modal.query("Button"):
            assert btn.id is not None, "Button missing id"
            assert textual_id.match(btn.id), f"invalid Textual id: {btn.id!r}"


# ---------------------------------------------------------------------------
# Dismissal returns the correct model string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clicking_model_button_dismisses_with_correct_name() -> None:
    """Clicking the Nth button must dismiss with model_list[N]."""
    app = _Harness(REAL_MODEL_NAMES)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(s for s in app.screen_stack if isinstance(s, ModelSelectModal))
        # Click the third entry (qwen2.5-coder:32b)
        await pilot.click(f"#{modal.id} #model-2") if modal.id else None
        # If css-selector form above didn't apply (no app id), drive directly:
        if app.result is None:
            target = modal.query_one("#model-2")
            await pilot.click(target)
        await pilot.pause()
        assert app.result == "qwen2.5-coder:32b"


@pytest.mark.asyncio
async def test_cancel_button_returns_empty_string() -> None:
    """Cancel button must dismiss with an empty string (signals 'no selection')."""
    app = _Harness(REAL_MODEL_NAMES)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(s for s in app.screen_stack if isinstance(s, ModelSelectModal))
        cancel = modal.query_one("#cancel")
        await pilot.click(cancel)
        await pilot.pause()
        assert app.result == ""


# ---------------------------------------------------------------------------
# Empty-list edge case — must not crash and must still show Cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modal_with_empty_model_list_still_composes() -> None:
    """Empty model_list must still render Cancel without raising."""
    app = _Harness([])
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(s for s in app.screen_stack if isinstance(s, ModelSelectModal))
        cancel = modal.query_one("#cancel")
        await pilot.click(cancel)
        await pilot.pause()
        assert app.result == ""


# ===========================================================================
# Generic harness for the remaining modals
# ===========================================================================


class _ModalHarness(App[Any]):
    """Mount an arbitrary ModalScreen and record its dismiss value."""

    def __init__(self, modal_factory: Any) -> None:
        super().__init__()
        self._factory = modal_factory
        self.result: Any = None
        self.result_set: bool = False

    def on_mount(self) -> None:
        def _cb(value: Any) -> None:
            self.result = value
            self.result_set = True

        self.push_screen(self._factory(), callback=_cb)


# ---------------------------------------------------------------------------
# HelpModal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_modal_composes() -> None:
    app = _ModalHarness(HelpModal)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert any(isinstance(s, HelpModal) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_help_modal_close_button_dismisses_true() -> None:
    app = _ModalHarness(HelpModal)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        modal = next(s for s in app.screen_stack if isinstance(s, HelpModal))
        # Drive the handler directly so the test is robust against the
        # modal extending past the simulated viewport when other tests
        # have mutated SPARKLINE_WIDTH/HISTORY_LEN module globals.
        btn = modal.query_one("#close-help")
        from textual.widgets import Button
        modal.on_button_pressed(Button.Pressed(btn))
        await pilot.pause()
        assert app.result is True


@pytest.mark.asyncio
async def test_help_modal_escape_dismisses_false() -> None:
    app = _ModalHarness(HelpModal)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is False


# ---------------------------------------------------------------------------
# ConfirmActionModal
# ---------------------------------------------------------------------------


def _confirm_factory() -> ConfirmActionModal:
    return ConfirmActionModal("Restart", "Really restart?")


@pytest.mark.asyncio
async def test_confirm_action_modal_composes() -> None:
    app = _ModalHarness(_confirm_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, ConfirmActionModal)
        )
        # Title + details rendered.
        assert modal.action_name == "Restart"
        assert modal.action_details == "Really restart?"


@pytest.mark.asyncio
async def test_confirm_action_yes_dismisses_true() -> None:
    app = _ModalHarness(_confirm_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, ConfirmActionModal)
        )
        await pilot.click(modal.query_one("#confirm-yes"))
        await pilot.pause()
        assert app.result is True


@pytest.mark.asyncio
async def test_confirm_action_no_dismisses_false() -> None:
    app = _ModalHarness(_confirm_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, ConfirmActionModal)
        )
        await pilot.click(modal.query_one("#confirm-no"))
        await pilot.pause()
        assert app.result is False


@pytest.mark.asyncio
async def test_confirm_action_escape_dismisses_false() -> None:
    app = _ModalHarness(_confirm_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is False


# ---------------------------------------------------------------------------
# GPUProcessListModal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_process_list_modal_empty_composes() -> None:
    """With no GPU processes, modal renders 'No GPU compute processes found'."""
    with patch(
        "bastion.dashboard.modals.SystemDataCollector.query_gpu_processes",
        return_value=[],
    ):
        app = _ModalHarness(GPUProcessListModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
            )
            assert modal._procs == []
            # Cancel still present.
            assert modal.query_one("#gpuproc-cancel") is not None


@pytest.mark.asyncio
async def test_gpu_process_list_modal_populated_dismisses_with_pid() -> None:
    # The modal now reads the cached ProcessSnapshot (spec 5.3), not a
    # subprocess; feed it via the snapshot harness.
    snapshot = {
        "top_processes": [],
        "gpu_processes": [
            {"pid": 12345, "name": "ollama", "vram_mb": 8192, "sm_pct": None,
             "mem_pct": None, "enc_pct": None, "dec_pct": None,
             "is_inference_owned": True, "role": "ollama"},
            {"pid": 67890, "name": "python", "vram_mb": 2048, "sm_pct": None,
             "mem_pct": None, "enc_pct": None, "dec_pct": None,
             "is_inference_owned": False, "role": None},
        ],
        "own_pids": {}, "watchlist_hits": [], "recent_churn_events": [],
        "collected_at": 1.0, "gpu_collected_at": 1.0,
    }
    app = _SnapshotHarness(snapshot)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
        )
        # Click the first process button.
        await pilot.click(modal.query_one("#gpuproc-12345"))
        await pilot.pause()
        assert app.result == "12345"


@pytest.mark.asyncio
async def test_gpu_process_list_modal_cancel_returns_empty() -> None:
    with patch(
        "bastion.dashboard.modals.SystemDataCollector.query_gpu_processes",
        return_value=[{"pid": "1", "name": "n", "vram_mb": "1"}],
    ):
        app = _ModalHarness(GPUProcessListModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
            )
            await pilot.click(modal.query_one("#gpuproc-cancel"))
            await pilot.pause()
            assert app.result == ""


@pytest.mark.asyncio
async def test_gpu_process_list_modal_escape_returns_empty() -> None:
    with patch(
        "bastion.dashboard.modals.SystemDataCollector.query_gpu_processes",
        return_value=[],
    ):
        app = _ModalHarness(GPUProcessListModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.result == ""


@pytest.mark.asyncio
async def test_gpu_process_list_button_ids_textual_safe() -> None:
    """Process PIDs are integers in string form; ids must still be valid."""
    snapshot = {
        "top_processes": [],
        "gpu_processes": [
            {"pid": 1234, "name": "ollama", "vram_mb": 1024, "sm_pct": None,
             "mem_pct": None, "enc_pct": None, "dec_pct": None,
             "is_inference_owned": True, "role": "ollama"},
            {"pid": 5678, "name": "x", "vram_mb": 2048, "sm_pct": None,
             "mem_pct": None, "enc_pct": None, "dec_pct": None,
             "is_inference_owned": False, "role": None},
        ],
        "own_pids": {}, "watchlist_hits": [], "recent_churn_events": [],
        "collected_at": 1.0, "gpu_collected_at": 1.0,
    }
    app = _SnapshotHarness(snapshot)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
        )
        for btn in modal.query("Button"):
            assert btn.id is not None
            assert _TEXTUAL_ID.match(btn.id), f"bad id: {btn.id!r}"


# ---------------------------------------------------------------------------
# GPUProcessListModal — reads the cached snapshot, NOT a subprocess (T1, 5.3)
# ---------------------------------------------------------------------------


class _SnapshotHarness(App[Any]):
    """App that exposes ``_last_process_snapshot`` (the cached attribution dict).

    The refactored ``GPUProcessListModal`` must read the GPU rows from
    ``app._last_process_snapshot`` (a ``ProcessSnapshot`` dict served by the
    broker's 10s slow tick) and must **not** spawn an nvidia-smi subprocess on
    open (spec 5.3 — moves the modal off the UI-thread subprocess).
    """

    def __init__(self, snapshot: Any) -> None:
        super().__init__()
        self._last_process_snapshot: Any = snapshot
        self.result: Any = None
        self.result_set: bool = False

    def on_mount(self) -> None:
        def _cb(value: Any) -> None:
            self.result = value
            self.result_set = True

        self.push_screen(GPUProcessListModal(), callback=_cb)


_SNAPSHOT_WITH_GPU = {
    "top_processes": [],
    "gpu_processes": [
        {"pid": 12345, "name": "ollama", "vram_mb": 8192, "sm_pct": 80,
         "mem_pct": 40, "enc_pct": None, "dec_pct": None,
         "is_inference_owned": True, "role": "ollama"},
        {"pid": 67890, "name": "python", "vram_mb": 2048, "sm_pct": 10,
         "mem_pct": 5, "enc_pct": None, "dec_pct": None,
         "is_inference_owned": False, "role": None},
    ],
    "own_pids": {"12345": "ollama"},
    "watchlist_hits": [],
    "recent_churn_events": [],
    "collected_at": 1.0,
    "gpu_collected_at": 1.0,
}


@pytest.mark.asyncio
async def test_gpu_modal_reads_cached_snapshot_no_subprocess() -> None:
    """compose() must read app._last_process_snapshot and NOT call the subprocess."""
    with patch(
        "bastion.dashboard.modals.SystemDataCollector.query_gpu_processes",
    ) as mocked:
        app = _SnapshotHarness(_SNAPSHOT_WITH_GPU)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
            )
            # The subprocess bridge must NOT have been invoked on open.
            mocked.assert_not_called()
            # The cached GPU rows are surfaced as kill buttons.
            assert modal._procs[0]["pid"] == "12345"
            await pilot.click(modal.query_one("#gpuproc-12345"))
            await pilot.pause()
            assert app.result == "12345"


@pytest.mark.asyncio
async def test_gpu_modal_empty_cached_snapshot_shows_no_processes() -> None:
    """An empty cached snapshot (StubBackend / no GPU) shows the no-process label."""
    empty = dict(_SNAPSHOT_WITH_GPU)
    empty["gpu_processes"] = []
    with patch(
        "bastion.dashboard.modals.SystemDataCollector.query_gpu_processes",
    ) as mocked:
        app = _SnapshotHarness(empty)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
            )
            mocked.assert_not_called()
            assert modal._procs == []
            assert modal.query_one("#gpuproc-cancel") is not None


@pytest.mark.asyncio
async def test_gpu_modal_no_snapshot_attr_degrades_gracefully() -> None:
    """A host app with no _last_process_snapshot yet renders empty, no crash."""
    with patch(
        "bastion.dashboard.modals.SystemDataCollector.query_gpu_processes",
    ) as mocked:
        app = _SnapshotHarness(None)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, GPUProcessListModal)
            )
            mocked.assert_not_called()
            assert modal._procs == []


# ---------------------------------------------------------------------------
# ConfirmGPUKillModal
# ---------------------------------------------------------------------------


def _kill_factory() -> ConfirmGPUKillModal:
    return ConfirmGPUKillModal(pid="9999", name="ollama", vram_mb="4096")


@pytest.mark.asyncio
async def test_confirm_gpu_kill_normal_dismisses_kill() -> None:
    app = _ModalHarness(_kill_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, ConfirmGPUKillModal)
        )
        await pilot.click(modal.query_one("#kill-normal"))
        await pilot.pause()
        assert app.result == "kill"


@pytest.mark.asyncio
async def test_confirm_gpu_kill_force_dismisses_kill_9() -> None:
    app = _ModalHarness(_kill_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, ConfirmGPUKillModal)
        )
        await pilot.click(modal.query_one("#kill-force"))
        await pilot.pause()
        assert app.result == "kill-9"


@pytest.mark.asyncio
async def test_confirm_gpu_kill_cancel_dismisses_empty() -> None:
    app = _ModalHarness(_kill_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = next(
            s for s in app.screen_stack if isinstance(s, ConfirmGPUKillModal)
        )
        await pilot.click(modal.query_one("#kill-cancel"))
        await pilot.pause()
        assert app.result == ""


@pytest.mark.asyncio
async def test_confirm_gpu_kill_escape_dismisses_empty() -> None:
    app = _ModalHarness(_kill_factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result == ""


# ---------------------------------------------------------------------------
# FanControlModal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fan_modal_available_speed_button_dismisses_with_speed() -> None:
    """With fan control available, each speed button dismisses with its number."""
    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ):
        app = _ModalHarness(FanControlModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, FanControlModal)
            )
            await pilot.click(modal.query_one("#fan-70"))
            await pilot.pause()
            assert app.result == "70"


@pytest.mark.asyncio
@pytest.mark.parametrize("button_id,expected", [
    ("fan-30", "30"),
    ("fan-50", "50"),
    ("fan-90", "90"),
    ("fan-100", "100"),
    ("fan-auto", "auto"),
])
async def test_fan_modal_each_speed_dismisses_with_correct_value(
    button_id: str, expected: str
) -> None:
    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ):
        app = _ModalHarness(FanControlModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, FanControlModal)
            )
            await pilot.click(modal.query_one(f"#{button_id}"))
            await pilot.pause()
            assert app.result == expected


@pytest.mark.asyncio
async def test_fan_modal_toggle_auto_dismisses_with_special_value() -> None:
    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ):
        app = _ModalHarness(FanControlModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, FanControlModal)
            )
            await pilot.click(modal.query_one("#fan-toggle-auto"))
            await pilot.pause()
            assert app.result == "toggle-auto"


@pytest.mark.asyncio
async def test_fan_modal_cancel_dismisses_empty() -> None:
    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ):
        app = _ModalHarness(FanControlModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, FanControlModal)
            )
            await pilot.click(modal.query_one("#fan-cancel"))
            await pilot.pause()
            assert app.result == ""


@pytest.mark.asyncio
async def test_fan_modal_escape_dismisses_empty() -> None:
    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ):
        app = _ModalHarness(FanControlModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.result == ""


@pytest.mark.asyncio
async def test_fan_modal_unavailable_shows_close_button() -> None:
    """When fan control wrapper missing, only Close button is offered."""
    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=False
    ):
        app = _ModalHarness(FanControlModal)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = next(
                s for s in app.screen_stack if isinstance(s, FanControlModal)
            )
            # Close button uses the same id ('fan-cancel').
            await pilot.click(modal.query_one("#fan-cancel"))
            await pilot.pause()
            assert app.result == ""


# ---------------------------------------------------------------------------
# Module-wide invariant: every modal's button ids match Textual's rule.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_modal_button_ids_textual_safe() -> None:
    """Each modal in turn: every Button id matches Textual's identifier rule."""
    factories: list[Any] = [
        HelpModal,
        _confirm_factory,
        _kill_factory,
        FanControlModal,
        lambda: ModelSelectModal("pick", ["a:b", "c.d"]),
    ]
    for factory in factories:
        app = _ModalHarness(factory)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = app.screen_stack[-1]
            for btn in modal.query("Button"):
                assert btn.id is not None
                assert _TEXTUAL_ID.match(btn.id), (
                    f"{type(modal).__name__} bad id: {btn.id!r}"
                )


# ---------------------------------------------------------------------------
# Module-level helpers: fan_control_available + set_fan_speed
# ---------------------------------------------------------------------------


def test_fan_control_available_returns_bool() -> None:
    from bastion.dashboard.modals import fan_control_available

    assert isinstance(fan_control_available(), bool)


def test_set_fan_speed_when_wrapper_missing_returns_false() -> None:
    from bastion.dashboard.modals import set_fan_speed

    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=False
    ):
        ok, msg = set_fan_speed("70")
        assert ok is False
        assert "wrapper not found" in msg


def test_set_fan_speed_subprocess_success() -> None:
    from bastion.dashboard.modals import set_fan_speed

    class _Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ), patch(
        "bastion.dashboard.modals.subprocess.run", return_value=_Result()
    ):
        ok, msg = set_fan_speed("70")
        assert ok is True
        assert msg == "ok"


def test_set_fan_speed_subprocess_failure() -> None:
    from bastion.dashboard.modals import set_fan_speed

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "permission denied\n"

    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ), patch(
        "bastion.dashboard.modals.subprocess.run", return_value=_Result()
    ):
        ok, msg = set_fan_speed("70")
        assert ok is False
        assert msg == "permission denied"


def test_set_fan_speed_timeout_returns_false() -> None:
    import subprocess

    from bastion.dashboard.modals import set_fan_speed

    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ), patch(
        "bastion.dashboard.modals.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=10),
    ):
        ok, msg = set_fan_speed("70")
        assert ok is False
        assert "timed out" in msg


def test_set_fan_speed_generic_exception_returns_false() -> None:
    from bastion.dashboard.modals import set_fan_speed

    with patch(
        "bastion.dashboard.modals.fan_control_available", return_value=True
    ), patch(
        "bastion.dashboard.modals.subprocess.run",
        side_effect=FileNotFoundError("no sudo"),
    ):
        ok, msg = set_fan_speed("70")
        assert ok is False
        assert "no sudo" in msg
