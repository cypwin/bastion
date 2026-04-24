"""Pre-flight system validator for BASTION.

Runs a series of checks to verify that the system is ready to run BASTION:
Python version, GPU detection, Ollama connectivity, port availability,
config validation, and file permissions.

Usage::

    bastion --validate
"""

from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from bastion.config import _find_config, load_config
from bastion.gpu_profiles import lookup_profile
from bastion.health import query_gpu_status


class CheckStatus(StrEnum):
    """Result status for a pre-flight check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    """Result of a single pre-flight check."""

    name: str
    status: CheckStatus
    message: str


def check_python_version() -> CheckResult:
    """Check that Python version is >= 3.11."""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 11):
        return CheckResult("Python version", CheckStatus.PASS, version_str)
    return CheckResult(
        "Python version",
        CheckStatus.FAIL,
        f"{version_str} -- Python 3.11+ required",
    )


def _query_gpu_name() -> str | None:
    """Query GPU name from nvidia-smi (sync, for validator only)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


async def check_gpu() -> CheckResult:
    """Check for NVIDIA GPU and query status."""
    gpu_name = _query_gpu_name()
    if gpu_name is None:
        return CheckResult(
            "NVIDIA GPU",
            CheckStatus.FAIL,
            "nvidia-smi not found or no GPU detected -- install NVIDIA drivers",
        )

    status = await query_gpu_status()
    vram_mb = status.vram_total_mb or 0
    driver = _query_driver_version()
    parts = [gpu_name]
    if vram_mb > 0:
        parts.append(f"{vram_mb} MB VRAM")
    if driver:
        parts.append(f"driver {driver}")

    return CheckResult("NVIDIA GPU", CheckStatus.PASS, ", ".join(parts))


def check_gpu_profile(gpu_name: str | None) -> CheckResult:
    """Look up GPU in profile table."""
    if gpu_name is None:
        return CheckResult(
            "GPU profile",
            CheckStatus.WARN,
            "No GPU detected -- cannot look up profile",
        )

    profile = lookup_profile(gpu_name)
    if profile.name == "Unknown GPU":
        return CheckResult(
            "GPU profile",
            CheckStatus.WARN,
            f"'{gpu_name}' not in profile table -- using conservative defaults "
            f"(swap limit {profile.safe_swap_rate}/min, headroom "
            f"{profile.vram_headroom_mb // 1024}GB, thermal {profile.thermal_ceiling_c}C)",
        )

    return CheckResult(
        "GPU profile",
        CheckStatus.PASS,
        f"{profile.name} -- swap limit {profile.safe_swap_rate}/min, "
        f"headroom {profile.vram_headroom_mb // 1024}GB, "
        f"thermal {profile.thermal_ceiling_c}C",
    )


async def check_ollama(
    port: int = 11435,
    host: str = "127.0.0.1",
    proxy_port: int | None = None,
) -> CheckResult:
    """Check if Ollama is reachable on the backend port.

    When ``proxy_port`` is provided and the direct probe to ``port`` fails,
    retry via the BASTION proxy on ``proxy_port``. A successful proxy
    probe yields WARN (not FAIL) so users with locked-down direct access
    (e.g. nftables GID restriction) don't see a misleading failure.
    """
    url = f"http://{host}:{port}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5.0)
        if resp.status_code == 200:
            return CheckResult(
                "Ollama",
                CheckStatus.PASS,
                f"reachable on {host}:{port}",
            )
        direct_err = f"HTTP {resp.status_code}"
    except Exception as e:  # noqa: BLE001
        direct_err = str(e) or "unreachable"

    if proxy_port is None:
        return CheckResult(
            "Ollama",
            CheckStatus.FAIL,
            f"unreachable on {host}:{port} -- is Ollama running on that port?",
        )

    # Fallback via BASTION proxy
    proxy_url = f"http://{host}:{proxy_port}/api/tags"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(proxy_url, timeout=5.0)
        if resp.status_code == 200:
            return CheckResult(
                "Ollama",
                CheckStatus.WARN,
                f"direct probe {host}:{port} failed ({direct_err}); "
                f"reachable via BASTION proxy on :{proxy_port} — "
                "likely nftables lockdown (normal)",
            )
    except Exception:  # noqa: BLE001
        pass
    return CheckResult(
        "Ollama",
        CheckStatus.FAIL,
        f"unreachable on {host}:{port} ({direct_err}) and not reachable via "
        f"BASTION proxy on :{proxy_port} -- is Ollama running?",
    )


