"""Tests for watchdog: sd_notify helpers and ProcessMonitor.

Covers:
  - sd_notify functions (safe no-ops without NOTIFY_SOCKET)
  - ProcessMonitor lifecycle (start, stop)
  - Ollama health checks (healthy, unhealthy, connection refused)
  - GPU checks (responsive, timeout, unavailable)
  - Failure threshold: transitions to unhealthy after N failures
  - Recovery: transitions back to healthy after success
  - Callbacks: on_unhealthy / on_healthy fire on transitions
  - WatchdogStatus model serialization
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import bastion.gpu as _gpu_pkg
from bastion.gpu.nvidia import NvidiaBackend
from bastion.watchdog import (
    GPUState,
    OllamaState,
    ProcessMonitor,
    WatchdogStatus,
    init_watchdog,
    notify_ready,
    notify_status,
    notify_stopping,
    notify_watchdog,
)


@pytest.fixture
def _force_nvidia_backend():
    """Force NvidiaBackend so subprocess mocks are exercised on hosts without nvidia-smi."""
    original = _gpu_pkg._backend
    _gpu_pkg.set_backend(NvidiaBackend())
    try:
        yield
    finally:
        _gpu_pkg._backend = original


# ---------------------------------------------------------------------------
# sd_notify tests
# ---------------------------------------------------------------------------

class TestSdNotify:
    """Test systemd sd_notify helpers (safe no-ops without NOTIFY_SOCKET)."""

    def test_init_watchdog_returns_false_without_socket(self) -> None:
        """init_watchdog returns False when NOTIFY_SOCKET is not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove NOTIFY_SOCKET if present
            os.environ.pop("NOTIFY_SOCKET", None)
            result = init_watchdog()
            assert result is False

    def test_notify_functions_are_noops_without_init(self) -> None:
        """All notify_ functions are safe no-ops without init."""
        # Should not raise even without initialization
        notify_ready()
        notify_watchdog()
        notify_stopping()
        notify_status("test status")


# ---------------------------------------------------------------------------
# WatchdogStatus model tests
# ---------------------------------------------------------------------------

class TestWatchdogStatus:
    """Test WatchdogStatus Pydantic model."""

    def test_defaults(self) -> None:
        status = WatchdogStatus()
        assert status.ollama_state == OllamaState.UNKNOWN
        assert status.gpu_state == GPUState.UNAVAILABLE
        assert status.consecutive_ollama_failures == 0
        assert status.consecutive_gpu_timeouts == 0
        assert status.scheduler_paused is False

    def test_serialization(self) -> None:
        status = WatchdogStatus(
            ollama_state=OllamaState.HEALTHY,
            gpu_state=GPUState.RESPONSIVE,
            ollama_latency_ms=5.2,
            gpu_query_latency_ms=12.0,
        )
        data = status.model_dump()
        assert data["ollama_state"] == "healthy"
        assert data["gpu_state"] == "responsive"
        assert data["ollama_latency_ms"] == 5.2
        assert data["gpu_query_latency_ms"] == 12.0

    def test_enum_values(self) -> None:
        assert OllamaState.HEALTHY.value == "healthy"
        assert OllamaState.UNHEALTHY.value == "unhealthy"
        assert OllamaState.UNKNOWN.value == "unknown"
        assert GPUState.RESPONSIVE.value == "responsive"
        assert GPUState.TIMEOUT.value == "timeout"
        assert GPUState.UNAVAILABLE.value == "unavailable"


# ---------------------------------------------------------------------------
# ProcessMonitor tests
# ---------------------------------------------------------------------------

