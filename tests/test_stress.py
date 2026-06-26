"""Tests for bastion stress-test calibrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.stress import (
    StressConfig,
    baseline_phase,
    check_prerequisites,
    single_load_phase,
    swap_ramp_phase,
)


class TestStressConfig:
    """Test stress test configuration."""

    def test_default_config(self) -> None:
        config = StressConfig()
        assert config.bastion_url == "http://127.0.0.1:11434"
        assert config.thermal_cutoff_pct == 0.90
        assert config.max_inference_latency_s == 30.0

    def test_custom_bastion_url(self) -> None:
        config = StressConfig(bastion_url="http://localhost:9999")
        assert config.bastion_url == "http://localhost:9999"


class TestCheckPrerequisites:
    """Test pre-flight checks for stress test."""

    @pytest.mark.asyncio
    async def test_bastion_not_running(self) -> None:
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock,
                   side_effect=Exception("Connection refused")):
            ok, msg = await check_prerequisites(StressConfig())
        assert not ok
        assert "not running" in msg.lower() or "unreachable" in msg.lower()

    @pytest.mark.asyncio
    async def test_not_enough_models(self) -> None:
        status_resp = MagicMock()
        status_resp.status_code = 200
        status_resp.json.return_value = {"state": "running"}

        tags_resp = MagicMock()
        tags_resp.status_code = 200
        tags_resp.json.return_value = {"models": [{"name": "one:latest", "size": 1_000_000_000}]}

        async def mock_get(url: str, **kwargs):
            if "/broker/status" in url:
                return status_resp
            if "/api/tags" in url:
                return tags_resp
            return status_resp

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
            ok, msg = await check_prerequisites(StressConfig())
        assert not ok
        assert "2" in msg  # needs at least 2 models


class TestBaselinePhase:
    """Test Phase 1: Baseline measurement."""

    @pytest.mark.asyncio
    async def test_baseline_collects_samples(self) -> None:
        mock_status = MagicMock()
        mock_status.temperature_c = 42
        mock_status.power_draw_watts = 18.5
        mock_status.vram_used_mb = 512

        with patch("bastion.stress.query_gpu_status", new_callable=AsyncMock,
                   return_value=mock_status):
            result = await baseline_phase(duration_seconds=2, sample_interval=1.0)

        assert result.phase == "baseline"
        assert result.success
        assert result.data["idle_temp_c"] == 42
        assert result.data["idle_power_w"] == 18.5
        assert result.data["vram_in_use_mb"] == 512


class TestSingleLoadPhase:
    """Test Phase 2: Single model load/inference/unload."""

    @pytest.mark.asyncio
    async def test_single_load_measures_latency(self) -> None:
        generate_resp = MagicMock()
        generate_resp.status_code = 200
        generate_resp.json.return_value = {
            "response": "Hello!",
            "eval_count": 50,
            "eval_duration": 500_000_000,  # 500ms in ns
        }

        unload_resp = MagicMock()
        unload_resp.status_code = 200

        mock_gpu = MagicMock()
        mock_gpu.temperature_c = 45
        mock_gpu.vram_used_mb = 4096

        async def mock_post(url: str, **kwargs):
            if "/api/generate" in url:
                return generate_resp
            return unload_resp

        with (
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=mock_post),
            patch(
                "bastion.stress.query_gpu_status",
                new_callable=AsyncMock,
                return_value=mock_gpu,
            ),
        ):
            result = await single_load_phase(
                bastion_url="http://localhost:11434",
                model="test:latest",
                baseline_temp=42,
            )

        assert result.phase == "single_load"
        assert result.success
        assert "inference_latency_s" in result.data
        assert "thermal_delta_c" in result.data


class TestSwapRampPhase:
    """Test Phase 3: swap-velocity ramp + burst-depth calibration."""

    @pytest.mark.asyncio
    async def test_stable_path_reports_safe_burst_depth(self) -> None:
        """Completing all intervals emits safe_burst_depth in PhaseResult.data."""
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        cool_gpu = MagicMock()
        cool_gpu.temperature_c = 50  # well below cutoff

        with (
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=ok_resp),
            patch(
                "bastion.stress.query_gpu_status",
                new_callable=AsyncMock,
                return_value=cool_gpu,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await swap_ramp_phase(
                bastion_url="http://localhost:11434",
                models=["a:latest", "b:latest"],
                thermal_ceiling=100,
            )

        assert result.phase == "swap_ramp"
        assert result.success
        assert "safe_swap_rate_per_min" in result.data
        assert "safe_burst_depth" in result.data
        # All 5 intervals x 3 swaps tolerated with min-spacing respected.
        assert result.data["safe_burst_depth"] == 15
        assert result.data["stop_reason"] == "completed all intervals"

    @pytest.mark.asyncio
    async def test_instability_path_reports_last_known_safe_burst(self) -> None:
        """On first instability, report LAST-KNOWN-SAFE burst, not the unstable value."""
        ok_resp = MagicMock()
        ok_resp.status_code = 200

        cool_gpu = MagicMock()
        cool_gpu.temperature_c = 50   # below cutoff (90)
        hot_gpu = MagicMock()
        hot_gpu.temperature_c = 95    # above cutoff -> instability

        # First interval (3 swaps) stays cool -> last-known-safe burst = 3.
        # Next interval's first swap goes hot -> cutoff fires (consecutive would be 4).
        temps = [cool_gpu, cool_gpu, cool_gpu, hot_gpu]

        with (
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=ok_resp),
            patch(
                "bastion.stress.query_gpu_status",
                new_callable=AsyncMock,
                side_effect=temps,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await swap_ramp_phase(
                bastion_url="http://localhost:11434",
                models=["a:latest", "b:latest"],
                thermal_ceiling=100,
            )

        assert result.phase == "swap_ramp"
        assert result.success
        assert "safe_burst_depth" in result.data
        # Last-known-safe is 3 (the completed cool interval), NOT 4 (the unstable swap).
        assert result.data["safe_burst_depth"] == 3
        assert result.data["safe_burst_depth"] != 4
        assert "thermal cutoff" in result.data["stop_reason"]
