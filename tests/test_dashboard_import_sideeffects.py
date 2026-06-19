"""Importing the dashboard package (and its submodules) must be side-effect-free.

Regression guard for the argv-parsing entrypoint introduced in commit acd8f65:
``bastion.dashboard.__main__`` ran ``main()`` at module scope, so merely
*importing* it parsed ``sys.argv`` and could call ``sys.exit()`` / print usage.
That bites any code (tests, Phase 3 panels) that imports the package under a
foreign ``argv``.
"""
from __future__ import annotations

import subprocess
import sys

# Modules that must import cleanly regardless of the ambient process argv.
_IMPORT_TARGETS = [
    "bastion.dashboard",
    "bastion.dashboard.__main__",
    "bastion.dashboard.app",
    "bastion.dashboard.panels_system",
    "bastion.dashboard.client",
    "bastion.dashboard.collectors",
    "bastion.dashboard.helpers",
    "bastion.dashboard.modals",
    "bastion.dashboard.statusbar",
    "bastion.dashboard.widgets",
]


def _import_under_hostile_argv(module: str) -> subprocess.CompletedProcess[str]:
    """Import *module* in a subprocess whose argv would break argparse.

    A hostile flag (``--this-flag-does-not-exist``) is planted in ``sys.argv``.
    If importing the module parses argv, argparse aborts with exit code 2 and
    prints a usage line. A side-effect-free import leaves argv untouched and
    exits 0.
    """
    code = (
        "import sys; "
        f"sys.argv = ['pytest', '--this-flag-does-not-exist']; "
        f"import {module}; "
        "print('IMPORT_OK')"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_importing_dashboard_modules_has_no_argv_side_effects() -> None:
    for module in _IMPORT_TARGETS:
        result = _import_under_hostile_argv(module)
        assert result.returncode == 0, (
            f"importing {module} exited {result.returncode} "
            f"(argv was parsed at import time)\nstderr:\n{result.stderr}"
        )
        assert "IMPORT_OK" in result.stdout, (
            f"importing {module} did not reach the post-import print\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # A parse would emit an argparse usage/error banner to stderr.
        assert "usage:" not in result.stderr.lower(), (
            f"importing {module} printed an argparse usage banner\n"
            f"stderr:\n{result.stderr}"
        )
        assert "unrecognized arguments" not in result.stderr.lower(), (
            f"importing {module} parsed argv (unrecognized-arg error)\n"
            f"stderr:\n{result.stderr}"
        )


def test_main_entrypoint_still_exists_and_is_callable() -> None:
    import bastion.dashboard

    assert hasattr(bastion.dashboard, "main")
    assert callable(bastion.dashboard.main)
    # The console-script target (pyproject [project.scripts]) resolves here.
    import inspect

    sig = inspect.signature(bastion.dashboard.main)
    # main(argv=None) — callable with zero positional args.
    assert "argv" in sig.parameters
