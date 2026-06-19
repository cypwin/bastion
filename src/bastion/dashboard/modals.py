"""Modal dialogs for the BASTION dashboard."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from bastion.dashboard.collectors import SystemDataCollector

# ---------------------------------------------------------------------------
# Fan control constants and helper
# ---------------------------------------------------------------------------

FAN_WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts"
    / "gpu_fan_control_wrapper.py"
)
FAN_PYTHON_PATH = Path(sys.executable)


def fan_control_available() -> bool:
    """Check whether fan control prerequisites are met.

    Requires the wrapper script to exist on disk.  Fan control also needs
    ``nvidia-settings``, X11, and sudo NOPASSWD — but those are checked
    at runtime when the user actually tries to set a speed.
    """
    return FAN_WRAPPER_PATH.exists()


def set_fan_speed(speed: str) -> tuple[bool, str]:
    """Set GPU fan speed via the wrapper script.

    Returns
    -------
    tuple[bool, str]
        ``(success, message)``
    """
    if not fan_control_available():
        return False, "fan control wrapper not found (requires source install)"
    try:
        result = subprocess.run(
            ["sudo", str(FAN_PYTHON_PATH), str(FAN_WRAPPER_PATH), speed],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or "unknown error"
    except subprocess.TimeoutExpired:
        return False, "fan control timed out"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Confirm action modal
# ---------------------------------------------------------------------------

class ConfirmActionModal(ModalScreen[bool]):
    """Generic confirmation dialog for destructive actions."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ConfirmActionModal {
        align: center middle;
    }

    #confirm-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #confirm-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }

    Button {
        margin: 0 2;
    }
    """

    def __init__(self, action: str, details: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action_name = action
        self.action_details = details

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Confirm: {self.action_name}", id="confirm-title")
            yield Label(self.action_details, id="confirm-details")
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", variant="error", id="confirm-yes")
                yield Button("Cancel", variant="primary", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")

    def action_dismiss_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Model selection modal
# ---------------------------------------------------------------------------

class ModelSelectModal(ModalScreen[str]):
    """Modal to select a model from a list."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ModelSelectModal {
        align: center middle;
    }

    #select-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, title: str, models: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.title_text = title
        self.model_list = models

    def compose(self) -> ComposeResult:
        with Vertical(id="select-dialog"):
            yield Label(self.title_text)
            for idx, model in enumerate(self.model_list):
                # Index-based id — model names contain ':' and '.' which
                # Textual rejects as widget ids.
                yield Button(model, id=f"model-{idx}")
            yield Button("Cancel", variant="primary", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "cancel" or btn_id is None:
            self.dismiss("")
            return
        if btn_id.startswith("model-"):
            try:
                idx = int(btn_id[len("model-"):])
                self.dismiss(self.model_list[idx])
                return
            except (ValueError, IndexError):
                pass
        self.dismiss("")

    def action_dismiss_cancel(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# Help modal
# ---------------------------------------------------------------------------

class HelpModal(ModalScreen[bool]):
    """Help overlay showing all keyboard bindings."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }

    #help-dialog {
        width: 65;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #help-title {
        text-align: center;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        from bastion.dashboard.helpers import HISTORY_LEN, SPARKLINE_WIDTH
        with Vertical(id="help-dialog"):
            yield Label("BASTION Dashboard v2 -- Keyboard Shortcuts", id="help-title")
            yield Label("")
            yield Label(" MONITORING")
            yield Label("  [h]  Show this help overlay")
            yield Label("  [r]  Force refresh all panels")
            yield Label("  [q]  Quit the dashboard")
            yield Label("")
            yield Label(" LAYOUT")
            yield Label("  [1]  Compact layout (1-column: GPU focused)")
            yield Label("  [2]  Standard layout (2-column: GPU + system)")
            yield Label("  [3]  Full layout (3-column: all panels)")
            yield Label("  [t]  Toggle secondary panels (trace/A2A/leases/audit)")
            yield Label("")
            yield Label(" SPARKLINES")
            yield Label(f"  [+]  Wider sparklines (+5 chars, now {SPARKLINE_WIDTH})")
            yield Label(f"  [-]  Narrower sparklines (-5 chars, now {SPARKLINE_WIDTH})")
            yield Label(f"  []]  Longer history (+30 samples, now {HISTORY_LEN})")
            yield Label(f"  [[]  Shorter history (-30 samples, now {HISTORY_LEN})")
            yield Label("")
            yield Label(" GPU / MODELS")
            yield Label("  [f]  GPU fan control (30/50/70/90/100%/auto)")
            yield Label("  [g]  Kill a GPU process")
            yield Label("  [p]  Preload a model into VRAM")
            yield Label("  [u]  Unload a model from VRAM")
            yield Label("")
            yield Label(" BROKER")
            yield Label("  [d]  Toggle drain mode (pause/resume scheduling)")
            yield Label("  [s]  Restart bastion.service (requires sudoers)")
            yield Label("")
            yield Label("  Data refreshes automatically at the configured interval.")
            yield Label("  Connection indicator shows STALE when broker unreachable.")
            yield Label("")
            with Horizontal(id="confirm-buttons"):
                yield Button("Close", variant="primary", id="close-help")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Fan control modal
# ---------------------------------------------------------------------------

class FanControlModal(ModalScreen[str]):
    """Fan speed selection modal with auto-trigger toggle."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    FanControlModal {
        align: center middle;
    }

    #fan-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #fan-title {
        text-align: center;
        text-style: bold;
    }

    #fan-row-low, #fan-row-high, #fan-row-actions {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #fan-row-low Button, #fan-row-high Button, #fan-row-actions Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        available = fan_control_available()
        auto_fan = getattr(self.app, "_auto_fan_enabled", False)
        auto_speed = getattr(self.app, "_auto_fan_speed", None)
        auto_status = "ON" if auto_fan else "OFF"
        applied = f"{auto_speed}%" if auto_speed else "auto"
        auto_detail = (
            f" (CPU 60→30 70→50 80→90 85+→100, GPU-safe floor; now {applied})"
            if auto_fan else ""
        )

        with Vertical(id="fan-dialog"):
            yield Label("GPU Fan Control", id="fan-title")
            if not available:
                yield Label("Fan control unavailable.")
                yield Label("Requires source install with scripts/ directory,")
                yield Label("nvidia-settings, X11, and sudo NOPASSWD.")
                yield Label("")
                with Horizontal(id="fan-row-actions"):
                    yield Button("Close", id="fan-cancel", variant="primary")
            else:
                yield Label("Press a button to set fan speed:")
                yield Label("")
                with Horizontal(id="fan-row-low"):
                    yield Button("30%", id="fan-30")
                    yield Button("50%", id="fan-50")
                    yield Button("70%", id="fan-70")
                with Horizontal(id="fan-row-high"):
                    yield Button("90%", id="fan-90")
                    yield Button("100%", id="fan-100", variant="error")
                    yield Button("Auto", id="fan-auto", variant="success")
                yield Label("")
                yield Label(f"Auto-trigger: {auto_status}{auto_detail}")
                with Horizontal(id="fan-row-actions"):
                    yield Button(
                        f"Auto-trigger: {auto_status}",
                        id="fan-toggle-auto",
                        variant="warning" if auto_fan else "default",
                    )
                    yield Button("Cancel", id="fan-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "fan-cancel":
            self.dismiss("")
        elif btn_id == "fan-toggle-auto":
            self.dismiss("toggle-auto")
        elif btn_id and btn_id.startswith("fan-"):
            speed = btn_id.replace("fan-", "")
            self.dismiss(speed)

    def action_dismiss_cancel(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# GPU process list modal
# ---------------------------------------------------------------------------

class GPUProcessListModal(ModalScreen[str]):
    """List GPU processes and select one to kill."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    GPUProcessListModal {
        align: center middle;
    }

    #gpuproc-dialog {
        width: 70;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }

    #gpuproc-title {
        text-align: center;
        text-style: bold;
    }

    #gpuproc-dialog Button {
        margin: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._procs: list[dict[str, str]] = []

    @staticmethod
    def _rows_from_snapshot(snapshot: Any) -> list[dict[str, str]]:
        """Normalize ``ProcessSnapshot.gpu_processes`` to the modal's row shape.

        Reads the cached snapshot (a ``model_dump()`` dict the broker's 10s slow
        tick maintains) and returns one ``{pid, name, vram_mb}`` string-keyed row
        per GPU process — the shape ``compose`` / ``on_button_pressed`` and the
        kill confirmation flow already consume. A missing snapshot, a missing
        ``gpu_processes`` key, or a malformed row degrades to ``[]`` (no GPU rows,
        no crash — StubBackend / no-GPU host).
        """
        if not snapshot:
            return []
        gpu_rows = snapshot.get("gpu_processes") if isinstance(snapshot, dict) else None
        if not gpu_rows:
            return []
        out: list[dict[str, str]] = []
        for row in gpu_rows:
            try:
                pid = row.get("pid")
                if pid is None:
                    continue
                vram = row.get("vram_mb")
                out.append({
                    "pid": str(pid),
                    "name": str(row.get("name") or ""),
                    "vram_mb": str(vram) if vram is not None else "?",
                })
            except AttributeError:
                continue
        return out

    def compose(self) -> ComposeResult:
        # Read the broker-maintained cached snapshot instead of spawning an
        # nvidia-smi subprocess on open (spec 5.3 — moves the modal off the
        # UI-thread subprocess; the always-on ProcessAttributionPanel owns
        # collection now). ``app._last_process_snapshot`` may be absent on a
        # host app that does not poll /broker/processes -> empty (graceful).
        snapshot = getattr(self.app, "_last_process_snapshot", None)
        self._procs = self._rows_from_snapshot(snapshot)
        with Vertical(id="gpuproc-dialog"):
            yield Label("GPU Processes", id="gpuproc-title")
            if self._procs:
                yield Label("Select a process to kill:")
                yield Label("")
                for proc in self._procs[:9]:
                    label = (
                        f"{proc['name']:<20s}  PID {proc['pid']:>7s}"
                        f"  {proc['vram_mb']:>6s} MB"
                    )
                    yield Button(label, id=f"gpuproc-{proc['pid']}")
            else:
                yield Label("No GPU compute processes found.")
            yield Label("")
            yield Button("Cancel", id="gpuproc-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "gpuproc-cancel" or btn_id is None:
            self.dismiss("")
        elif btn_id.startswith("gpuproc-"):
            pid = btn_id.replace("gpuproc-", "")
            self.dismiss(pid)

    def action_dismiss_cancel(self) -> None:
        self.dismiss("")


# ---------------------------------------------------------------------------
# Confirm GPU kill modal
# ---------------------------------------------------------------------------

class ConfirmGPUKillModal(ModalScreen[str]):
    """Confirm kill of a GPU process with normal and force options."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel", show=False)]

    DEFAULT_CSS = """
    ConfirmGPUKillModal {
        align: center middle;
    }

    #gpukill-title {
        text-align: center;
        text-style: bold;
    }

    #gpukill-buttons {
        width: 100%;
        height: auto;
        align: center middle;
    }

    #gpukill-buttons Button {
        margin: 0 1;
    }

    #gpukill-dialog {
        width: 60;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, pid: str, name: str, vram_mb: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.proc_pid = pid
        self.proc_name = name
        self.proc_vram = vram_mb

    def compose(self) -> ComposeResult:
        with Vertical(id="gpukill-dialog"):
            yield Label("Kill GPU Process?", id="gpukill-title")
            yield Label(f"PID:  {self.proc_pid}")
            yield Label(f"Name: {self.proc_name}")
            yield Label(f"VRAM: {self.proc_vram} MB")
            yield Label("")
            with Horizontal(id="gpukill-buttons"):
                yield Button("Kill", id="kill-normal", variant="warning")
                yield Button("Force Kill (SIGKILL)", id="kill-force", variant="error")
                yield Button("Cancel", id="kill-cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "kill-cancel":
            self.dismiss("")
        elif btn_id == "kill-normal":
            self.dismiss("kill")
        elif btn_id == "kill-force":
            self.dismiss("kill-9")

    def action_dismiss_cancel(self) -> None:
        self.dismiss("")
