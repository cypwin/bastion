"""Watchdog: Ollama process monitor + systemd sd_notify integration.

Two responsibilities:
  1. **Systemd watchdog** — sends sd_notify heartbeats (READY, WATCHDOG,
     STOPPING, STATUS) via the NOTIFY_SOCKET Unix datagram socket.
  2. **Process monitor** — periodically checks Ollama health (HTTP ping)
     and GPU responsiveness (nvidia-smi timeout detection).  Integrates
     with the scheduler: when Ollama is unreachable or GPU is locked up
     the monitor sets ``is_healthy=False`` so the scheduler can pause
     scheduling.

All sd_notify functions are safe no-ops when not running under systemd.

Reference: https://www.freedesktop.org/software/systemd/man/sd_notify.html
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Systemd sd_notify (unchanged from original)
# ---------------------------------------------------------------------------

_socket: socket.socket | None = None
_notify_addr: str | None = None


def init_watchdog() -> bool:
    """Initialize the sd_notify socket.

    Returns True if NOTIFY_SOCKET is set and the socket was created,
    False otherwise (not running under systemd, or no watchdog configured).
    """
    global _socket, _notify_addr

    notify_path = os.environ.get("NOTIFY_SOCKET")
    if not notify_path:
        logger.debug("NOTIFY_SOCKET not set — watchdog disabled")
        return False

    try:
        _socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        # Abstract socket (starts with @) — replace @ with null byte
        _notify_addr = "\x00" + notify_path[1:] if notify_path.startswith("@") else notify_path
        logger.info("Watchdog initialized (NOTIFY_SOCKET=%s)", notify_path)
        return True
    except Exception as e:
        logger.warning("Failed to create watchdog socket: %s", e)
        _socket = None
        return False


def notify_ready() -> None:
    """Send READY=1 to systemd (service is fully started)."""
    _send("READY=1")


def notify_watchdog() -> None:
    """Send WATCHDOG=1 heartbeat to systemd."""
    _send("WATCHDOG=1")


def notify_stopping() -> None:
    """Send STOPPING=1 to systemd (service is shutting down)."""
    _send("STOPPING=1")


def notify_status(status: str) -> None:
    """Send STATUS=<text> to systemd (human-readable status line)."""
    _send(f"STATUS={status}")


def _send(message: str) -> None:
    """Send a message to the NOTIFY_SOCKET."""
    if _socket is None or _notify_addr is None:
        return
    try:
        _socket.sendto(message.encode(), _notify_addr)
    except Exception as e:
        logger.debug("Failed to send sd_notify '%s': %s", message, e)


# ---------------------------------------------------------------------------
# Process monitor types
# ---------------------------------------------------------------------------


class OllamaState(StrEnum):
    """Ollama backend state as seen by the watchdog."""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class GPUState(StrEnum):
    """GPU state as seen by the watchdog."""
    RESPONSIVE = "responsive"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class WatchdogStatus(BaseModel):
    """Snapshot of watchdog checks — returned by /broker/watchdog."""
    ollama_state: OllamaState = OllamaState.UNKNOWN
    gpu_state: GPUState = GPUState.UNAVAILABLE
    ollama_latency_ms: float | None = None
    gpu_query_latency_ms: float | None = None
    last_check: float = Field(default_factory=time.time)
    consecutive_ollama_failures: int = 0
    consecutive_gpu_timeouts: int = 0
    scheduler_paused: bool = False


# ---------------------------------------------------------------------------
# ProcessMonitor — the async background task
# ---------------------------------------------------------------------------


class ProcessMonitor:
    """Async background task that checks Ollama health and GPU responsiveness.

    Parameters
    ----------
    ollama_url : str
        Base URL of the Ollama backend (e.g. ``http://127.0.0.1:11435``).
    check_interval : float
        Seconds between health checks.
    ollama_timeout : float
        HTTP timeout for the Ollama health ping.
    gpu_timeout : int
        Subprocess timeout for nvidia-smi, in seconds.
    failure_threshold : int
        Number of consecutive failures before declaring unhealthy.
    on_unhealthy : callable, optional
        Async callback invoked when the monitor transitions to unhealthy.
    on_healthy : callable, optional
        Async callback invoked when the monitor transitions back to healthy.
    """

    def __init__(
        self,
        ollama_url: str = "http://127.0.0.1:11435",
        check_interval: float = 10.0,
        ollama_timeout: float = 5.0,
        gpu_timeout: int = 5,
        failure_threshold: int = 3,
        on_unhealthy: Any | None = None,
        on_healthy: Any | None = None,
    ) -> None:
        self._ollama_url = ollama_url
        self._check_interval = check_interval
        self._ollama_timeout = ollama_timeout
        self._gpu_timeout = gpu_timeout
        self._failure_threshold = failure_threshold
        self._on_unhealthy = on_unhealthy
        self._on_healthy = on_healthy

        self._status = WatchdogStatus()
        self._running = False
        self._task: asyncio.Task | None = None
        self._is_healthy = True

    @property
    def is_healthy(self) -> bool:
        """True when both Ollama and GPU are considered operational."""
        return self._is_healthy

    @property
    def status(self) -> WatchdogStatus:
        """Return latest watchdog status snapshot."""
        return self._status

    async def start(self) -> None:
        """Start the background health-check loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="bastion-watchdog")
        logger.info(
            "Process monitor started (interval=%.1fs, threshold=%d)",
            self._check_interval,
            self._failure_threshold,
        )

    async def stop(self) -> None:
        """Stop the background health-check loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """Main loop: check Ollama + GPU, update status, fire callbacks."""
        while self._running:
            try:
                await self._check_ollama()
                await self._check_gpu()
                self._status.last_check = time.time()

                # Determine overall health
                was_healthy = self._is_healthy
                ollama_ok = (
                    self._status.consecutive_ollama_failures < self._failure_threshold
                )
                gpu_ok = (
                    self._status.consecutive_gpu_timeouts < self._failure_threshold
                )
                self._is_healthy = ollama_ok and gpu_ok
                self._status.scheduler_paused = not self._is_healthy

                # Fire callbacks on transitions
                if was_healthy and not self._is_healthy:
                    logger.warning(
                        "Watchdog: system UNHEALTHY (ollama_failures=%d, gpu_timeouts=%d)",
                        self._status.consecutive_ollama_failures,
                        self._status.consecutive_gpu_timeouts,
                    )
                    if self._on_unhealthy:
                        await self._on_unhealthy()
                elif not was_healthy and self._is_healthy:
                    logger.info("Watchdog: system recovered to HEALTHY")
                    if self._on_healthy:
                        await self._on_healthy()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Watchdog loop error: %s", e, exc_info=True)

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    async def _check_ollama(self) -> None:
        """Ping Ollama's root endpoint to verify it's responsive."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._ollama_timeout) as client:
                resp = await client.get(f"{self._ollama_url}/")
                elapsed_ms = (time.monotonic() - start) * 1000
                self._status.ollama_latency_ms = round(elapsed_ms, 1)
                if resp.status_code == 200:
                    self._status.ollama_state = OllamaState.HEALTHY
                    self._status.consecutive_ollama_failures = 0
                else:
                    self._status.ollama_state = OllamaState.UNHEALTHY
                    self._status.consecutive_ollama_failures += 1
                    logger.warning(
                        "Ollama health check: HTTP %d (%.0fms)",
                        resp.status_code, elapsed_ms,
                    )
        except Exception as e:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._status.ollama_latency_ms = round(elapsed_ms, 1)
            self._status.ollama_state = OllamaState.UNHEALTHY
            self._status.consecutive_ollama_failures += 1
            logger.warning("Ollama health check failed (%.0fms): %s", elapsed_ms, e)

    async def _check_gpu(self) -> None:
        """Check GPU responsiveness via the GPU backend.

        A lockup (query hangs > gpu_timeout seconds) is a strong signal
        the GPU driver is wedged, which often precedes or accompanies a
        GPU crash.
        """
        from bastion.gpu import get_backend
        from bastion.gpu.nvidia import NvidiaBackend

        backend = get_backend()
        start = time.monotonic()

        if isinstance(backend, NvidiaBackend):
            result = await backend.check_gpu_responsive(self._gpu_timeout)
            elapsed_ms = (time.monotonic() - start) * 1000
            self._status.gpu_query_latency_ms = round(elapsed_ms, 1)

            if result is True:
                self._status.gpu_state = GPUState.RESPONSIVE
                self._status.consecutive_gpu_timeouts = 0
            elif result is False:
                self._status.gpu_state = GPUState.TIMEOUT
                self._status.consecutive_gpu_timeouts += 1
                logger.warning(
                    "GPU query timed out after %ds \u2014 possible GPU lockup",
                    self._gpu_timeout,
                )
            else:
                self._status.gpu_state = GPUState.UNAVAILABLE
                self._status.gpu_query_latency_ms = None
        else:
            # Stub or other backend — no GPU monitoring
            self._status.gpu_state = GPUState.UNAVAILABLE
            self._status.gpu_query_latency_ms = None
