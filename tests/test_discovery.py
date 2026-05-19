"""Tests for ``bastion.discovery`` — the ``--detect-models`` UX path.

These tests pin down the behavior of the model discovery helper that powers
the ``bastion --detect-models`` CLI flag. The module probes Ollama's HTTP
``/api/tags`` endpoint first, then falls back to the ``ollama list`` CLI,
and finally prints a YAML ``models:`` config section the user can paste into
``broker.yaml``.

Mocks (no real network, no real subprocess):
  * ``httpx.get`` — patched to simulate Ollama HTTP responses
  * ``subprocess.run`` — patched to simulate the ``ollama list`` CLI
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest

from bastion.discovery import (
    _is_likely_embedding,
    _parse_size_from_ollama_list,
    _query_ollama_cli,
    _query_ollama_models,
    detect_models,
)

# ---------------------------------------------------------------------------
# _is_likely_embedding — heuristic classification
# ---------------------------------------------------------------------------


def test_embedding_detected_by_small_size() -> None:
    """Models under 1.0 GB are classified as embeddings regardless of name."""
    assert _is_likely_embedding("some-random-model", 0.4) is True


def test_embedding_detected_by_name_hint_embed() -> None:
    """Name fragment 'embed' marks a model as an embedding even if large."""
    assert _is_likely_embedding("nomic-embed-text", 5.0) is True


def test_embedding_detected_by_name_hint_bge() -> None:
    """Name fragment 'bge' marks a model as an embedding."""
    assert _is_likely_embedding("bge-large", 2.0) is True


def test_embedding_detected_by_name_hint_nomic() -> None:
    """Name fragment 'nomic' marks a model as an embedding."""
    assert _is_likely_embedding("nomic-text", 3.0) is True


def test_non_embedding_when_large_and_unknown_name() -> None:
    """Large generic model names are NOT classified as embeddings."""
    assert _is_likely_embedding("llama3.1:8b", 4.5) is False


def test_embedding_threshold_boundary_below() -> None:
    """A model exactly below the 1.0 GB threshold is classified embedding."""
    assert _is_likely_embedding("foo", 0.9) is True


def test_embedding_threshold_boundary_at_one_gb() -> None:
    """A model exactly at the 1.0 GB threshold is NOT below; needs name hint."""
    # 1.0 is not < 1.0, and 'foo' has no embedding hint
    assert _is_likely_embedding("foo", 1.0) is False


def test_embedding_name_match_case_insensitive() -> None:
    """Embedding name hints match case-insensitively."""
    assert _is_likely_embedding("NOMIC-EMBED-TEXT", 5.0) is True


# ---------------------------------------------------------------------------
# _parse_size_from_ollama_list — size column extraction
# ---------------------------------------------------------------------------


def test_parse_size_gb_token() -> None:
    """Extracts the float preceding a 'GB' token."""
    parts = ["llama3.1:8b", "abc123", "4.7", "GB", "2", "weeks", "ago"]
    assert _parse_size_from_ollama_list(parts) == 4.7


def test_parse_size_mb_token_converted_to_gb() -> None:
    """MB sizes are converted to GB."""
    parts = ["nomic-embed-text", "id456", "274", "MB", "1", "day", "ago"]
    # 274 / 1024 ≈ 0.267 → rounded to 0.3
    assert _parse_size_from_ollama_list(parts) == 0.3


def test_parse_size_handles_lowercase_units() -> None:
    """Unit suffix matching is case-insensitive via .upper()."""
    parts = ["foo", "id", "1.5", "gb"]
    assert _parse_size_from_ollama_list(parts) == 1.5


def test_parse_size_returns_zero_when_no_unit() -> None:
    """No GB/MB token → 0.0 (silent default, never raises)."""
    parts = ["model-name", "id", "garbage", "stuff"]
    assert _parse_size_from_ollama_list(parts) == 0.0


def test_parse_size_handles_unparseable_value_before_unit() -> None:
    """Non-numeric value before 'GB' is swallowed and returns 0.0."""
    parts = ["model", "id", "notanumber", "GB"]
    assert _parse_size_from_ollama_list(parts) == 0.0


def test_parse_size_empty_parts_returns_zero() -> None:
    """Empty input list returns 0.0 (no parts to iterate)."""
    assert _parse_size_from_ollama_list([]) == 0.0


def test_parse_size_unit_in_position_zero_ignored() -> None:
    """A 'GB' at index 0 has no preceding value and is skipped."""
    parts = ["GB", "stuff"]
    assert _parse_size_from_ollama_list(parts) == 0.0


# ---------------------------------------------------------------------------
# _query_ollama_models — HTTP API path
# ---------------------------------------------------------------------------


def _mock_httpx_response(status_code: int = 200, json_data=None) -> httpx.Response:
    """Build a minimal httpx.Response carrying the supplied JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=json_data if json_data is not None else {},
        request=httpx.Request("GET", "http://mock/api/tags"),
    )


