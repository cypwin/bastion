"""CLI tests for ``python -m bastion`` (bastion.__main__).

Exercises argparse flag wiring, dispatch, and the security banner branches
without booting uvicorn, probing nvidia-smi, or hitting Ollama. Every heavy
side-effect (uvicorn.run, validate runner, stress test, model discovery,
generate-config) is mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bastion import __main__ as cli
from bastion.models import (
    A2AConfig,
    AuthConfig,
    BrokerConfig,
    OllamaConfig,
    RateLimitConfig,
    ServerConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """Invoke ``cli.main()`` after stuffing ``sys.argv``."""
    monkeypatch.setattr("sys.argv", ["bastion", *argv])
    cli.main()


def _make_config(
    *,
    host: str = "127.0.0.1",
    port: int = 11434,
    admin_port: int = 0,
    auth_enabled: bool = False,
    api_keys: list[str] | None = None,
    a2a_enabled: bool = False,
    a2a_tokens: list[str] | None = None,
    rate_limit_enabled: bool = True,
) -> BrokerConfig:
    """Build a BrokerConfig for banner / dispatch tests."""
    return BrokerConfig(
        ollama=OllamaConfig(host="127.0.0.1", port=11435),
        server=ServerConfig(host=host, port=port, admin_port=admin_port),
        auth=AuthConfig(enabled=auth_enabled, api_keys=api_keys or []),
        rate_limit=RateLimitConfig(enabled=rate_limit_enabled),
        a2a=A2AConfig(enabled=a2a_enabled, tokens=a2a_tokens or []),
    )


# ---------------------------------------------------------------------------
# --- argument parsing & defaults
# ---------------------------------------------------------------------------


def test_help_flag_exits_zero_and_prints_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--help` writes usage to stdout and exits with code 0."""
    monkeypatch.setattr("sys.argv", ["bastion", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "BASTION" in out
    assert "--config" in out
    assert "--validate" in out
    assert "--stress-test" in out
    assert "--detect-models" in out
    assert "--init-config" in out
    assert "--admin-port" in out


def test_invalid_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse choices guard --log-level."""
    monkeypatch.setattr("sys.argv", ["bastion", "--log-level", "VERBOSE"])
    with pytest.raises(SystemExit):
        cli.main()


# ---------------------------------------------------------------------------
# --- --init-config dispatch
# ---------------------------------------------------------------------------


def test_init_config_dispatches_to_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--init-config` calls _generate_config and returns without booting."""
    gen = MagicMock()
    monkeypatch.setattr(cli, "_generate_config", gen)
    # Sanity guard: if dispatch is wrong and we fall through, uvicorn would run.
    with patch("bastion.__main__.uvicorn.run") as uv:
        _run_cli(monkeypatch, ["--init-config"])
    gen.assert_called_once_with()
    uv.assert_not_called()


def test_generate_config_skips_when_destination_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_generate_config refuses to clobber an existing broker.yaml."""
    dest = tmp_path / "broker.yaml"
    dest.write_text("existing: true\n")
    monkeypatch.setattr("bastion.paths.config_dir", lambda: tmp_path)
    cli._generate_config()
    out = capsys.readouterr().out
    assert "already exists" in out
    assert dest.read_text() == "existing: true\n"


def test_generate_config_writes_inline_when_example_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When no bundled example exists, _generate_config writes an inline stub."""
    monkeypatch.setattr("bastion.paths.config_dir", lambda: tmp_path)
    # Force the example-not-found branch by pointing __file__ inside tmp_path.
    fake_file = tmp_path / "pkg" / "bastion" / "__main__.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("")
    monkeypatch.setattr(cli, "__file__", str(fake_file))
    cli._generate_config()
    out = capsys.readouterr().out
    written = (tmp_path / "broker.yaml").read_text()
    assert "BASTION configuration" in written
    assert "ollama:" in written
    assert "Config written to" in out


# ---------------------------------------------------------------------------
# --- --detect-models dispatch
# ---------------------------------------------------------------------------


def test_detect_models_dispatches_with_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect = MagicMock()
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.discovery",
        MagicMock(detect_models=detect),
    )
    with patch("bastion.__main__.uvicorn.run") as uv:
        _run_cli(monkeypatch, ["--detect-models"])
    detect.assert_called_once_with(ollama_port=11435)
    uv.assert_not_called()


def test_detect_models_honors_ollama_port_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect = MagicMock()
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.discovery",
        MagicMock(detect_models=detect),
    )
    _run_cli(monkeypatch, ["--detect-models", "--ollama-port", "9999"])
    detect.assert_called_once_with(ollama_port=9999)


