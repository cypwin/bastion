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
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Lazy import to allow config override before app creation
    from bastion.config import load_config
    from bastion.server import create_admin_app, create_app, create_proxy_app

    config = load_config(args.config)

    # CLI overrides take precedence over config file
    host = args.host or config.server.host
    port = args.port or config.server.port
    if args.admin_port is not None:
        config.server.admin_port = args.admin_port
    if args.ollama_port:
        config.ollama.port = args.ollama_port

    bastion_logger = logging.getLogger("bastion")

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
    config: "BrokerConfig",  # noqa: F821 — lazy import avoids circular
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


if __name__ == "__main__":
    main()
