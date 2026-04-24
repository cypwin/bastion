"""Tests for token-bucket rate limiting middleware."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bastion.ratelimit import RateLimitConfig, RateLimitMiddleware


def _make_app(config: RateLimitConfig) -> TestClient:
    """Create a minimal FastAPI app with RateLimitMiddleware."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=config)

    @app.get("/api/tags")
    async def tags() -> dict:
        return {"models": []}

    return TestClient(app)


class TestRateLimitDisabled:
    """When rate limiting is disabled, all requests pass."""

    def test_disabled_flag(self) -> None:
        client = _make_app(RateLimitConfig(enabled=False))
        for _ in range(20):
            resp = client.get("/api/tags")
            assert resp.status_code == 200

    def test_zero_rpm_passes(self) -> None:
        """requests_per_minute=0 effectively disables rate limiting."""
        client = _make_app(
            RateLimitConfig(enabled=True, requests_per_minute=0, burst=5)
        )
        resp = client.get("/api/tags")
        assert resp.status_code == 200


class TestRateLimitEnabled:
    """When rate limiting is enabled, burst is enforced per client IP."""

    def test_within_burst_passes(self) -> None:
        config = RateLimitConfig(enabled=True, requests_per_minute=60, burst=5)
        client = _make_app(config)
        # First 5 requests should pass (burst allows 5 tokens)
        for _ in range(5):
            resp = client.get("/api/tags")
            assert resp.status_code == 200

    def test_exceeding_burst_returns_429(self) -> None:
        config = RateLimitConfig(enabled=True, requests_per_minute=60, burst=3)
        client = _make_app(config)
        # Exhaust the burst allowance
        for _ in range(3):
            resp = client.get("/api/tags")
            assert resp.status_code == 200

        # Next request should be rate limited
        resp = client.get("/api/tags")
        assert resp.status_code == 429
        assert "Too many requests" in resp.json()["error"]

    def test_429_includes_retry_after_header(self) -> None:
        config = RateLimitConfig(enabled=True, requests_per_minute=60, burst=1)
        client = _make_app(config)
        # Use the single token
        resp = client.get("/api/tags")
        assert resp.status_code == 200

        # Trigger rate limit
        resp = client.get("/api/tags")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        retry_after = int(resp.headers["Retry-After"])
        assert retry_after >= 1

    def test_different_ips_get_separate_buckets(self) -> None:
        # TestClient's socket peer is "testclient" — add it to trusted_proxies
        # so that X-Forwarded-For is honored and each distinct XFF IP gets
        # its own bucket.
        config = RateLimitConfig(
            enabled=True,
            requests_per_minute=60,
            burst=2,
            trusted_proxies=["testclient"],
        )
        client = _make_app(config)

        # Exhaust bucket for IP "10.0.0.1"
        for _ in range(2):
            resp = client.get(
                "/api/tags",
                headers={"X-Forwarded-For": "10.0.0.1"},
            )
            assert resp.status_code == 200

        # "10.0.0.1" is now rate limited
        resp = client.get(
            "/api/tags",
            headers={"X-Forwarded-For": "10.0.0.1"},
        )
        assert resp.status_code == 429

        # "10.0.0.2" should still have a fresh bucket
        resp = client.get(
            "/api/tags",
            headers={"X-Forwarded-For": "10.0.0.2"},
        )
        assert resp.status_code == 200


def test_xff_ignored_when_no_trusted_proxies() -> None:
    from bastion.ratelimit import RateLimitConfig, RateLimitMiddleware
    from starlette.requests import Request

    config = RateLimitConfig(enabled=True, trusted_proxies=[])
    mw = RateLimitMiddleware(app=None, config=config)

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"10.0.0.99")],
        "client": ("192.168.1.50", 1234),
    }
    ip = mw._get_client_ip(Request(scope))
    assert ip == "192.168.1.50"  # socket peer, not XFF


def test_xff_used_when_peer_is_trusted_proxy() -> None:
    from bastion.ratelimit import RateLimitConfig, RateLimitMiddleware
    from starlette.requests import Request

    config = RateLimitConfig(enabled=True, trusted_proxies=["192.168.1.50"])
    mw = RateLimitMiddleware(app=None, config=config)
    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"10.0.0.99")],
        "client": ("192.168.1.50", 1234),
    }
    ip = mw._get_client_ip(Request(scope))
    assert ip == "10.0.0.99"  # XFF accepted because peer is trusted
