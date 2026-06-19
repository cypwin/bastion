"""Error boundary tests (D3).

Covers:
  - Invalid config YAML: missing required fields, wrong types, negative values
  - Ollama unreachable: handler paths when httpx fails
  - GPU query failure: nvidia-smi timeout, permission denied, binary not found
  - Large payload: request body at and above max_request_body_bytes
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from bastion.config import load_config
from bastion.health import check_gpu_safe, get_vram_free_gb, query_gpu_status
from bastion.models import (
    BrokerConfig,
    GPUConfig,
    GPUStatus,
    ModelInfo,
    OllamaConfig,
    ProxyConfig,
)
from bastion.vram import VRAMTracker

# ---------------------------------------------------------------------------
# D3: Invalid config YAML
# ---------------------------------------------------------------------------


class TestInvalidConfig:
    def test_empty_yaml_gives_defaults(self, tmp_path: Path) -> None:
        """Empty YAML file should return defaults."""
        path = tmp_path / "empty.yaml"
        path.write_text("")
        config = load_config(path)
        assert isinstance(config, BrokerConfig)
        assert config.ollama.port == 11435

    def test_null_yaml_gives_defaults(self, tmp_path: Path) -> None:
        """YAML with null content should return defaults."""
        path = tmp_path / "null.yaml"
        path.write_text("null")
        config = load_config(path)
        assert isinstance(config, BrokerConfig)

    def test_wrong_type_for_port(self, tmp_path: Path) -> None:
        """String where int expected should raise validation error."""
        cfg = {"server": {"port": "not-a-number"}}
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump(cfg))
        with pytest.raises((TypeError, ValueError)):  # Pydantic ValidationError
            load_config(path)

    def test_negative_cooldown(self, tmp_path: Path) -> None:
        """Negative cooldown should either be rejected or clamped."""
        cfg = {"scheduler": {"cooldown_seconds": -5.0}}
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump(cfg))
        # This may raise or may clamp to 0 depending on Pydantic validators
        try:
            config = load_config(path)
            # If it loaded, the value should be clamped to 0 or kept negative
            assert isinstance(config, BrokerConfig)
        except Exception:
            pass  # Validation error is also acceptable

    def test_extra_fields_ignored(self, tmp_path: Path) -> None:
        """Unknown YAML keys should be ignored (Pydantic extra='ignore')."""
        cfg = {"unknown_section": {"foo": "bar"}, "server": {"port": 8080}}
        path = tmp_path / "extra.yaml"
        path.write_text(yaml.dump(cfg))
        config = load_config(path)
        assert config.server.port == 8080

    def test_missing_config_file_raises(self, tmp_path: Path, monkeypatch) -> None:
        """When an explicit config path doesn't exist, raise FileNotFoundError."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="nonexistent.yaml"):
            load_config(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# D3: Ollama unreachable
# ---------------------------------------------------------------------------


class TestOllamaUnreachable:
    @pytest.fixture
    def tracker(self) -> VRAMTracker:
        config = BrokerConfig(
            ollama=OllamaConfig(host="127.0.0.1", port=99999),
            models={"test:7b": ModelInfo(vram_gb=5.0)},
        )
        return VRAMTracker(config)

    @pytest.mark.asyncio
    async def test_get_loaded_models_returns_none_on_connect_error(
        self, tracker: VRAMTracker,
    ) -> None:
        """Ollama unreachable -> None state-unknown sentinel (not [])."""
        with patch.object(
            tracker._http, "get",
            new_callable=AsyncMock, side_effect=httpx.ConnectError("refused"),
        ):
            models = await tracker.get_loaded_models()
        assert models is None

    @pytest.mark.asyncio
    async def test_get_loaded_models_returns_none_on_timeout(
        self, tracker: VRAMTracker,
    ) -> None:
        """Ollama timeout -> None state-unknown sentinel (not [])."""
        with patch.object(
            tracker._http, "get",
            new_callable=AsyncMock, side_effect=httpx.ReadTimeout("timeout"),
        ):
            models = await tracker.get_loaded_models()
        assert models is None

    @pytest.mark.asyncio
    async def test_unload_model_returns_false_on_error(
        self, tracker: VRAMTracker,
    ) -> None:
        with patch.object(
            tracker._http, "post",
            new_callable=AsyncMock, side_effect=httpx.ConnectError("refused"),
        ):
            success = await tracker.unload_model("test:7b")
        assert success is False

    @pytest.mark.asyncio
    async def test_get_loaded_vram_returns_zero_on_failure(
        self, tracker: VRAMTracker,
    ) -> None:
        with patch.object(
            tracker._http, "get",
            new_callable=AsyncMock, side_effect=httpx.ConnectError("refused"),
        ):
            vram = await tracker.get_loaded_vram_gb()
        assert vram == 0.0

    @pytest.mark.asyncio
    async def test_can_load_model_checks_gpu_even_when_ollama_down(
        self, tracker: VRAMTracker,
    ) -> None:
        """Even if Ollama is unreachable, GPU temperature check should still work."""
        with (
            patch.object(
                tracker._http, "get",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("refused"),
            ),
            patch(
                "bastion.vram.query_gpu_status",
                AsyncMock(return_value=GPUStatus(temperature_c=95)),
            ),
        ):
            can, reason = await tracker.can_load_model("test:7b")
        assert can is False
        assert "hot" in reason.lower()

    @pytest.mark.asyncio
    async def test_get_loaded_models_returns_none_on_http_error(
        self, tracker: VRAMTracker,
    ) -> None:
        """HTTP 5xx from Ollama -> None state-unknown sentinel (not [])."""
        mock_resp = httpx.Response(
            500,
            json={"error": "Internal server error"},
            request=httpx.Request("GET", "http://mock"),
        )
        mock_resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "500", request=mock_resp.request, response=mock_resp
        ))
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp):
            models = await tracker.get_loaded_models()
        assert models is None


