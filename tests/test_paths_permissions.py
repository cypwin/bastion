"""Tests that data files get 0o600 mode."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from bastion.paths import audit_log_path


def test_audit_log_file_is_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))
    log_path = Path(audit_log_path())
    # Create (simulate first log write)
    log_path.touch()
    from bastion.paths import harden_audit_log
    harden_audit_log()
    mode = stat.S_IMODE(log_path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_harden_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))
    from bastion.paths import harden_audit_log
    log_path = Path(audit_log_path())
    log_path.touch()
    harden_audit_log()
    harden_audit_log()  # must not raise
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_harden_noop_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BASTION_DATA_DIR", str(tmp_path))
    from bastion.paths import harden_audit_log
    # File does not exist — must not raise
    harden_audit_log()