# ---------------------------------------------------------------------------
# --- --validate dispatch
# ---------------------------------------------------------------------------


def test_validate_dispatches_and_exits_with_computed_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--validate` runs checks, prints output, and exits with compute_exit_code."""
    run_all = MagicMock(return_value=[])
    fake_validate = MagicMock(
        run_all_checks=run_all,
        format_results=MagicMock(return_value="VALIDATION_OUTPUT"),
        compute_exit_code=MagicMock(return_value=7),
    )
    monkeypatch.setitem(__import__("sys").modules, "bastion.validate", fake_validate)
    monkeypatch.setattr("asyncio.run", lambda coro: run_all.return_value)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["--validate"])
    assert exc.value.code == 7
    assert "VALIDATION_OUTPUT" in capsys.readouterr().out
    run_all.assert_called_once_with(ollama_port=11435, bastion_port=11434)


def test_validate_uses_cli_overrides_for_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_all = MagicMock(return_value=[])
    fake_validate = MagicMock(
        run_all_checks=run_all,
        format_results=MagicMock(return_value=""),
        compute_exit_code=MagicMock(return_value=0),
    )
    monkeypatch.setitem(__import__("sys").modules, "bastion.validate", fake_validate)
    monkeypatch.setattr("asyncio.run", lambda coro: run_all.return_value)
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, [
            "--validate", "--port", "9001", "--ollama-port", "9002",
        ])
    run_all.assert_called_once_with(ollama_port=9002, bastion_port=9001)


# ---------------------------------------------------------------------------
# --- --stress-test dispatch
# ---------------------------------------------------------------------------


def test_stress_test_aborts_without_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stress test requires exact 'I understand' acknowledgement."""
    fake_stress = MagicMock(
        SAFETY_BANNER="BANNER",
        StressConfig=MagicMock(return_value=MagicMock(bastion_url="http://x")),
        recovery_phase=MagicMock(),
    )
    monkeypatch.setitem(__import__("sys").modules, "bastion.stress", fake_stress)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "no thanks")
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["--stress-test"])
    assert exc.value.code == 0
    assert "Aborted" in capsys.readouterr().out


def test_stress_test_handles_ctrl_c_at_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """KeyboardInterrupt at the safety prompt exits gracefully."""
    fake_stress = MagicMock(
        SAFETY_BANNER="",
        StressConfig=MagicMock(return_value=MagicMock(bastion_url="http://x")),
        recovery_phase=MagicMock(),
    )
    monkeypatch.setitem(__import__("sys").modules, "bastion.stress", fake_stress)

    def _raise(*_a: Any, **_kw: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["--stress-test"])
    assert exc.value.code == 0
    assert "Aborted" in capsys.readouterr().out


def test_stress_test_runs_when_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 'I understand' the runner is invoked via asyncio.run."""
    fake_stress = MagicMock(
        SAFETY_BANNER="",
        StressConfig=MagicMock(return_value=MagicMock(bastion_url="http://127.0.0.1:11434")),
        recovery_phase=MagicMock(),
    )
    monkeypatch.setitem(__import__("sys").modules, "bastion.stress", fake_stress)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "I understand")
    runs: list[Any] = []
    monkeypatch.setattr("asyncio.run", lambda coro: runs.append(coro) or None)
    # Make sure _run_stress_test returns a coroutine but doesn't actually execute.
    monkeypatch.setattr(cli, "_run_stress_test", MagicMock(return_value=MagicMock()))
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["--stress-test"])
    assert exc.value.code == 0
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# --- default path: load config + uvicorn boot
# ---------------------------------------------------------------------------


def test_default_path_single_port_calls_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No subcommand → load_config → create_app → uvicorn.run."""
    cfg = _make_config(host="127.0.0.1", port=11434)
    fake_app = object()
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.config",
        MagicMock(load_config=MagicMock(return_value=cfg)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.server",
        MagicMock(create_app=MagicMock(return_value=fake_app)),
    )
    with patch("bastion.__main__.uvicorn.run") as uv:
        _run_cli(monkeypatch, [])
    uv.assert_called_once()
    kwargs = uv.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 11434
    assert kwargs["log_level"] == "info"


def test_default_path_cli_host_and_port_override_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--host` and `--port` win over config values."""
    cfg = _make_config(host="127.0.0.1", port=11434)
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.config",
        MagicMock(load_config=MagicMock(return_value=cfg)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.server",
        MagicMock(create_app=MagicMock(return_value=object())),
    )
    with patch("bastion.__main__.uvicorn.run") as uv:
        _run_cli(monkeypatch, ["--host", "10.0.0.1", "--port", "8000"])
    kwargs = uv.call_args.kwargs
    assert kwargs["host"] == "10.0.0.1"
    assert kwargs["port"] == 8000


def test_default_path_admin_port_triggers_two_port_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--admin-port` mutates config and routes to _run_two_port via asyncio.run."""
    cfg = _make_config(host="127.0.0.1", port=11434, admin_port=0)
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.config",
        MagicMock(load_config=MagicMock(return_value=cfg)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.server",
        MagicMock(create_app=MagicMock(return_value=object())),
    )
    monkeypatch.setattr(cli, "_run_two_port", MagicMock(return_value=MagicMock()))
    captured: list[Any] = []
    monkeypatch.setattr("asyncio.run", lambda coro: captured.append(coro) or None)
    with patch("bastion.__main__.uvicorn.run") as uv:
        _run_cli(monkeypatch, ["--admin-port", "9999"])
    # Single-port uvicorn.run must NOT be called when two-port mode is active.
    uv.assert_not_called()
    assert cfg.server.admin_port == 9999
    assert cfg.server.two_port_mode is True
    assert len(captured) == 1


