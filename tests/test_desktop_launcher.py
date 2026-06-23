"""Hermetic tests for the desktop-app launch path.

Covers ``scripts/launch_dashboard.sh`` (conda-env resolution + fail-loud) and
``scripts/install-desktop.sh`` (XDG entry generation + active-env baking).

Design notes
------------
* Fully hermetic: every test builds a throwaway ``$HOME`` with *fake* conda
  envs (a ``python`` shell-stub that exits 0/1 on the import probe) and runs
  the scripts under a minimal ``PATH`` with no real conda. Nothing depends on
  the host having a particular env installed.
* ``BASTION_LAUNCH_SELFTEST=1`` makes ``launch_dashboard.sh`` resolve Python,
  verify deps, and exit *before* touching the GPU / Ollama / broker — so the
  suite never starts a service or calls sudo.
* ``stdin`` is always closed (``DEVNULL``); ``keep_open`` is a TTY-only no-op,
  so a fail path can never block the test on a key-press.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_SH = REPO_ROOT / "scripts" / "launch_dashboard.sh"
INSTALL_SH = REPO_ROOT / "scripts" / "install-desktop.sh"
TEMPLATE = REPO_ROOT / "packaging" / "bastion-dashboard.desktop.in"

# A POSIX shell + the coreutils the scripts call must exist.
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _make_fake_env(home: Path, name: str, *, deps_ok: bool) -> Path:
    """Create ``$home/miniforge3/envs/<name>/bin/python`` as a probe stub.

    The stub exits 0 (deps importable) or 1 (missing) for the
    ``python -c "import ..."`` checks the launcher runs.
    """
    bindir = home / "miniforge3" / "envs" / name / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py = bindir / "python"
    py.write_text(f"#!/bin/bash\nexit {0 if deps_ok else 1}\n")
    py.chmod(0o755)
    return py


def _run_launch(home: Path, tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    """Run launch_dashboard.sh in selftest mode under a clean, conda-free env."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(run_dir),
        "BASTION_LAUNCH_SELFTEST": "1",
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(LAUNCH_SH)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# launch_dashboard.sh — Python/env resolution
# ---------------------------------------------------------------------------

def test_autodetects_env_not_named_bastion(tmp_path: Path) -> None:
    """The original bug: only an env literally named 'bastion' was found.

    A deps-capable env under any other name must now be auto-detected.
    """
    home = tmp_path / "home"
    home.mkdir()
    py = _make_fake_env(home, "phenotype", deps_ok=True)

    result = _run_launch(home, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Dashboard deps present" in result.stdout
    # Resolved interpreter is the fake env's python, not some system python.
    assert str(py) in result.stdout


def test_skips_envs_missing_deps_and_picks_capable_one(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _make_fake_env(home, "aaa_broken", deps_ok=False)
    good = _make_fake_env(home, "zzz_good", deps_ok=True)

    result = _run_launch(home, tmp_path)

    assert result.returncode == 0, result.stderr
    assert str(good) in result.stdout


def test_fail_loud_when_no_capable_env(tmp_path: Path) -> None:
    """No env with deps anywhere -> exit 1, visible message, no hang."""
    home = tmp_path / "home"
    home.mkdir()
    _make_fake_env(home, "broken", deps_ok=False)

    result = _run_launch(home, tmp_path)

    assert result.returncode == 1
    assert "[!!]" in result.stderr
    # Either flavour of the actionable error is acceptable.
    assert (
        "No Python found" in result.stderr
        or "missing required packages" in result.stderr
    )
    assert "BASTION_CONDA_ENV" in result.stderr


def test_explicit_env_that_cannot_activate_dies(tmp_path: Path) -> None:
    """BASTION_CONDA_ENV set but unactivatable (no conda) -> die, exit 1."""
    home = tmp_path / "home"
    home.mkdir()
    # A capable env exists, but the explicit request must take precedence and
    # fail loudly rather than silently falling through to auto-detect.
    _make_fake_env(home, "phenotype", deps_ok=True)

    result = _run_launch(home, tmp_path, BASTION_CONDA_ENV="does_not_exist")

    assert result.returncode == 1
    assert "could not be activated" in result.stderr


def test_selftest_exits_before_starting_services(tmp_path: Path) -> None:
    """Selftest must short-circuit before any GPU/Ollama/broker output."""
    home = tmp_path / "home"
    home.mkdir()
    _make_fake_env(home, "phenotype", deps_ok=True)

    result = _run_launch(home, tmp_path)

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    # None of the service-startup banners should appear.
    for banner in ("Ollama", "BASTION broker", "nvidia-modprobe"):
        assert banner not in combined


def test_launcher_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(LAUNCH_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# install-desktop.sh — XDG entry generation
# ---------------------------------------------------------------------------

def _run_install(
    tmp_path: Path, *args: str, conda_env: str | None
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    data_home = tmp_path / "data"
    config_home = tmp_path / "cfg"
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_DATA_HOME": str(data_home),
        "XDG_CONFIG_HOME": str(config_home),
    }
    if conda_env is not None:
        env["CONDA_DEFAULT_ENV"] = conda_env
    result = subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    app_entry = data_home / "applications" / "bastion-dashboard.desktop"
    autostart_entry = config_home / "autostart" / "bastion-dashboard.desktop"
    return result, app_entry, autostart_entry


def test_install_writes_app_and_autostart(tmp_path: Path) -> None:
    result, app_entry, autostart_entry = _run_install(
        tmp_path, "install", conda_env="phenotype"
    )
    assert result.returncode == 0, result.stderr
    assert app_entry.is_file()
    assert autostart_entry.is_file()


def test_install_bakes_active_conda_env_into_exec(tmp_path: Path) -> None:
    _, app_entry, _ = _run_install(tmp_path, "install", conda_env="phenotype")
    exec_line = next(
        ln for ln in app_entry.read_text().splitlines() if ln.startswith("Exec=")
    )
    assert "env BASTION_CONDA_ENV=phenotype" in exec_line
    assert "launch_dashboard.sh" in exec_line


def test_install_omits_env_prefix_for_base(tmp_path: Path) -> None:
    """A 'base' (or unset) env must not bake a misleading BASTION_CONDA_ENV."""
    _, app_entry, _ = _run_install(tmp_path, "install", conda_env="base")
    exec_line = next(
        ln for ln in app_entry.read_text().splitlines() if ln.startswith("Exec=")
    )
    assert "BASTION_CONDA_ENV" not in exec_line
    assert exec_line.endswith("launch_dashboard.sh")


def test_no_autostart_flag_skips_autostart(tmp_path: Path) -> None:
    result, app_entry, autostart_entry = _run_install(
        tmp_path, "--no-autostart", conda_env="phenotype"
    )
    assert result.returncode == 0, result.stderr
    assert app_entry.is_file()
    assert not autostart_entry.exists()


def test_uninstall_removes_entries(tmp_path: Path) -> None:
    _run_install(tmp_path, "install", conda_env="phenotype")
    result, app_entry, autostart_entry = _run_install(
        tmp_path, "uninstall", conda_env=None
    )
    assert result.returncode == 0, result.stderr
    assert not app_entry.exists()
    assert not autostart_entry.exists()


def test_generated_entry_is_valid_desktop_file(tmp_path: Path) -> None:
    _, app_entry, _ = _run_install(tmp_path, "install", conda_env="phenotype")
    text = app_entry.read_text()
    assert text.startswith("[Desktop Entry]")
    assert "Type=Application" in text
    assert "Terminal=true" in text
    # Template placeholder must be fully substituted.
    assert "@LAUNCHER@" not in text


def test_installer_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
