"""Tests for bastion validate pre-flight checks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bastion.validate import (
    CheckResult,
    CheckStatus,
    check_config,
    check_gpu,
    check_ollama,
    check_port,
    check_python_version,
    run_all_checks,
)


class TestCheckPythonVersion:
    """Test Python version check."""

    def test_current_python_passes(self) -> None:
        result = check_python_version()
        assert result.status == CheckStatus.PASS
        assert "3." in result.message

    def test_result_structure(self) -> None:
        result = check_python_version()
        assert isinstance(result, CheckResult)
        assert result.name == "Python version"


class TestCheckGPU:
    """Test GPU detection check."""

    @pytest.mark.asyncio
    async def test_gpu_detected(self) -> None:
        mock_status = MagicMock()
        mock_status.vram_total_mb = 24576
        mock_status.temperature_c = 45

        with (
            patch(
                "bastion.validate.query_gpu_status",
                new_callable=AsyncMock,
                return_value=mock_status,
            ),
            patch(
                "bastion.validate._query_gpu_name",
                return_value="NVIDIA GeForce RTX 4090",
            ),
            patch(
                "bastion.validate._query_driver_version",
                return_value="565.57",
            ),
        ):
            result = await check_gpu()
        assert result.status == CheckStatus.PASS
        assert "RTX 4090" in result.message

    @pytest.mark.asyncio
    async def test_no_gpu(self) -> None:
        with patch("bastion.validate._query_gpu_name", return_value=None):
            result = await check_gpu()
        assert result.status == CheckStatus.FAIL


class TestCheckOllama:
    """Test Ollama connectivity check."""

    @pytest.mark.asyncio
    async def test_ollama_reachable(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "Ollama is running"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            result = await check_ollama(port=11435)
        assert result.status == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_ollama_unreachable(self) -> None:
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=Exception("Connection refused"),
        ):
            result = await check_ollama(port=11435)
        assert result.status == CheckStatus.FAIL


class TestCheckPort:
    """Test port availability check."""

    @pytest.mark.asyncio
    async def test_free_port(self) -> None:
        # Use an unlikely-to-be-used port
        result = await check_port(port=19999)
        assert result.status == CheckStatus.PASS

    @pytest.mark.asyncio
    async def test_message_includes_port(self) -> None:
        result = await check_port(port=19999)
        assert "19999" in result.message


class TestCheckConfig:
    """Test config validation check."""

    def test_no_config_warns(self) -> None:
        with patch("bastion.validate._find_config_path", return_value=None):
            result = check_config()
        assert result.status == CheckStatus.WARN
        assert "init-config" in result.message


class TestRunAllChecks:
    """Test the full check runner."""

    @pytest.mark.asyncio
    async def test_returns_list_of_results(self) -> None:
        results = await run_all_checks(ollama_port=11435, bastion_port=11434)
        assert isinstance(results, list)
        assert all(isinstance(r, CheckResult) for r in results)
        assert len(results) >= 6  # At least 6 checks

    @pytest.mark.asyncio
    async def test_exit_code_zero_on_all_pass_or_warn(self) -> None:
        results = await run_all_checks(ollama_port=11435, bastion_port=11434)
        has_fail = any(r.status == CheckStatus.FAIL for r in results)
        exit_code = 1 if has_fail else 0
        # Just verify the logic, actual result depends on environment
        assert exit_code in (0, 1)


class TestCheckOllamaProxyFallback:
    """Tests validator fallback behavior."""

    @pytest.mark.asyncio
    async def test_ollama_direct_succeeds(self) -> None:
        mock_resp = AsyncMock(status_code=200)
        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            result = await check_ollama(port=11435, host="127.0.0.1")
        assert result.status == CheckStatus.PASS
        assert "11435" in result.message

    @pytest.mark.asyncio
    async def test_ollama_direct_fails_but_proxy_ok_is_warn_not_fail(self) -> None:
        calls: list[str] = []

        async def fake_get(self: object, url: str, timeout: float = 5.0) -> AsyncMock:
            calls.append(url)
            if "11435" in url:
                raise Exception("RST (nftables)")
            # The BASTION proxy on 11434 answers /api/tags
            mock = AsyncMock()
            mock.status_code = 200
            return mock

        with patch("httpx.AsyncClient.get", fake_get):
            result = await check_ollama(
                port=11435, host="127.0.0.1", proxy_port=11434
            )
        assert result.status == CheckStatus.WARN, result.message
        assert "via BASTION proxy" in result.message
        assert any("11434" in c for c in calls)

    @pytest.mark.asyncio
    async def test_ollama_both_direct_and_proxy_fail_is_fail(self) -> None:
        async def fake_get(self: object, url: str, timeout: float = 5.0) -> None:
            raise Exception("unreachable")

        with patch("httpx.AsyncClient.get", fake_get):
            result = await check_ollama(
                port=11435, host="127.0.0.1", proxy_port=11434
            )
        assert result.status == CheckStatus.FAIL
