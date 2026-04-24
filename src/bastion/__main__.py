"""CLI entry point for BASTION.

Usage:
    python -m bastion                    # Start with default config
    python -m bastion --config my.yaml   # Start with custom config
    python -m bastion --port 11434       # Override listen port
    python -m bastion --admin-port 9999  # Two-port mode (admin on 9999)
    bastion                              # If installed via pip
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import uvicorn


logger = logging.getLogger(__name__)


def _security_banner_lines(config: "BrokerConfig") -> list[str]:
    """Return warning lines for insecure server configuration.

    Returns an empty list when bind is localhost-only or when auth is
    properly configured. Otherwise returns a SECURITY WARNING banner.

    Checks performed (all gated on public bind):
    1. Auth disabled or no API keys configured.
    2. A2A enabled with no tokens — task endpoints are open.
    3. Rate limiting disabled — GPU saturation risk (applies to all proxy traffic).
    """
    warnings: list[str] = []
    host = config.server.host
    auth_on = config.auth.enabled
    keys_configured = bool(config.auth.api_keys)

    # Compute once; reused by all three checks below.
    public_bind = host in ("0.0.0.0", "::") or (
        host
        and not host.startswith("127.")
        and host != "localhost"
    )

    # Check 1: auth disabled or no API keys on public bind.
    if public_bind and (not auth_on or not keys_configured):
        warnings.append("=" * 72)
        warnings.append("*** SECURITY WARNING ***")
        if not auth_on:
            warnings.append(
                f"  Listening on {host}:{config.server.port} with auth is disabled."
            )
        else:
            warnings.append(
                f"  Listening on {host}:{config.server.port} with auth.enabled=true "
                "but no api_keys configured — proxy is OPEN."
            )
        warnings.append(
            "  Anyone who can reach this port can run inference against your GPU."
        )
        warnings.append(
            "  To secure: set auth.enabled: true + auth.api_keys: [\"<key>\"] in broker.yaml,"
        )
        warnings.append(
            "  OR restrict server.host to 127.0.0.1 for localhost-only."
        )
        warnings.append("=" * 72)

    # Check 2: A2A enabled but no tokens configured — task endpoints are open.
    if public_bind and config.a2a.enabled and not config.a2a.tokens:
        warnings.append("-" * 72)
        warnings.append("A2A endpoints (/a2a/*) are OPEN — no a2a.tokens configured.")
        warnings.append(
            "  Any caller can create, cancel, and stream A2A tasks. "
            "Set a2a.tokens in broker.yaml to restrict."
        )
        warnings.append("-" * 72)

    # Check 3: Rate limiting disabled on a public bind — GPU saturation risk.
    if public_bind and not config.rate_limit.enabled:
        warnings.append("-" * 72)
        warnings.append("Rate limiting is DISABLED on a public bind.")
        warnings.append(
            "  A single client can saturate your GPU. "
            "Set rate_limit.enabled: true in broker.yaml to throttle per-IP."
        )
        warnings.append("-" * 72)

    return warnings


def _generate_config() -> None:
    """Generate a starter config with auto-detected GPU values."""
    import shutil

    from bastion.paths import config_dir

    # Locate the example config bundled with the source
    example = Path(__file__).resolve().parent.parent.parent / "config" / "broker.example.yaml"

    dest = config_dir() / "broker.yaml"
    if dest.exists():
        print(f"Config already exists: {dest}")
        print("Remove it first if you want to regenerate.")
        return

    if example.exists():
        shutil.copy2(example, dest)
        print(f"Config written to {dest}")
    else:
        # Minimal inline config when example file isn't available (pip install)
        dest.write_text(
            "# BASTION configuration\n"
            "# See https://github.com/cyprian-w/bastion for full reference.\n"
            "\n"
            "ollama:\n"
            "  host: \"127.0.0.1\"\n"
            "  port: 11435\n"
            "\n"
            "server:\n"
            "  host: \"0.0.0.0\"\n"
            "  port: 11434\n"
            "\n"
            "gpu:\n"
            "  total_vram_gb: 0    # 0 = auto-detect from nvidia-smi\n"
            "  headroom_gb: 6\n"
            "  max_temperature_c: 83\n"
            "\n"
            "scheduler:\n"
            "  cooldown_seconds: 2.0\n"
            "  max_queue_size: 512\n"
            "\n"
            "models: {}\n",
            encoding="utf-8",
        )
        print(f"Config written to {dest}")

    print("Edit to customize, then start BASTION with: bastion")
    print("Run `bastion --detect-models` to discover installed Ollama models.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bastion",
        description="BASTION — Batch Affinity Scheduler for Throttled Inference on Ollama Networks",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        default=None,
        help="Path to broker.yaml config file (default: config/broker.yaml)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Listen address (default: from config or 0.0.0.0)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=None,
        help="Listen port (default: from config or 11434)",
    )
    parser.add_argument(
        "--admin-port",
        type=int,
        default=None,
        help="Admin+A2A port (default: from config or disabled). "
             "Enables two-port mode when set to a port different from --port.",
    )
    parser.add_argument(
        "--ollama-port",
        type=int,
        default=None,
        help="Ollama backend port (default: from config or 11435)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Generate a starter config file at ~/.config/bastion/broker.yaml "
             "with auto-detected GPU values, then exit.",
    )
    parser.add_argument(
        "--detect-models",
        action="store_true",
        help="Discover installed Ollama models and print a YAML models "
             "section to paste into broker.yaml, then exit.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run pre-flight checks to verify your system is ready for BASTION, "
             "then exit.",
    )
    parser.add_argument(
        "--stress-test",
        action="store_true",
        help="Run GPU stress calibrator to discover safe operating limits. "
             "Requires BASTION to be running. Writes results to "
             "~/.config/bastion/gpu-profile.yaml.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.init_config:
        _generate_config()
        return

    if args.detect_models:
        from bastion.discovery import detect_models

        ollama_port = args.ollama_port or 11435
        detect_models(ollama_port=ollama_port)
        return

    if args.validate:
        from bastion.validate import (
            compute_exit_code,
            format_results,
            run_all_checks,
        )

        ollama_port = args.ollama_port or 11435
        bastion_port = args.port or 11434
        results = asyncio.run(run_all_checks(
            ollama_port=ollama_port,
            bastion_port=bastion_port,
        ))
        print(format_results(results))
        sys.exit(compute_exit_code(results))

    if args.stress_test:
        from bastion.stress import (
            SAFETY_BANNER,
            StressConfig,
            recovery_phase,
        )

        stress_config = StressConfig(
            bastion_url=f"http://127.0.0.1:{args.port or 11434}",
        )

        # Safety ceremony
        print(SAFETY_BANNER)
        try:
            response = input().strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

        if response.lower() != "i understand":
            print("Aborted -- you must type 'I understand' to continue.")
            sys.exit(0)

        try:
            asyncio.run(_run_stress_test(stress_config))
        except KeyboardInterrupt:
            print("\n\nCtrl+C -- running recovery phase...")
            asyncio.run(
                recovery_phase(
                    stress_config.bastion_url,
                    baseline_temp=40,  # conservative fallback
                )
            )
            print("Recovery complete. Exiting.")
        sys.exit(0)

    # Lazy import to allow config override before app creation
    from bastion.config import load_config
    from bastion.server import create_app

    config = load_config(args.config)

    # CLI overrides take precedence over config file
    host = args.host or config.server.host
    port = args.port or config.server.port
    if args.admin_port is not None:
        config.server.admin_port = args.admin_port
    if args.ollama_port:
        config.ollama.port = args.ollama_port

    bastion_logger = logging.getLogger("bastion")

    for line in _security_banner_lines(config):
        logger.warning(line)

    if config.server.two_port_mode:
        # Two-port mode: proxy on port, admin on admin_port
        admin_port = config.server.admin_port
        bastion_logger.info(
            "Starting BASTION in two-port mode: "
            "proxy %s:%d, admin %s:%d → Ollama at %s:%d",
            host, port, host, admin_port,
            config.ollama.host, config.ollama.port,
        )
        asyncio.run(_run_two_port(config, host, port, admin_port, args.log_level.lower()))
    else:
        # Single-port mode (backward compatible)
        app = create_app(config)
        bastion_logger.info(
            "Starting BASTION on %s:%d → Ollama at %s:%d",
            host, port, config.ollama.host, config.ollama.port,
        )
        uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())


async def _run_two_port(
    config: BrokerConfig,  # noqa: F821 — lazy import avoids circular
    host: str,
    proxy_port: int,
    admin_port: int,
    log_level: str,
) -> None:
    """Run proxy and admin servers concurrently using asyncio.

    Both servers share the same module-level state (scheduler, queue,
    VRAM tracker) via the proxy app's lifespan. They start together
    and shut down together via asyncio.gather.

    Signal handling: SIGTERM and SIGINT trigger graceful shutdown of
    both servers. The lifespan shutdown handler in server.py drains the
    scheduler queue, waits for in-flight requests, and closes httpx clients.

    Parameters
    ----------
    config : BrokerConfig
        Validated broker configuration.
    host : str
        Bind address for both servers.
    proxy_port : int
        Port for the Ollama-compatible proxy.
    admin_port : int
        Port for admin + A2A endpoints.
    log_level : str
        Uvicorn log level (e.g. "info", "debug").
    """
    from bastion.server import create_admin_app, create_proxy_app
    from bastion.watchdog import notify_stopping

    proxy_app = create_proxy_app(config)
    admin_app = create_admin_app(config)

    proxy_config = uvicorn.Config(
        proxy_app, host=host, port=proxy_port, log_level=log_level,
    )
    admin_config = uvicorn.Config(
        admin_app, host=host, port=admin_port, log_level=log_level,
    )

    proxy_server = uvicorn.Server(proxy_config)
    admin_server = uvicorn.Server(admin_config)

    logger = logging.getLogger("bastion")

    # Register signal handlers for graceful shutdown of both servers
    loop = asyncio.get_running_loop()

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("Received %s — initiating graceful shutdown", sig.name)
        notify_stopping()
        proxy_server.should_exit = True
        admin_server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal, sig)

    await asyncio.gather(
        proxy_server.serve(),
        admin_server.serve(),
    )


async def _run_stress_test(config: StressConfig) -> None:  # noqa: F821
    """Run the full stress test sequence with phase-by-phase confirmation."""
    from bastion.stress import (
        CalibrationResult,
        baseline_phase,
        check_prerequisites,
        concurrent_load_phase,
        recovery_phase,
        single_load_phase,
        swap_ramp_phase,
        write_profile,
    )
    from bastion.gpu_profiles import lookup_profile
    from bastion.validate import _query_driver_version, _query_gpu_name

    # Prerequisites
    print("\nChecking prerequisites...")
    ok, msg = await check_prerequisites(config)
    if not ok:
        print(f"\n  FAILED: {msg}")
        return
    print(f"  {msg}")

    # Get GPU info
    gpu_name = _query_gpu_name() or "Unknown GPU"
    driver = _query_driver_version() or "unknown"
    profile = lookup_profile(gpu_name)

    from bastion.health import query_gpu_status
    status = await query_gpu_status()
    vram_total = status.vram_total_mb or profile.vram_total_mb

    result = CalibrationResult(
        gpu_name=gpu_name,
        vram_total_mb=vram_total,
        driver=driver,
    )

    # Get small models for testing
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{config.bastion_url}/api/tags", timeout=5.0)
    models_data = resp.json().get("models", [])
    small_models = sorted(
        [m["name"] for m in models_data if m.get("size", 0) < 5 * 1024**3],
        key=lambda n: next((m["size"] for m in models_data if m["name"] == n), 0),
    )[:2]

    # Phase 1: Baseline
    print(f"\n--- Phase 1: Baseline ({config.baseline_duration_s:.0f}s) ---")
    phase1 = await baseline_phase(config.baseline_duration_s, config.sample_interval_s)
    result.phases.append(phase1)

    if not phase1.success:
        print(f"  FAILED: {phase1.error}")
        await recovery_phase(config.bastion_url, 40)
        return

    print(f"  Idle temp: {phase1.data['idle_temp_c']}C")
    print(f"  Idle power: {phase1.data['idle_power_w']}W")
    print(f"  VRAM in use: {phase1.data['vram_in_use_mb']} MB")
    if not _confirm_continue():
        await recovery_phase(config.bastion_url, phase1.data["idle_temp_c"])
        return

    baseline_temp = phase1.data["idle_temp_c"]

    # Phase 2: Single load
    print(f"\n--- Phase 2: Single Load ({small_models[0]}) ---")
    phase2 = await single_load_phase(config.bastion_url, small_models[0], baseline_temp)
    result.phases.append(phase2)

    if not phase2.success:
        print(f"  FAILED: {phase2.error}")
        await recovery_phase(config.bastion_url, baseline_temp)
        return

    print(f"  Inference latency: {phase2.data['inference_latency_s']}s")
    print(f"  Thermal delta: +{phase2.data['thermal_delta_c']}C")
    print(f"  Peak VRAM: {phase2.data['peak_vram_mb']} MB")
    if not _confirm_continue():
        await recovery_phase(config.bastion_url, baseline_temp)
        return

    # Phase 3: Swap ramp
    print("\n--- Phase 3: Swap Ramp ---")
    phase3 = await swap_ramp_phase(
        config.bastion_url, small_models, profile.thermal_ceiling_c,
    )
    result.phases.append(phase3)
    print(f"  Safe swap rate: {phase3.data['safe_swap_rate_per_min']}/min")
    print(f"  Stop reason: {phase3.data['stop_reason']}")
    print(f"  Avg swap duration: {phase3.data['swap_duration_avg_s']}s")
    if not _confirm_continue():
        await recovery_phase(config.bastion_url, baseline_temp)
        return

    # Phase 4: Concurrent load
    print(f"\n--- Phase 4: Concurrent Load ({small_models[0]}) ---")
    phase4 = await concurrent_load_phase(config.bastion_url, small_models[0])
    result.phases.append(phase4)
    print(f"  Max concurrent: {phase4.data['max_concurrent_requests']}")
    print(f"  Stop reason: {phase4.data['stop_reason']}")

    # Phase 5: Recovery
    print("\n--- Phase 5: Recovery ---")
    phase5 = await recovery_phase(config.bastion_url, baseline_temp)
    result.phases.append(phase5)
    print(f"  Cooldown: {phase5.data['cooldown_duration_s']}s")
    print(f"  Final temp: {phase5.data.get('final_temp_c', '?')}C")

    # Aggregate calibrated values
    result.calibrated = {
        "safe_swap_rate_per_min": phase3.data.get("safe_swap_rate_per_min", 3),
        "max_concurrent_requests": phase4.data.get("max_concurrent_requests", 2),
        "vram_headroom_mb": profile.vram_headroom_mb,
        "thermal_ceiling_c": profile.thermal_ceiling_c,
        "cooldown_seconds": max(2, int(phase5.data.get("cooldown_duration_s", 3) / 2)),
        "swap_duration_avg_s": phase3.data.get("swap_duration_avg_s", 0),
        "models_used": small_models,
    }

    # Write profile
    dest = write_profile(result)
    print(f"\n  Profile written to {dest}")
    print("  BASTION will use these calibrated values on next startup.")


def _confirm_continue() -> bool:
    """Ask user to continue to next phase. Returns False on abort."""
    try:
        response = input("\n  Continue to next phase? [Y/n] ").strip().lower()
        return response in ("", "y", "yes")
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborting -- running recovery...")
        return False


if __name__ == "__main__":
    main()