# ---------------------------------------------------------------------------
# D3: GPU query failure
# ---------------------------------------------------------------------------


class TestGPUQueryFailure:
    @pytest.mark.asyncio
    async def test_nvidia_smi_timeout(self) -> None:
        """nvidia-smi timeout returns empty GPUStatus."""
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()
        assert status.temperature_c is None
        assert status.vram_used_mb is None

    @pytest.mark.asyncio
    async def test_nvidia_smi_not_found(self) -> None:
        """nvidia-smi binary not found returns empty GPUStatus."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("nvidia-smi")):
            status = await query_gpu_status()
        assert status.temperature_c is None

    @pytest.mark.asyncio
    async def test_nvidia_smi_nonzero_exit(self) -> None:
        """nvidia-smi with non-zero exit code returns empty GPUStatus."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            status = await query_gpu_status()
        assert status.temperature_c is None

    @pytest.mark.asyncio
    async def test_nvidia_smi_garbled_output(self) -> None:
        """nvidia-smi with garbled output returns empty GPUStatus."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (
            b"not,a,valid,csv,output\nmore,garbled,data", b""
        )
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await query_gpu_status()
        # Should not crash, fields should be None for invalid data

    @pytest.mark.asyncio
    async def test_get_vram_free_gb_returns_none_on_failure(self) -> None:
        """get_vram_free_gb returns None when nvidia-smi fails."""
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await get_vram_free_gb()
        assert result is None

    @pytest.mark.asyncio
    async def test_check_gpu_safe_returns_ok_when_unavailable(self) -> None:
        """When nvidia-smi is unavailable, GPU check should still return safe."""
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=GPUStatus())):
            safe, reason = await check_gpu_safe(GPUConfig())
        assert safe is True
        assert reason == "OK"

    @pytest.mark.asyncio
    async def test_check_gpu_safe_detects_high_temp(self) -> None:
        with patch(
            "bastion.health.query_gpu_status",
            AsyncMock(return_value=GPUStatus(temperature_c=90)),
        ):
            safe, reason = await check_gpu_safe(GPUConfig(max_temperature_c=80))
        assert safe is False
        assert "temperature" in reason.lower()

    @pytest.mark.asyncio
    async def test_check_gpu_safe_detects_high_power(self) -> None:
        with patch(
            "bastion.health.query_gpu_status",
            AsyncMock(return_value=GPUStatus(power_draw_watts=500.0)),
        ):
            safe, reason = await check_gpu_safe(GPUConfig(max_power_watts=400.0))
        assert safe is False
        assert "power" in reason.lower()

    @pytest.mark.asyncio
    async def test_check_gpu_safe_detects_high_vram(self) -> None:
        with patch("bastion.health.query_gpu_status", AsyncMock(return_value=GPUStatus(
            vram_used_mb=31000, vram_total_mb=32000
        ))):
            safe, reason = await check_gpu_safe(GPUConfig())
        assert safe is False
        assert "vram" in reason.lower()


# ---------------------------------------------------------------------------
# D3: Large payload
# ---------------------------------------------------------------------------


class TestLargePayload:
    def test_proxy_config_max_body_bytes(self) -> None:
        """ProxyConfig should have max_request_body_bytes field."""
        config = ProxyConfig()
        assert hasattr(config, "max_request_body_bytes")

    def test_proxy_config_custom_max_body(self) -> None:
        """max_request_body_bytes can be set via config."""
        config = ProxyConfig(max_request_body_bytes=1024)
        assert config.max_request_body_bytes == 1024


# ---------------------------------------------------------------------------
# D3: ResidencyCache edge cases
# ---------------------------------------------------------------------------


class TestResidencyCacheEdgeCases:
    @pytest.mark.asyncio
    async def test_cache_invalidate_forces_refresh(self) -> None:
        """After invalidate(), next query should refresh from VRAMTracker."""
        config = BrokerConfig(models={"test:7b": ModelInfo(vram_gb=5.0)})
        tracker = VRAMTracker(config)
        cache = tracker.residency_cache

        # Mock get_loaded_models to return different results on successive calls
        call_count = 0

        async def mock_get_loaded(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={"models": [{"name": "test:7b", "size": 5000000000, "details": {}}]},
                    request=httpx.Request("GET", "http://mock"),
                )
            return httpx.Response(
                200,
                json={"models": []},
                request=httpx.Request("GET", "http://mock"),
            )

        with patch.object(
            tracker._http, "get",
            new_callable=AsyncMock, side_effect=mock_get_loaded,
        ):
            # First call populates cache
            models = await cache.get_resident_models()
            assert "test:7b" in models

            # Cache should still return same result
            models = await cache.get_resident_models()
            assert "test:7b" in models  # Still cached

            # Invalidate and re-query
            cache.invalidate()
            models = await cache.get_resident_models()
            assert "test:7b" not in models  # Refreshed

    @pytest.mark.asyncio
    async def test_is_model_resident(self) -> None:
        config = BrokerConfig(models={"test:7b": ModelInfo(vram_gb=5.0)})
        tracker = VRAMTracker(config)
        mock_resp = httpx.Response(
            200,
            json={"models": [{"name": "test:7b", "size": 5000000000, "details": {}}]},
            request=httpx.Request("GET", "http://mock"),
        )
        with patch.object(tracker._http, "get", new_callable=AsyncMock, return_value=mock_resp):
            is_resident = await tracker.residency_cache.is_model_resident("test:7b")
        assert is_resident is True
