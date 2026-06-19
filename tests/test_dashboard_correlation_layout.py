"""Dashboard layout tests for the correlation + process secondary panels (T5).

Phase 7.1 grows the secondary toggle group from 3 to 5 panels
(``a2a-tasks``/``leases``/``audit-stream`` + the new ``processes`` and
``correlation``) and renders the ``[3]`` full-layout secondary group as a 3+2
two-column sub-grid. These tests mount a real ``BastionDashboard`` (HTTP client
stubbed, periodic tick suppressed) and assert:

  - the ``CorrelationPanel`` (``#correlation``) is composed and queryable;
  - both ``processes`` and ``correlation`` are members of the secondary set;
  - in full layout the secondary panels toggle with ``[t]`` and the
    non-secondary panels toggle inversely (the existing contract still holds
    with the larger set).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bastion.dashboard.app import BastionDashboard
from bastion.dashboard.panels_correlation import CorrelationPanel
from bastion.dashboard.panels_processes import ProcessAttributionPanel


def _stub_app() -> BastionDashboard:
    app = BastionDashboard(url="http://test", interval=3600.0)
    # Suppress the periodic refresh so it does not hit the network during mount.
    app.refresh_data = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return app


@pytest.mark.asyncio
async def test_correlation_panel_is_composed() -> None:
    app = _stub_app()
    async with app.run_test():
        panel = app.query_one("#correlation", CorrelationPanel)
        assert panel is not None


@pytest.mark.asyncio
async def test_processes_panel_is_composed() -> None:
    app = _stub_app()
    async with app.run_test():
        panel = app.query_one("#processes", ProcessAttributionPanel)
        assert panel is not None


@pytest.mark.asyncio
async def test_secondary_set_includes_processes_and_correlation() -> None:
    # _apply_layout owns the secondary_ids set; assert the two new members are
    # toggled as secondaries in full mode (visible only when _show_secondary).
    app = _stub_app()
    async with app.run_test():
        app._layout_mode = "full"
        app._show_secondary = True
        app._apply_layout()
        assert app.query_one("#processes").display is True
        assert app.query_one("#correlation").display is True

        app._show_secondary = False
        app._apply_layout()
        # When secondaries are hidden, the non-secondary panels show instead.
        assert app.query_one("#processes").display is False
        assert app.query_one("#correlation").display is False
        # A non-secondary panel (scheduler) is visible in this state.
        assert app.query_one("#scheduler").display is True