def test_default_path_ollama_port_override_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--ollama-port` rewrites config.ollama.port."""
    cfg = _make_config()
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.config",
        MagicMock(load_config=MagicMock(return_value=cfg)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.server",
        MagicMock(create_app=MagicMock(return_value=object())),
    )
    with patch("bastion.__main__.uvicorn.run"):
        _run_cli(monkeypatch, ["--ollama-port", "12000"])
    assert cfg.ollama.port == 12000


def test_default_path_missing_config_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FileNotFoundError from load_config must surface as sys.exit(1)."""
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.config",
        MagicMock(load_config=MagicMock(side_effect=FileNotFoundError("missing"))),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.server",
        MagicMock(create_app=MagicMock()),
    )
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["--config", "/nope/does/not/exist.yaml"])
    assert exc.value.code == 1


def test_default_path_log_level_passed_lowercase_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config()
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.config",
        MagicMock(load_config=MagicMock(return_value=cfg)),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "bastion.server",
        MagicMock(create_app=MagicMock(return_value=object())),
    )
    with patch("bastion.__main__.uvicorn.run") as uv:
        _run_cli(monkeypatch, ["--log-level", "DEBUG"])
    assert uv.call_args.kwargs["log_level"] == "debug"


# ---------------------------------------------------------------------------
# --- security banner branches
# ---------------------------------------------------------------------------


def test_security_banner_silent_on_localhost_bind() -> None:
    """No warnings when binding to 127.0.0.1, regardless of auth state."""
    cfg = _make_config(host="127.0.0.1", auth_enabled=False)
    assert cli._security_banner_lines(cfg) == []


def test_security_banner_warns_on_public_bind_without_auth() -> None:
    """0.0.0.0 + auth disabled triggers SECURITY WARNING banner."""
    cfg = _make_config(host="0.0.0.0", auth_enabled=False)
    lines = cli._security_banner_lines(cfg)
    assert any("SECURITY WARNING" in line for line in lines)
    assert any("auth is disabled" in line for line in lines)


def test_security_banner_warns_when_auth_enabled_but_no_keys() -> None:
    """auth.enabled=true but empty api_keys = OPEN proxy."""
    cfg = _make_config(host="0.0.0.0", auth_enabled=True, api_keys=[])
    lines = cli._security_banner_lines(cfg)
    assert any("OPEN" in line for line in lines)


def test_security_banner_silent_when_auth_keys_present() -> None:
    """Properly-configured auth produces no Check-1 banner."""
    cfg = _make_config(
        host="0.0.0.0",
        auth_enabled=True,
        api_keys=["secret"],
        rate_limit_enabled=True,
    )
    lines = cli._security_banner_lines(cfg)
    assert not any("SECURITY WARNING" in line for line in lines)


def test_security_banner_flags_open_a2a() -> None:
    """A2A enabled with no tokens on public bind emits A2A-specific warning."""
    cfg = _make_config(
        host="0.0.0.0",
        auth_enabled=True,
        api_keys=["k"],
        a2a_enabled=True,
        a2a_tokens=[],
    )
    lines = cli._security_banner_lines(cfg)
    assert any("/a2a/" in line for line in lines)


def test_security_banner_flags_disabled_rate_limit() -> None:
    """Rate limiting off on a public bind emits its own warning."""
    cfg = _make_config(
        host="0.0.0.0",
        auth_enabled=True,
        api_keys=["k"],
        rate_limit_enabled=False,
    )
    lines = cli._security_banner_lines(cfg)
    assert any("Rate limiting" in line for line in lines)


# ---------------------------------------------------------------------------
# --- _confirm_continue helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply,expected", [
    ("", True),
    ("y", True),
    ("Y", True),
    ("yes", True),
    ("n", False),
    ("no", False),
    ("anything else", False),
])
def test_confirm_continue_parses_replies(
    monkeypatch: pytest.MonkeyPatch, reply: str, expected: bool
) -> None:
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: reply)
    assert cli._confirm_continue() is expected


def test_confirm_continue_returns_false_on_ctrl_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: Any, **_kw: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise)
    assert cli._confirm_continue() is False


# ---------------------------------------------------------------------------
# --- stress test Ctrl+C-during-run recovery branch
# ---------------------------------------------------------------------------


def test_stress_test_ctrl_c_during_run_triggers_recovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If asyncio.run(_run_stress_test) raises KeyboardInterrupt, recovery_phase runs."""
    recovery = MagicMock(return_value=MagicMock())
    fake_stress = MagicMock(
        SAFETY_BANNER="",
        StressConfig=MagicMock(return_value=MagicMock(bastion_url="http://x")),
        recovery_phase=recovery,
    )
    monkeypatch.setitem(__import__("sys").modules, "bastion.stress", fake_stress)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "I understand")
    monkeypatch.setattr(cli, "_run_stress_test", MagicMock(return_value=MagicMock()))

    calls: list[Any] = []

    def fake_run(coro: Any) -> None:
        calls.append(coro)
        if len(calls) == 1:  # first asyncio.run (the stress test) raises
            raise KeyboardInterrupt

    monkeypatch.setattr("asyncio.run", fake_run)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["--stress-test"])
    assert exc.value.code == 0
    assert recovery.called
    out = capsys.readouterr().out
    assert "Recovery complete" in out


# ---------------------------------------------------------------------------
# --- two-port mode runtime (_run_two_port)
# ---------------------------------------------------------------------------


def test_run_two_port_wires_proxy_and_admin_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_two_port creates two uvicorn servers and awaits them in parallel."""
    import asyncio

    cfg = _make_config(host="127.0.0.1", port=11434, admin_port=9999)

    proxy_app = object()
    admin_app = object()
    server_mod = MagicMock(
        create_proxy_app=MagicMock(return_value=proxy_app),
        create_admin_app=MagicMock(return_value=admin_app),
    )
    watchdog_mod = MagicMock(notify_stopping=MagicMock())
    monkeypatch.setitem(__import__("sys").modules, "bastion.server", server_mod)
    monkeypatch.setitem(__import__("sys").modules, "bastion.watchdog", watchdog_mod)

    fake_proxy_server = MagicMock()
    fake_admin_server = MagicMock()

    async def _idle() -> None:
        return None

    fake_proxy_server.serve = MagicMock(return_value=_idle())
    fake_admin_server.serve = MagicMock(return_value=_idle())

    server_ctor = MagicMock(side_effect=[fake_proxy_server, fake_admin_server])
    config_ctor = MagicMock()
    monkeypatch.setattr("bastion.__main__.uvicorn.Server", server_ctor)
    monkeypatch.setattr("bastion.__main__.uvicorn.Config", config_ctor)

    asyncio.run(cli._run_two_port(cfg, "127.0.0.1", 11434, 9999, "info"))

    server_mod.create_proxy_app.assert_called_once_with(cfg)
    server_mod.create_admin_app.assert_called_once_with(cfg)
    assert config_ctor.call_count == 2
    assert server_ctor.call_count == 2


# ---------------------------------------------------------------------------
# --- _run_stress_test: early-exit branches
# ---------------------------------------------------------------------------


def _install_stress_module_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prereq_ok: bool = True,
    phase1_success: bool = True,
    phase2_success: bool = True,
    confirms: tuple[bool, bool, bool] = (True, True, True),
) -> dict[str, MagicMock]:
    """Wire up the heavy modules touched by _run_stress_test."""
    import asyncio as _asyncio

    def _async_returning(value: Any) -> MagicMock:
        """Return a MagicMock whose call yields a fresh coroutine each time."""
        async def _coro(*_a: Any, **_kw: Any) -> Any:
            return value
        return MagicMock(side_effect=lambda *a, **kw: _coro(*a, **kw))

    def _phase(success: bool, data: dict[str, Any] | None = None, error: str = "") -> MagicMock:
        m = MagicMock()
        m.success = success
        m.data = data or {}
        m.error = error
        return m

    phase1 = _phase(
        phase1_success,
        {"idle_temp_c": 40, "idle_power_w": 30, "vram_in_use_mb": 100},
        "p1-fail",
    )
    phase2 = _phase(
        phase2_success,
        {"inference_latency_s": 1.0, "thermal_delta_c": 5, "peak_vram_mb": 4000},
        "p2-fail",
    )
    phase3 = _phase(True, {
        "safe_swap_rate_per_min": 3,
        "stop_reason": "ok",
        "swap_duration_avg_s": 2.0,
    })
    phase4 = _phase(True, {"max_concurrent_requests": 2, "stop_reason": "ok"})
    phase5 = _phase(True, {"cooldown_duration_s": 3, "final_temp_c": 41})

    stress_mod = MagicMock(
        CalibrationResult=MagicMock(side_effect=lambda **kw: MagicMock(
            phases=[], calibrated={}, **kw,
        )),
        check_prerequisites=_async_returning((prereq_ok, "ok")),
        baseline_phase=_async_returning(phase1),
        single_load_phase=_async_returning(phase2),
        swap_ramp_phase=_async_returning(phase3),
        concurrent_load_phase=_async_returning(phase4),
        recovery_phase=_async_returning(phase5),
        write_profile=MagicMock(return_value="/tmp/profile.yaml"),
    )
    gpu_profiles_mod = MagicMock(
        lookup_profile=MagicMock(return_value=MagicMock(
            vram_total_mb=32000, vram_headroom_mb=6000, thermal_ceiling_c=82,
        )),
    )
    validate_mod = MagicMock(
        _query_gpu_name=MagicMock(return_value="RTX 5090"),
        _query_driver_version=MagicMock(return_value="555.0"),
    )
    health_mod = MagicMock(
        query_gpu_status=_async_returning(MagicMock(vram_total_mb=32000)),
    )

    # httpx.AsyncClient stub returning a small-model list.
    class _Resp:
        def json(self) -> dict[str, Any]:
            return {"models": [{"name": "tiny:1b", "size": 1_000_000}]}

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def get(self, *_a: Any, **_kw: Any) -> _Resp:
            return _Resp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "AsyncClient", _Client)

    monkeypatch.setitem(__import__("sys").modules, "bastion.stress", stress_mod)
    monkeypatch.setitem(__import__("sys").modules, "bastion.gpu_profiles", gpu_profiles_mod)
    monkeypatch.setitem(__import__("sys").modules, "bastion.validate", validate_mod)
    monkeypatch.setitem(__import__("sys").modules, "bastion.health", health_mod)

    confirm_iter = iter(confirms)
    monkeypatch.setattr(cli, "_confirm_continue", lambda: next(confirm_iter, True))

    return {
        "stress": stress_mod,
        "asyncio": _asyncio,
    }


def test_run_stress_test_aborts_when_prerequisites_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failed prerequisites short-circuit the run before phase 1."""
    stubs = _install_stress_module_stubs(monkeypatch, prereq_ok=False)
    stress_config = MagicMock(
        bastion_url="http://x",
        baseline_duration_s=1.0,
        sample_interval_s=0.5,
    )
    stubs["asyncio"].run(cli._run_stress_test(stress_config))
    assert "FAILED" in capsys.readouterr().out
    stubs["stress"].baseline_phase.assert_not_called()


def test_run_stress_test_recovers_when_phase2_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed single-load phase calls recovery and returns."""
    stubs = _install_stress_module_stubs(monkeypatch, phase2_success=False)
    stress_config = MagicMock(
        bastion_url="http://x",
        baseline_duration_s=1.0,
        sample_interval_s=0.5,
    )
    stubs["asyncio"].run(cli._run_stress_test(stress_config))
    out = capsys.readouterr().out
    assert "FAILED" in out
    stubs["stress"].recovery_phase.assert_called()
    stubs["stress"].swap_ramp_phase.assert_not_called()


def test_run_stress_test_happy_path_writes_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """All phases succeed → write_profile is invoked."""
    stubs = _install_stress_module_stubs(monkeypatch)
    stress_config = MagicMock(
        bastion_url="http://x",
        baseline_duration_s=1.0,
        sample_interval_s=0.5,
    )
    stubs["asyncio"].run(cli._run_stress_test(stress_config))
    stubs["stress"].write_profile.assert_called_once()
    assert "Profile written to" in capsys.readouterr().out
