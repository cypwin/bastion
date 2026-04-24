"""Platform-aware directory resolution for BASTION data and config files.

Resolves paths using XDG conventions on Linux, with environment variable
overrides for containerized deployments:

- ``BASTION_DATA_DIR``   — override data directory (audit logs, VRAM journal)
- ``BASTION_CONFIG_DIR`` — override config directory (broker.yaml)

Default locations (Linux):
  - Data:   ``~/.local/share/bastion/``
  - Config: ``~/.config/bastion/``
"""

from __future__ import annotations

import os
from pathlib import Path


def _xdg_data_home() -> Path:
    """Return XDG_DATA_HOME or its default ``~/.local/share``."""
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def _xdg_config_home() -> Path:
    """Return XDG_CONFIG_HOME or its default ``~/.config``."""
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def data_dir() -> Path:
    """Return the BASTION data directory, creating it if needed.

    Override with ``BASTION_DATA_DIR`` environment variable.
    """
    d = Path(os.environ.get("BASTION_DATA_DIR", _xdg_data_home() / "bastion"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    """Return the BASTION config directory, creating it if needed.

    Override with ``BASTION_CONFIG_DIR`` environment variable.
    """
    d = Path(os.environ.get("BASTION_CONFIG_DIR", _xdg_config_home() / "bastion"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def audit_log_path() -> str:
    """Return the path to the audit log file.

    Returns ``str`` for direct use by ``logging.handlers.RotatingFileHandler``.
    """
    return str(data_dir() / "bastion-audit.jsonl")


def vram_journal_path() -> Path:
    """Return the path to the VRAM crash-forensics journal."""
    return data_dir() / "bastion-vram-journal.jsonl"


def database_path() -> Path:
    """Return the default path to the SQLite persistence database."""
    return data_dir() / "bastion.db"


def harden_audit_log() -> None:
    """Set audit log file mode to 0o600 if it exists.

    Protects hashed tokens and tier-2 prompt hashes from other local users.
    Idempotent; no-op when the file does not yet exist.
    """
    path = Path(audit_log_path())
    if not path.exists():
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort; failure to chmod should not crash the service.
        pass