def test_query_ollama_models_happy_path() -> None:
    """Typical Ollama /api/tags response yields sorted model list with GB sizes."""
    fake_resp = _mock_httpx_response(json_data={
        "models": [
            {"name": "qwen3:14b", "size": 10 * (1024**3)},   # 10 GB
            {"name": "llama3.1:8b", "size": int(4.5 * (1024**3))},  # 4.5 GB
        ],
    })
    with patch("httpx.get", return_value=fake_resp):
        out = _query_ollama_models("http://127.0.0.1:11435")

    assert out is not None
    assert len(out) == 2
    # Sorted alphabetically by name
    assert out[0]["name"] == "llama3.1:8b"
    assert out[1]["name"] == "qwen3:14b"
    assert out[0]["size_gb"] == 4.5
    assert out[1]["size_gb"] == 10.0


def test_query_ollama_models_empty_list() -> None:
    """Ollama with no installed models returns an empty list (not None)."""
    fake_resp = _mock_httpx_response(json_data={"models": []})
    with patch("httpx.get", return_value=fake_resp):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out == []


def test_query_ollama_models_missing_models_key() -> None:
    """Response without 'models' key is treated as empty."""
    fake_resp = _mock_httpx_response(json_data={"other_key": "value"})
    with patch("httpx.get", return_value=fake_resp):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out == []


def test_query_ollama_models_entry_missing_fields_uses_defaults() -> None:
    """Per-model entry without 'name'/'size' falls back to safe defaults."""
    fake_resp = _mock_httpx_response(json_data={"models": [{}]})
    with patch("httpx.get", return_value=fake_resp):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out == [{"name": "unknown", "size_gb": 0.0}]


def test_query_ollama_models_returns_none_on_connection_error() -> None:
    """Connection refused / network failure returns None (signal for CLI fallback)."""
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out is None


def test_query_ollama_models_returns_none_on_timeout() -> None:
    """Timeout returns None so the caller can try the CLI fallback."""
    with patch("httpx.get", side_effect=httpx.TimeoutException("slow")):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out is None


def test_query_ollama_models_returns_none_on_http_error() -> None:
    """Non-2xx response (raise_for_status) returns None."""
    fake_resp = _mock_httpx_response(status_code=500, json_data={"error": "boom"})
    with patch("httpx.get", return_value=fake_resp):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out is None


def test_query_ollama_models_returns_none_on_garbage_json() -> None:
    """Malformed JSON body (or .json() raising) returns None, not a crash."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(side_effect=ValueError("not json"))
    with patch("httpx.get", return_value=fake_resp):
        out = _query_ollama_models("http://127.0.0.1:11435")
    assert out is None


# ---------------------------------------------------------------------------
# _query_ollama_cli — subprocess fallback
# ---------------------------------------------------------------------------


def _make_subprocess_result(stdout: str, returncode: int = 0):
    """Build a CompletedProcess-like MagicMock for subprocess.run."""
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def test_query_ollama_cli_happy_path() -> None:
    """Parses `ollama list` table output into sorted [{name, size_gb}] dicts."""
    stdout = (
        "NAME              ID            SIZE      MODIFIED\n"
        "qwen3:14b         aaa111        9.3 GB    2 weeks ago\n"
        "llama3.1:8b       bbb222        4.5 GB    3 days ago\n"
    )
    result = _make_subprocess_result(stdout)
    with patch("subprocess.run", return_value=result):
        out = _query_ollama_cli()

    assert len(out) == 2
    assert out[0]["name"] == "llama3.1:8b"
    assert out[0]["size_gb"] == 4.5
    assert out[1]["name"] == "qwen3:14b"
    assert out[1]["size_gb"] == 9.3


def test_query_ollama_cli_skips_blank_lines() -> None:
    """Blank lines inside the table body are skipped without crashing."""
    stdout = (
        "NAME    ID    SIZE    MODIFIED\n"
        "foo     a     1.0 GB  today\n"
        "\n"
        "bar     b     2.0 GB  today\n"
    )
    result = _make_subprocess_result(stdout)
    with patch("subprocess.run", return_value=result):
        out = _query_ollama_cli()
    names = [m["name"] for m in out]
    assert names == ["bar", "foo"]


def test_query_ollama_cli_returns_empty_on_nonzero_exit() -> None:
    """A non-zero ollama exit code yields an empty list."""
    result = _make_subprocess_result("error", returncode=1)
    with patch("subprocess.run", return_value=result):
        out = _query_ollama_cli()
    assert out == []


def test_query_ollama_cli_returns_empty_when_ollama_missing() -> None:
    """FileNotFoundError (ollama binary absent) degrades gracefully to []."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        out = _query_ollama_cli()
    assert out == []


