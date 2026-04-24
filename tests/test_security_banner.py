"""Tests startup banner warns on insecure bind + auth disabled."""
from __future__ import annotations

from bastion.__main__ import _security_banner_lines
from bastion.models import AuthConfig, BrokerConfig, RateLimitConfig, ServerConfig


def test_banner_warns_on_0000_with_auth_off():
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="0.0.0.0")
    cfg.auth = AuthConfig(enabled=False)
    lines = _security_banner_lines(cfg)
    text = "\n".join(lines)
    assert "SECURITY WARNING" in text
    assert "0.0.0.0" in text
    assert "auth is disabled" in text.lower()


def test_banner_silent_on_localhost():
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="127.0.0.1")
    cfg.auth = AuthConfig(enabled=False)
    assert _security_banner_lines(cfg) == []


def test_banner_silent_when_auth_enabled_with_keys():
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="0.0.0.0")
    cfg.auth = AuthConfig(enabled=True, api_keys=["k"])
    # With Task 11's rate-limit and A2A checks, the banner is silent only when
    # all security axes are configured on a public bind.
    cfg.rate_limit = RateLimitConfig(enabled=True)
    # a2a.enabled defaults to False, so the A2A check is skipped naturally
    assert _security_banner_lines(cfg) == []


def test_banner_warns_when_auth_enabled_but_no_keys():
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="0.0.0.0")
    cfg.auth = AuthConfig(enabled=True, api_keys=[])
    lines = _security_banner_lines(cfg)
    assert any("no api_keys configured" in line.lower() for line in lines)


def test_banner_warns_on_empty_a2a_tokens():
    from bastion.models import A2AConfig
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="0.0.0.0")
    cfg.auth = AuthConfig(enabled=True, api_keys=["k"])
    # A2A enabled but tokens empty — endpoints are open
    cfg.a2a = A2AConfig(enabled=True, tokens=[])
    lines = _security_banner_lines(cfg)
    text = "\n".join(lines).lower()
    assert "a2a" in text
    assert "tokens" in text


def test_banner_warns_on_rate_limit_off_with_public_bind():
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="0.0.0.0")
    cfg.auth = AuthConfig(enabled=True, api_keys=["k"])
    # Rate limiting disabled on a public bind — GPU saturation risk regardless of A2A
    cfg.rate_limit = RateLimitConfig(enabled=False)
    lines = _security_banner_lines(cfg)
    text = "\n".join(lines).lower()
    assert "rate limit" in text
