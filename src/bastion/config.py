"""Configuration loading for BASTION.

Loads broker.yaml and validates with Pydantic models. Falls back to
sensible defaults if no config file is found.  GPU parameters default
to auto-detection via nvidia-smi when not explicitly set.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

from bastion.models import BrokerConfig, ModelInfo

# Avoid circular import — BrokerConfig fields used by _apply_gpu_profile
# are accessed via attribute, not import.

logger = logging.getLogger(__name__)


def _build_config_search_paths() -> list[Path]:
    """Build the ordered list of config search paths.

    ``/etc/bastion/`` is only included on Linux.
    """
    paths = [
        Path("config/broker.yaml"),
        Path("broker.yaml"),
    ]
    if sys.platform == "linux":
        paths.append(Path("/etc/bastion/broker.yaml"))
    paths.append(Path.home() / ".config" / "bastion" / "broker.yaml")
    return paths


_CONFIG_SEARCH_PATHS = _build_config_search_paths()


def load_config(path: Path | None = None) -> BrokerConfig:
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
        logger.info(
            "No config file found \u2014 using defaults. "
            "Run `bastion --init-config` to generate a config file, "
            "then `bastion --detect-models` to discover your Ollama models."
        )
        config = BrokerConfig()
        resolve_gpu_defaults(config)
        _apply_env_overrides(config)
        return config

    logger.info("Loading config from %s", config_path)

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Remember which GPU fields the user explicitly set
    user_gpu = raw.get("gpu", {}) if isinstance(raw.get("gpu"), dict) else {}

    # Transform models section: convert nested dicts to ModelInfo
    if "models" in raw and isinstance(raw["models"], dict):
        raw["models"] = {
            name: ModelInfo(**info) if isinstance(info, dict) else info
            for name, info in raw["models"].items()
        }

    config = BrokerConfig(**raw)
    resolve_gpu_defaults(config, explicit_fields=set(user_gpu.keys()))
    _apply_env_overrides(config)

    # Apply calibrated GPU profile if available
    gpu_profile = _load_gpu_profile()
    if gpu_profile:
        _apply_gpu_profile(config, gpu_profile)

    if not config.models:
        logger.info(
            "No models registered in config. BASTION will estimate VRAM for "
            "unknown models. Run `bastion --detect-models` to auto-discover "
            "installed Ollama models."
        )

    return config


def resolve_gpu_defaults(
    config: BrokerConfig,
    explicit_fields: set[str] | None = None,
) -> None:
    """Auto-detect GPU parameters that weren't explicitly configured.

    Queries ``nvidia-smi`` for total VRAM, GPU name, and TDP.
    Only overwrites fields that were left at their defaults (i.e. not
    present in the user's ``broker.yaml``).

    Parameters
    ----------
    config : BrokerConfig
        Configuration to mutate in-place.
    explicit_fields : set[str], optional
        Field names the user explicitly set in their config file.
        These will NOT be overwritten by auto-detection.
    """
    explicit = explicit_fields or set()

    if "total_vram_gb" in explicit and "max_power_watts" in explicit:
        return  # User specified everything — skip detection

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,power.limit",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

        # Parse first GPU line
        line = result.stdout.strip().split("\n")[0]
        parts = [p.strip() for p in line.split(",")]
        gpu_name = parts[0] if len(parts) > 0 else "Unknown GPU"
        vram_mb = float(parts[1]) if len(parts) > 1 else 0.0
        power_limit_w = float(parts[2]) if len(parts) > 2 else 0.0

        if "total_vram_gb" not in explicit and vram_mb > 0:
            config.gpu.total_vram_gb = round(vram_mb / 1024, 1)

        if "max_power_watts" not in explicit and power_limit_w > 0:
            config.gpu.max_power_watts = power_limit_w

        logger.info(
            "Auto-detected GPU: %s, %.0f MB VRAM (%.1f GB), %.0f W TDP",
            gpu_name, vram_mb, config.gpu.total_vram_gb, config.gpu.max_power_watts,
        )

    except FileNotFoundError:
        logger.info(
            "nvidia-smi not found \u2014 running without GPU monitoring. "
            "Install NVIDIA drivers for full functionality."
        )
        if "total_vram_gb" not in explicit:
            config.gpu.total_vram_gb = 8.0  # Conservative fallback
    except Exception as e:
        logger.warning("GPU auto-detection failed: %s \u2014 using conservative defaults", e)
        if "total_vram_gb" not in explicit:
            config.gpu.total_vram_gb = 8.0


def _apply_env_overrides(config: BrokerConfig) -> None:
    """Apply BASTION_* environment variable overrides to the config.

    Environment variables take highest precedence — they override both
    config file values and defaults.  This is the primary configuration
    mechanism for Docker / CI / systemd environments.

    Supported variables:

    - ``BASTION_OLLAMA_HOST`` — Ollama backend host
    - ``BASTION_OLLAMA_PORT`` — Ollama backend port
    - ``BASTION_PORT`` — BASTION listen port
    - ``BASTION_ADMIN_PORT`` — Admin/A2A port (0 = disabled)
    - ``BASTION_GPU_TOTAL_VRAM_GB`` — Total GPU VRAM in GB
    - ``BASTION_GPU_MAX_TEMP_C`` — Max GPU temperature threshold
    - ``BASTION_GPU_MAX_POWER_W`` — Max GPU power threshold
    - ``BASTION_AUTH_ENABLED`` — Enable auth (true/false/1/0)
    - ``BASTION_API_KEYS`` — Comma-separated admin API keys
    - ``BASTION_AUDIT_TIER`` — Audit tier (1, 2, or 3)
    """
    overrides_applied = []

    def _env_str(key: str) -> str | None:
        return os.environ.get(key)

    def _env_int(key: str) -> int | None:
        val = os.environ.get(key)
        if val is not None:
            try:
                return int(val)
            except ValueError:
                logger.warning("Invalid integer for %s: %r", key, val)
        return None

    def _env_float(key: str) -> float | None:
        val = os.environ.get(key)
        if val is not None:
            try:
                return float(val)
            except ValueError:
                logger.warning("Invalid float for %s: %r", key, val)
        return None

    def _env_bool(key: str) -> bool | None:
        val = os.environ.get(key)
        if val is not None:
            return val.lower() in ("true", "1", "yes")
        return None

    # Ollama
    if (v := _env_str("BASTION_OLLAMA_HOST")) is not None:
        config.ollama.host = v
        overrides_applied.append("BASTION_OLLAMA_HOST")
    if (v := _env_int("BASTION_OLLAMA_PORT")) is not None:
        config.ollama.port = v
        overrides_applied.append("BASTION_OLLAMA_PORT")

    # Server
    if (v := _env_int("BASTION_PORT")) is not None:
        config.server.port = v
        overrides_applied.append("BASTION_PORT")
    if (v := _env_int("BASTION_ADMIN_PORT")) is not None:
        config.server.admin_port = v
        overrides_applied.append("BASTION_ADMIN_PORT")

    # GPU
    if (v := _env_float("BASTION_GPU_TOTAL_VRAM_GB")) is not None:
        config.gpu.total_vram_gb = v
        overrides_applied.append("BASTION_GPU_TOTAL_VRAM_GB")
    if (v := _env_int("BASTION_GPU_MAX_TEMP_C")) is not None:
        config.gpu.max_temperature_c = v
        overrides_applied.append("BASTION_GPU_MAX_TEMP_C")
    if (v := _env_float("BASTION_GPU_MAX_POWER_W")) is not None:
        config.gpu.max_power_watts = v
        overrides_applied.append("BASTION_GPU_MAX_POWER_W")

    # Auth
    if (v := _env_bool("BASTION_AUTH_ENABLED")) is not None:
        config.auth.enabled = v
        overrides_applied.append("BASTION_AUTH_ENABLED")
    if (v := _env_str("BASTION_API_KEYS")) is not None:
        config.auth.api_keys = [k.strip() for k in v.split(",") if k.strip()]
        overrides_applied.append("BASTION_API_KEYS")

    # Audit
    if (v := _env_int("BASTION_AUDIT_TIER")) is not None:
        config.audit.tier = v
        overrides_applied.append("BASTION_AUDIT_TIER")

    # Persistence
    if (v := _env_bool("BASTION_PERSISTENCE_ENABLED")) is not None:
        config.persistence.enabled = v
        overrides_applied.append("BASTION_PERSISTENCE_ENABLED")
    if (v := _env_str("BASTION_PERSISTENCE_DB_PATH")) is not None:
        config.persistence.database_path = v
        overrides_applied.append("BASTION_PERSISTENCE_DB_PATH")

    if overrides_applied:
        logger.info("Config overrides from environment: %s", ", ".join(overrides_applied))


def _load_gpu_profile(path: Path | None = None) -> dict | None:
    """Load calibrated GPU profile if it exists.

    Parameters
    ----------
    path : Path, optional
        Explicit path. If None, checks ~/.config/bastion/gpu-profile.yaml.

    Returns
    -------
    dict or None
        Parsed profile data, or None if no profile exists.
    """
    if path is None:
        from bastion.paths import config_dir
        path = config_dir() / "gpu-profile.yaml"

    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "calibrated" in data:
            logger.info(
                "Using calibrated GPU profile from %s (tested %s on %s)",
                path,
                data.get("tested", {}).get("date", "unknown"),
                data.get("gpu", {}).get("name", "unknown"),
            )
            return data
    except Exception as e:
        logger.warning("Failed to load GPU profile from %s: %s", path, e)

    return None


def _apply_gpu_profile(config: BrokerConfig, profile: dict) -> None:
    """Apply calibrated GPU profile values to config.

    Calibrated values override defaults but NOT explicit user config.
    """
    cal = profile.get("calibrated", {})

    if "cooldown_seconds" in cal:
        config.scheduler.cooldown_seconds = float(cal["cooldown_seconds"])
    if "safe_swap_rate_per_min" in cal:
        config.scheduler.swap_rate_warn_threshold = max(1, cal["safe_swap_rate_per_min"] - 1)
        config.scheduler.swap_rate_critical_threshold = cal["safe_swap_rate_per_min"]
    if "max_concurrent_requests" in cal:
        config.scheduler.max_concurrent_dispatches = cal["max_concurrent_requests"]
    if "thermal_ceiling_c" in cal:
        config.gpu.max_temperature_c = cal["thermal_ceiling_c"]
    if "vram_headroom_mb" in cal:
        config.gpu.headroom_gb = cal["vram_headroom_mb"] / 1024.0

    logger.info("Applied calibrated GPU profile overrides")


def _find_config(explicit_path: Path | None) -> Path | None:
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
