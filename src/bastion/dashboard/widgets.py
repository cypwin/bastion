"""Shared base widget classes for the BASTION dashboard."""
from __future__ import annotations

from textual.widgets import Static


class BastionPanel(Static):
    """Base class for all BASTION dashboard panel widgets.

    Scopes border/padding/min-height styling to actual panel widgets so the
    global Static rule does not leak into modals, the status bar, or helper
    widgets.  Previously the app-level ``Static { ... }`` CSS rule applied to
    every Static subclass in the widget tree, causing spurious focus artifacts
    in modal dialogs.
    """

    DEFAULT_CSS = """
    BastionPanel {
        border: solid $primary-background;
        height: auto;
        min-height: 5;
        padding: 0 1;
    }
    """