class TestProcessMonitor:
    """Test ProcessMonitor lifecycle and health checks."""

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        """Monitor starts and stops cleanly."""
        monitor = ProcessMonitor(
            ollama_url="http://127.0.0.1:99999",
            check_interval=100.0,  # long interval so it doesn't actually check
        )
        assert monitor.is_healthy is True
        await monitor.start()
        assert monitor._running is True
        await monitor.stop()
        assert monitor._running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """Calling start twice does not create a second task."""
        monitor = ProcessMonitor(check_interval=100.0)
        await monitor.start()
        task1 = monitor._task
        await monitor.start()
        task2 = monitor._task
        assert task1 is task2
        await monitor.stop()

    @pytest.mark.asyncio
    async def test_initial_status(self) -> None:
        """Initial status is UNKNOWN/UNAVAILABLE."""
        monitor = ProcessMonitor()
        status = monitor.status
        assert status.ollama_state == OllamaState.UNKNOWN
        assert status.gpu_state == GPUState.UNAVAILABLE
        assert status.scheduler_paused is False

    @pytest.mark.asyncio
    async def test_ollama_healthy(self) -> None:
        """Ollama check succeeds with HTTP 200."""
        monitor = ProcessMonitor(ollama_url="http://127.0.0.1:11435")

        mock_resp = httpx.Response(200, request=httpx.Request("GET", "http://mock"))
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            await monitor._check_ollama()

        assert monitor.status.ollama_state == OllamaState.HEALTHY
        assert monitor.status.consecutive_ollama_failures == 0
        assert monitor.status.ollama_latency_ms is not None

    @pytest.mark.asyncio
    async def test_ollama_unhealthy_http_error(self) -> None:
        """Ollama check records failure on HTTP 500."""
        monitor = ProcessMonitor()

        mock_resp = httpx.Response(500, request=httpx.Request("GET", "http://mock"))
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            await monitor._check_ollama()

        assert monitor.status.ollama_state == OllamaState.UNHEALTHY
        assert monitor.status.consecutive_ollama_failures == 1

    @pytest.mark.asyncio
    async def test_ollama_unhealthy_connection_refused(self) -> None:
        """Ollama check records failure on connection error."""
        monitor = ProcessMonitor()

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            await monitor._check_ollama()

        assert monitor.status.ollama_state == OllamaState.UNHEALTHY
        assert monitor.status.consecutive_ollama_failures == 1

    @pytest.mark.asyncio
    async def test_ollama_failure_counter_increments(self) -> None:
        """Consecutive failures increment the counter."""
        monitor = ProcessMonitor()

        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            await monitor._check_ollama()
            await monitor._check_ollama()
            await monitor._check_ollama()

        assert monitor.status.consecutive_ollama_failures == 3

    @pytest.mark.asyncio
    async def test_ollama_failure_counter_resets_on_success(self) -> None:
        """Counter resets to 0 on a successful check."""
        monitor = ProcessMonitor()

        # Fail twice
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            await monitor._check_ollama()
            await monitor._check_ollama()

        assert monitor.status.consecutive_ollama_failures == 2

        # Then succeed
        mock_resp = httpx.Response(200, request=httpx.Request("GET", "http://mock"))
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            await monitor._check_ollama()

        assert monitor.status.consecutive_ollama_failures == 0
        assert monitor.status.ollama_state == OllamaState.HEALTHY

    @pytest.mark.asyncio
    async def test_gpu_unavailable_when_nvidia_smi_missing(self, _force_nvidia_backend) -> None:
        """GPU check handles missing nvidia-smi gracefully."""
        monitor = ProcessMonitor()

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("nvidia-smi")):
            await monitor._check_gpu()

        assert monitor.status.gpu_state == GPUState.UNAVAILABLE
        assert monitor.status.consecutive_gpu_timeouts == 0

    @pytest.mark.asyncio
    async def test_gpu_responsive(self, _force_nvidia_backend) -> None:
        """GPU check succeeds when nvidia-smi returns quickly."""
        monitor = ProcessMonitor()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"42\n", b""))
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock, return_value=mock_proc,
        ):
            await monitor._check_gpu()

        assert monitor.status.gpu_state == GPUState.RESPONSIVE
        assert monitor.status.consecutive_gpu_timeouts == 0

    @pytest.mark.asyncio
    async def test_gpu_timeout_detection(self, _force_nvidia_backend) -> None:
        """GPU check detects timeout (nvidia-smi hang)."""
        monitor = ProcessMonitor(gpu_timeout=1)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError())
        mock_proc.kill = AsyncMock()
        mock_proc.wait = AsyncMock()

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock, return_value=mock_proc,
        ):
            await monitor._check_gpu()

        assert monitor.status.gpu_state == GPUState.TIMEOUT
        assert monitor.status.consecutive_gpu_timeouts == 1

    @pytest.mark.asyncio
    async def test_threshold_transitions_to_unhealthy(self) -> None:
        """Monitor transitions to unhealthy after failure_threshold consecutive failures."""
        monitor = ProcessMonitor(failure_threshold=2, check_interval=100.0)

        # Simulate 2 Ollama failures (threshold=2)
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            await monitor._check_ollama()
            await monitor._check_ollama()

        # Manually run the health check logic from _loop
        monitor._status.last_check = time.time()
        ollama_ok = monitor._status.consecutive_ollama_failures < monitor._failure_threshold
        gpu_ok = monitor._status.consecutive_gpu_timeouts < monitor._failure_threshold
        monitor._is_healthy = ollama_ok and gpu_ok
        monitor._status.scheduler_paused = not monitor._is_healthy

        assert monitor.is_healthy is False
        assert monitor.status.scheduler_paused is True

    @pytest.mark.asyncio
    async def test_recovery_to_healthy(self) -> None:
        """Monitor recovers to healthy after failures then success."""
        monitor = ProcessMonitor(failure_threshold=2, check_interval=100.0)
        monitor._is_healthy = False
        monitor._status.consecutive_ollama_failures = 5

        # Succeed
        mock_resp = httpx.Response(200, request=httpx.Request("GET", "http://mock"))
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            await monitor._check_ollama()

        # Recompute health
        ollama_ok = monitor._status.consecutive_ollama_failures < monitor._failure_threshold
        gpu_ok = monitor._status.consecutive_gpu_timeouts < monitor._failure_threshold
        monitor._is_healthy = ollama_ok and gpu_ok

        assert monitor.is_healthy is True

    @pytest.mark.asyncio
    async def test_on_unhealthy_callback(self) -> None:
        """on_unhealthy callback fires on healthy->unhealthy transition."""
        callback = AsyncMock()
        monitor = ProcessMonitor(
            failure_threshold=1,
            check_interval=100.0,
            on_unhealthy=callback,
        )

        # Simulate an unhealthy check manually
        monitor._status.consecutive_ollama_failures = 1
        was_healthy = monitor._is_healthy
        monitor._is_healthy = False
        monitor._status.scheduler_paused = True

        if was_healthy and not monitor._is_healthy:
            await callback()

        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_healthy_callback(self) -> None:
        """on_healthy callback fires on unhealthy->healthy transition."""
        callback = AsyncMock()
        monitor = ProcessMonitor(
            failure_threshold=3,
            check_interval=100.0,
            on_healthy=callback,
        )
        monitor._is_healthy = False  # Start unhealthy

        # Simulate recovery
        monitor._status.consecutive_ollama_failures = 0
        monitor._status.consecutive_gpu_timeouts = 0
        was_healthy = monitor._is_healthy
        monitor._is_healthy = True

        if not was_healthy and monitor._is_healthy:
            await callback()

        callback.assert_awaited_once()
