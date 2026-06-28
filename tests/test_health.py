"""Tests for GPU health monitoring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bastion.gpu as _gpu_pkg
from bastion.gpu.nvidia import NvidiaBackend
from bastion.health import check_gpu_safe, get_vram_free_gb, query_gpu_status
from bastion.models import GPUConfig, GPUStatus


@pytest.fixture
def _force_nvidia_backend():
    """Force NvidiaBackend so subprocess mocks are exercised on hosts without nvidia-smi."""
    original = _gpu_pkg._backend
    _gpu_pkg.set_backend(NvidiaBackend())
    try:
        yield
    finally:
        _gpu_pkg._backend = original


class TestQueryGPUStatus:
    @pytest.mark.asyncio
    async def test_parses_nvidia_smi_output(self, _force_nvidia_backend):
        """Parse well-formed nvidia-smi CSV output."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"55, 8192, 24576, 32768, 185.50\n", b""
        )
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c == 55
        assert status.vram_used_mb == 8192
        assert status.vram_free_mb == 24576
        assert status.vram_total_mb == 32768
        assert status.power_draw_watts == 185.5

    @pytest.mark.asyncio
    async def test_handles_nvidia_smi_not_found(self, _force_nvidia_backend):
        """Graceful fallback when nvidia-smi is not installed."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            status = await query_gpu_status()

        assert status.temperature_c is None
        assert status.vram_used_mb is None

    @pytest.mark.asyncio
    async def test_handles_timeout(self, _force_nvidia_backend):
        """Graceful fallback when nvidia-smi hangs."""
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c is None

    @pytest.mark.asyncio
    async def test_handles_empty_output(self, _force_nvidia_backend):
        """Graceful fallback for empty nvidia-smi output."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c is None

    @pytest.mark.asyncio
    async def test_handles_nonzero_return_code(self, _force_nvidia_backend):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c is None


class TestCheckGPUSafe:
    @pytest.mark.asyncio
    async def test_safe_conditions(self):
        safe = GPUStatus(
            temperature_c=55, vram_used_mb=8000,
            vram_total_mb=32000, power_draw_watts=180.0,
        )
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=safe)):
            is_safe, reason = await check_gpu_safe(GPUConfig())
        assert is_safe is True
        assert reason == "OK"

    @pytest.mark.asyncio
    async def test_temperature_exceeded(self):
        hot = GPUStatus(temperature_c=90)
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=hot)):
            is_safe, reason = await check_gpu_safe(GPUConfig(max_temperature_c=82))
        assert is_safe is False
        assert "temperature" in reason.lower()

    @pytest.mark.asyncio
    async def test_power_exceeded(self):
        high_power = GPUStatus(power_draw_watts=500.0)
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=high_power)):
            is_safe, reason = await check_gpu_safe(GPUConfig(max_power_watts=450.0))
        assert is_safe is False
        assert "power" in reason.lower()

    @pytest.mark.asyncio
    async def test_vram_exceeded(self):
        full = GPUStatus(vram_used_mb=31000, vram_total_mb=32000)
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=full)):
            is_safe, reason = await check_gpu_safe(GPUConfig())
        assert is_safe is False
        assert "vram" in reason.lower()

    @pytest.mark.asyncio
    async def test_no_gpu_data_is_safe(self):
        """When nvidia-smi unavailable, assume safe (can't block everything)."""
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=GPUStatus())):
            is_safe, reason = await check_gpu_safe(GPUConfig())
        assert is_safe is True

    @pytest.mark.asyncio
    async def test_publishes_power_gauge_when_reading_present(self):
        """Power-draw gauge is published when a reading is available (F6)."""
        status = GPUStatus(
            temperature_c=55, vram_used_mb=8000,
            vram_total_mb=32000, power_draw_watts=180.0,
        )
        config = GPUConfig(max_power_watts=450.0)
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=status)), \
                patch("bastion.health.update_gpu_power_watts") as mock_power, \
                patch("bastion.health.update_gpu_power_cap_watts") as mock_cap:
            await check_gpu_safe(config)
        mock_power.assert_called_once_with(180.0)
        mock_cap.assert_called_once_with(450.0)

    @pytest.mark.asyncio
    async def test_no_power_gauge_when_reading_absent(self):
        """Non-NVIDIA / StubBackend (None power) emits no power-draw gauge."""
        status = GPUStatus(temperature_c=55, power_draw_watts=None)
        config = GPUConfig(max_power_watts=450.0)
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=status)), \
                patch("bastion.health.update_gpu_power_watts") as mock_power, \
                patch("bastion.health.update_gpu_power_cap_watts") as mock_cap:
            await check_gpu_safe(config)
        mock_power.assert_not_called()
        # The cap is configured statically, so it is still published.
        mock_cap.assert_called_once_with(450.0)


class TestGetVRAMFreeGB:
    @pytest.mark.asyncio
    async def test_returns_gb(self):
        status = GPUStatus(vram_free_mb=24576)
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=status)):
            free = await get_vram_free_gb()
        assert free == 24.0

    @pytest.mark.asyncio
    async def test_unavailable(self):
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=GPUStatus())):
            assert await get_vram_free_gb() is None