async def check_models(
    port: int = 11435,
    host: str = "127.0.0.1",
    proxy_port: int | None = None,
) -> CheckResult:
    """Check installed Ollama models and VRAM compatibility."""
    urls = [f"http://{host}:{port}/api/tags"]
    if proxy_port is not None:
        urls.append(f"http://{host}:{proxy_port}/api/tags")
    for url in urls:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=5.0)
            if resp.status_code != 200:
                continue
            data = resp.json()
            models = data.get("models", [])
            if not models:
                return CheckResult(
                    "Installed models",
                    CheckStatus.WARN,
                    "no models installed -- run: ollama pull llama3.1:8b",
                )
            model_names = [m.get("name", "?") for m in models]
            return CheckResult(
                "Installed models",
                CheckStatus.PASS,
                f"{len(models)} model(s): {', '.join(model_names[:5])}"
                + (f" (+{len(models) - 5} more)" if len(models) > 5 else ""),
            )
        except Exception:  # noqa: BLE001
            continue
    return CheckResult("Installed models", CheckStatus.WARN, "could not query Ollama models")


async def check_port(port: int = 11434) -> CheckResult:
    """Check if BASTION's listen port is available."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
        sock.close()
        return CheckResult("Port", CheckStatus.PASS, f"{port}: available")
    except OSError:
        sock.close()
        # Check if it's BASTION already running
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/broker/status", timeout=2.0)
            if resp.status_code == 200:
                return CheckResult(
                    "Port",
                    CheckStatus.PASS,
                    f"{port}: BASTION already running",
                )
        except Exception:
            pass
        return CheckResult(
            "Port",
            CheckStatus.FAIL,
            f"{port}: in use by another process",
        )


def _find_config_path() -> Path | None:
    """Find config file using BASTION's search logic."""
    return _find_config(None)


def check_config() -> CheckResult:
    """Check if a valid config file exists and parses."""
    config_path = _find_config_path()
    if config_path is None:
        return CheckResult(
            "Config",
            CheckStatus.WARN,
            "no config file found -- run: bastion --init-config",
        )

    try:
        load_config(config_path)
        return CheckResult(
            "Config",
            CheckStatus.PASS,
            f"{config_path} valid",
        )
    except Exception as e:
        return CheckResult(
            "Config",
            CheckStatus.FAIL,
            f"{config_path} has errors: {e}",
        )


def check_permissions() -> CheckResult:
    """Check GPU device node permissions."""
    dev_nvidia = Path("/dev/nvidia0")
    if not dev_nvidia.exists():
        return CheckResult(
            "Permissions",
            CheckStatus.WARN,
            "/dev/nvidia0 not found -- GPU device nodes may not be created yet "
            "(run: sudo nvidia-modprobe)",
        )

    if dev_nvidia.stat().st_mode & 0o004:  # world-readable
        return CheckResult("Permissions", CheckStatus.PASS, "GPU device nodes accessible")

    # Check if current user can read it
    try:
        with open(dev_nvidia, "rb"):
            pass
        return CheckResult("Permissions", CheckStatus.PASS, "GPU device nodes accessible")
    except PermissionError:
        return CheckResult(
            "Permissions",
            CheckStatus.FAIL,
            "/dev/nvidia0 not readable -- add user to 'video' group: "
            "sudo usermod -aG video $USER",
        )


def _query_driver_version() -> str | None:
    """Query NVIDIA driver version from nvidia-smi."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")[0].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


async def run_all_checks(
    ollama_port: int = 11435,
    bastion_port: int = 11434,
    ollama_host: str = "127.0.0.1",
) -> list[CheckResult]:
    """Run all pre-flight checks in order.

    Returns
    -------
    list[CheckResult]
        Results for each check, in order.
    """
    results: list[CheckResult] = []

    # 1. Python version (sync)
    results.append(check_python_version())

    # 2. GPU detection (async)
    gpu_result = await check_gpu()
    results.append(gpu_result)

    # 3. GPU profile lookup
    gpu_name = _query_gpu_name()
    results.append(check_gpu_profile(gpu_name))

    # 4. Ollama reachable (async) — with proxy fallback
    results.append(
        await check_ollama(port=ollama_port, host=ollama_host, proxy_port=bastion_port)
    )

    # 5. Installed models (async) — with proxy fallback
    results.append(
        await check_models(port=ollama_port, host=ollama_host, proxy_port=bastion_port)
    )

    # 6. Port availability (async -- may do HTTP check)
    results.append(await check_port(port=bastion_port))

    # 7. Config validation (sync)
    results.append(check_config())

    # 8. Permissions (sync)
    results.append(check_permissions())

    return results


def format_results(results: list[CheckResult]) -> str:
    """Format check results for terminal output."""
    lines = ["", "BASTION Pre-flight Check", "=" * 24, ""]

    for r in results:
        tag = f"[{r.status.value}]"
        lines.append(f"{tag:6s} {r.name}: {r.message}")

    passed = sum(1 for r in results if r.status == CheckStatus.PASS)
    warned = sum(1 for r in results if r.status == CheckStatus.WARN)
    failed = sum(1 for r in results if r.status == CheckStatus.FAIL)

    lines.append("")
    lines.append(f"Result: {passed} passed, {warned} warning(s), {failed} failed")

    return "\n".join(lines)


def compute_exit_code(results: list[CheckResult]) -> int:
    """Compute exit code from results: 0 = all pass/warn, 1 = any fail."""
    if any(r.status == CheckStatus.FAIL for r in results):
        return 1
    return 0