def test_query_ollama_cli_returns_empty_on_timeout() -> None:
    """Generic exceptions (e.g. TimeoutExpired) return empty list, not raise."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ollama", timeout=10),
    ):
        out = _query_ollama_cli()
    assert out == []


def test_query_ollama_cli_only_header_returns_empty() -> None:
    """Output with just a header row (no model rows) returns []."""
    result = _make_subprocess_result("NAME    ID    SIZE    MODIFIED\n")
    with patch("subprocess.run", return_value=result):
        out = _query_ollama_cli()
    assert out == []


# ---------------------------------------------------------------------------
# detect_models — top-level CLI entry point
# ---------------------------------------------------------------------------


def test_detect_models_prints_help_when_no_models(capsys: pytest.CaptureFixture[str]) -> None:
    """No models anywhere → prints onboarding hints and `ollama pull` suggestions."""
    with patch("bastion.discovery._query_ollama_models", return_value=None), \
         patch("bastion.discovery._query_ollama_cli", return_value=[]):
        detect_models()

    out = capsys.readouterr().out
    assert "No Ollama models found" in out
    assert "ollama.com/library" in out
    assert "ollama pull llama3.1:8b" in out
    assert "After pulling" in out


def test_detect_models_falls_back_to_cli_when_api_unreachable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When HTTP API returns None, the CLI fallback is invoked and its result used."""
    cli_models = [{"name": "qwen3:14b", "size_gb": 9.3}]
    with patch("bastion.discovery._query_ollama_models", return_value=None) as http_mock, \
         patch("bastion.discovery._query_ollama_cli", return_value=cli_models) as cli_mock:
        detect_models()

    assert http_mock.called
    assert cli_mock.called
    out = capsys.readouterr().out
    assert "Found 1 model(s)" in out
    assert "qwen3:14b" in out


def test_detect_models_does_not_call_cli_when_api_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful (even empty) HTTP response short-circuits the CLI fallback."""
    with patch("bastion.discovery._query_ollama_models", return_value=[]) as http_mock, \
         patch("bastion.discovery._query_ollama_cli") as cli_mock:
        detect_models()

    assert http_mock.called
    assert not cli_mock.called  # API returned [] (not None), CLI not consulted
    out = capsys.readouterr().out
    assert "No Ollama models found" in out


def test_detect_models_emits_yaml_skeleton_for_general_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """General (non-embedding) models get default_num_ctx=4096 and 'general' tag."""
    models = [{"name": "llama3.1:8b", "size_gb": 4.5}]
    with patch("bastion.discovery._query_ollama_models", return_value=models):
        detect_models()

    out = capsys.readouterr().out
    assert "Found 1 model(s)" in out
    assert "models:" in out
    assert '"llama3.1:8b":' in out
    assert "vram_gb: 4.5" in out
    assert "default_num_ctx: 4096" in out
    assert '"general"' in out
    # Embedding-only flags must NOT appear
    assert "always_allowed: true" not in out
    assert '"embedding"' not in out


def test_detect_models_marks_embedding_with_always_allowed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Embedding models get default_num_ctx=512, always_allowed, embedding tag."""
    models = [{"name": "nomic-embed-text", "size_gb": 0.3}]
    with patch("bastion.discovery._query_ollama_models", return_value=models):
        detect_models()

    out = capsys.readouterr().out
    assert '"nomic-embed-text":' in out
    assert "default_num_ctx: 512" in out
    assert "always_allowed: true" in out
    assert '"embedding"' in out


def test_detect_models_total_vram_excludes_embeddings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Footer 'Total VRAM (non-embedding)' sums only non-embedding model sizes."""
    models = [
        {"name": "llama3.1:8b", "size_gb": 4.5},
        {"name": "qwen3:14b", "size_gb": 9.3},
        {"name": "nomic-embed-text", "size_gb": 0.3},
    ]
    with patch("bastion.discovery._query_ollama_models", return_value=models):
        detect_models()

    out = capsys.readouterr().out
    # 4.5 + 9.3 = 13.8 (embedding excluded)
    assert "Total VRAM (non-embedding): 13.8 GB" in out
    assert "Total models: 3" in out


def test_detect_models_passes_host_and_port_to_query(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Custom host/port args are propagated into the Ollama base URL."""
    with patch("bastion.discovery._query_ollama_models", return_value=[]) as mock:
        detect_models(ollama_host="example.local", ollama_port=9999)

    # Verify the constructed base_url was passed in
    assert mock.called
    base_url = mock.call_args[0][0]
    assert base_url == "http://example.local:9999"
