"""Regression tests for BASTION dashboard modals.

Originally added after [u] (unload) crashed the dashboard with BadIdentifier
because real Ollama model names like ``granite4.1:8b`` contain ``:`` and ``.``,
which Textual rejects when used inside a widget ``id``. The fix uses index-based
button ids and dereferences the model name on click.
"""

from __future__ import annotations

import pytest
from textual.app import App

from bastion.dashboard.modals import ModelSelectModal

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
