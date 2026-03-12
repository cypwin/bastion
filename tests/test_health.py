"""Tests for GPU health monitoring."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from bastion.health import query_gpu_status, check_gpu_safe, get_vram_free_gb
from bastion.models import GPUConfig, GPUStatus


class TestQueryGPUStatus:
    @pytest.mark.asyncio
    async def test_parses_nvidia_smi_output(self):
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
    async def test_handles_nvidia_smi_not_found(self):
        """Graceful fallback when nvidia-smi is not installed."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            status = await query_gpu_status()

        assert status.temperature_c is None
        assert status.vram_used_mb is None

    @pytest.mark.asyncio
    async def test_handles_timeout(self):
        """Graceful fallback when nvidia-smi hangs."""
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = asyncio.TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c is None

    @pytest.mark.asyncio
    async def test_handles_empty_output(self):
        """Graceful fallback for empty nvidia-smi output."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c is None

    @pytest.mark.asyncio
    async def test_handles_nonzero_return_code(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()

        assert status.temperature_c is None


class TestCheckGPUSafe:
    @pytest.mark.asyncio
    async def test_safe_conditions(self):
        safe = GPUStatus(temperature_c=55, vram_used_mb=8000, vram_total_mb=32000, power_draw_watts=180.0)
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
