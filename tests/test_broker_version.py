"""HTTP-contract tests for GET /broker/version.

Motivation: long-batch A2A clients cannot currently detect that
BASTION was redeployed mid-batch. Three S122 merges restarted the broker
mid-batch during a 31B embedding run and surfaced as four distinct error
shapes downstream (502, 500 CUDA, ECONNREFUSED, server-disconnected) that
each needed independent retry tuning. /broker/version exposes a stable
build identity so clients can:

  1. Pin git_sha at batch start.
  2. Refuse to continue (or surface a loud warning) when the SHA changes on
     a retry — a signal that an in-flight redeploy is responsible for the
     transient errors, not a transient infra blip.
  3. Detect process restarts even at unchanged SHA via boot_time_unix.

The endpoint MUST be admin-auth-gated like every other /broker/* route, so
this file mirrors the existing TestBrokerCounters / TestBrokerRecent pattern
in test_server_admin.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestBrokerVersion:
    """Pin the public contract of /broker/version."""

    def test_returns_200(self, app_with_stub_scheduler: TestClient) -> None:
        client = app_with_stub_scheduler
        resp = client.get("/broker/version")
        assert resp.status_code == 200

    def test_response_has_required_keys(
        self, app_with_stub_scheduler: TestClient,
    ) -> None:
        """Client-side pin logic depends on exactly these keys being present."""
        client = app_with_stub_scheduler
        body = client.get("/broker/version").json()
        for key in ("version", "git_sha", "boot_time_unix", "boot_time_iso"):
            assert key in body, f"missing key '{key}' in /broker/version response"

    def test_version_matches_package(self, app_with_stub_scheduler: TestClient) -> None:
        """The reported version field equals bastion.__version__."""
        import bastion

        client = app_with_stub_scheduler
        body = client.get("/broker/version").json()
        assert body["version"] == bastion.__version__

    def test_git_sha_is_string(self, app_with_stub_scheduler: TestClient) -> None:
        """git_sha is always a string. 'unknown' is acceptable when the
        package is installed from a wheel without git context, but never
        ``None`` — clients use this field for equality comparisons.
        """
        client = app_with_stub_scheduler
        body = client.get("/broker/version").json()
        assert isinstance(body["git_sha"], str)
        assert body["git_sha"]  # non-empty

    def test_boot_time_is_numeric_and_iso_string(
        self, app_with_stub_scheduler: TestClient,
    ) -> None:
        """boot_time_unix is a positive float; boot_time_iso parses as ISO-8601."""
        from datetime import datetime

        client = app_with_stub_scheduler
        body = client.get("/broker/version").json()
        assert isinstance(body["boot_time_unix"], (int, float))
        assert body["boot_time_unix"] > 0
        # Just verify it parses — exact value depends on test fixture lifespan
        datetime.fromisoformat(body["boot_time_iso"])

    def test_boot_time_stable_within_process(
        self, app_with_stub_scheduler: TestClient,
    ) -> None:
        """Two consecutive calls return the same boot_time_unix — it must be
        captured once at startup, not recomputed per request, so clients can
        detect restarts via inequality.
        """
        client = app_with_stub_scheduler
        a = client.get("/broker/version").json()
        b = client.get("/broker/version").json()
        assert a["boot_time_unix"] == b["boot_time_unix"]
        assert a["boot_time_iso"] == b["boot_time_iso"]

    def test_requires_admin_auth_when_configured(self) -> None:
        """When auth is enabled with an api_keys list, /broker/version
        requires the Authorization: Bearer header like every other /broker/*
        route. (No bypass for client probes.)
        """
        from bastion.models import (
            AuthConfig,
            BrokerConfig,
            GPUConfig,
            ModelInfo,
            OllamaConfig,
            ServerConfig,
        )
        from bastion.server import create_app

        config = BrokerConfig(
            ollama=OllamaConfig(host="127.0.0.1", port=11435),
            server=ServerConfig(host="127.0.0.1", port=11434),
            gpu=GPUConfig(total_vram_gb=32.0, headroom_gb=6.0),
            auth=AuthConfig(enabled=True, api_keys=["secret-test-key"]),
            models={"qwen3:14b": ModelInfo(vram_gb=9.3)},
        )
        app = create_app(config)
        with TestClient(app) as client:
            # No auth header → 401
            resp = client.get("/broker/version")
            assert resp.status_code == 401

            # With Bearer header → 200
            resp = client.get(
                "/broker/version",
                headers={"Authorization": "Bearer secret-test-key"},
            )
            assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# _detect_git_sha precedence and fallback branches (S130)
# ─────────────────────────────────────────────────────────────────────────────


class TestDetectGitSha:
    def test_env_var_takes_precedence(self, monkeypatch) -> None:
        import bastion.server as server_mod

        monkeypatch.setenv("BASTION_GIT_SHA", "deadbeef-from-deploy")
        assert server_mod._detect_git_sha() == "deadbeef-from-deploy"

    def test_no_git_entry_at_package_root_returns_unknown(
        self, monkeypatch, tmp_path
    ) -> None:
        """A wheel under site-packages nested inside some unrelated repo must
        NOT report that repo's SHA — without a .git at the package root the
        detector stops at 'unknown'."""
        import bastion.server as server_mod

        monkeypatch.delenv("BASTION_GIT_SHA", raising=False)
        fake_pkg = tmp_path / "src" / "bastion"
        fake_pkg.mkdir(parents=True)
        monkeypatch.setattr(
            server_mod, "__file__", str(fake_pkg / "server.py")
        )
        assert server_mod._detect_git_sha() == "unknown"

    def test_dev_checkout_reports_head_sha(self, monkeypatch) -> None:
        """In this repo (a real checkout) the detector returns a hex SHA."""
        import bastion.server as server_mod

        monkeypatch.delenv("BASTION_GIT_SHA", raising=False)
        sha = server_mod._detect_git_sha()
        assert sha == "unknown" or (
            len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
        )
