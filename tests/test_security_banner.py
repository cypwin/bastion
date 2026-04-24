"""Tests startup banner warns on insecure bind + auth disabled."""
from __future__ import annotations

from bastion.__main__ import _security_banner_lines
from bastion.models import AuthConfig, BrokerConfig, ServerConfig


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
    assert _security_banner_lines(cfg) == []


def test_banner_warns_when_auth_enabled_but_no_keys():
    cfg = BrokerConfig()
    cfg.server = ServerConfig(host="0.0.0.0")
    cfg.auth = AuthConfig(enabled=True, api_keys=[])
    lines = _security_banner_lines(cfg)
    assert any("no api_keys configured" in l.lower() for l in lines)
