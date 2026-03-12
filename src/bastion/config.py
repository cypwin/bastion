"""Configuration loading for BASTION.

Loads broker.yaml and validates with Pydantic models. Falls back to
sensible defaults if no config file is found.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

from bastion.models import BrokerConfig, ModelInfo

logger = logging.getLogger(__name__)

# Search paths for config file (in order)
_CONFIG_SEARCH_PATHS = [
    Path("config/broker.yaml"),
    Path("broker.yaml"),
    Path("/etc/bastion/broker.yaml"),
    Path.home() / ".config" / "bastion" / "broker.yaml",
]


def load_config(path: Optional[Path] = None) -> BrokerConfig:
    """Load and validate BASTION configuration.

    Parameters
    ----------
    path : Path, optional
        Explicit path to broker.yaml. If None, searches standard locations.

    Returns
    -------
    BrokerConfig
        Validated configuration with defaults for missing fields.
    """
    config_path = _find_config(path)

    if config_path is None:
        logger.warning("No config file found — using defaults")
        return BrokerConfig()

    logger.info("Loading config from %s", config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Transform models section: convert nested dicts to ModelInfo
    if "models" in raw and isinstance(raw["models"], dict):
        raw["models"] = {
            name: ModelInfo(**info) if isinstance(info, dict) else info
            for name, info in raw["models"].items()
        }

    return BrokerConfig(**raw)


def _find_config(explicit_path: Optional[Path]) -> Optional[Path]:
    """Find config file from explicit path or search paths."""
    if explicit_path is not None:
        if explicit_path.exists():
            return explicit_path
        logger.warning("Config file not found: %s", explicit_path)
        return None

    for search_path in _CONFIG_SEARCH_PATHS:
        if search_path.exists():
            return search_path

    return None