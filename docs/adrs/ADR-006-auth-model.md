# ADR-006: Auth model — token-based with loopback exemption, mTLS deferred

**Status:** Accepted (v0.5 milestone gate)
**Date:** 2026-05-19
**Deciders:** S122 maintainer with reference to S122 plan-C design review
**Related:** S122 plan-C vision-council retro Step 2 + dissent log (security-deployment-reviewer) (internal artifact, archived); `docs/security.md`; planned `bastion/mcp_adapter.py` (ADR-007)

## Context

The S122 plan-C design review (2026-05-19) declared auth-on-by-default a **v0.5 milestone gate** blocking ALL subsequent networked-surface work. The security-deployment-reviewer lens went further: *"Before any network-exposed control surface ships, [the sudo] path must be replaced with a systemd unit — no shell, no sudo, no ambient capability."* The parallel-merge lens called per-surface auth a "ship-trap under single-dev velocity."

BASTION today:

- `make_admin_key_dependency`/`make_a2a_token_dependency` in `src/bastion/auth.py` produce the `verify_admin`/`verify_a2a` dependencies wired into the FastAPI routers in `server.py` via `Depends(...)`.
- The default config disables auth (`auth.enabled: false`). Operators must explicitly enable it.
- `action_service_restart` (`dashboard/app.py:997`) shells out to `sudo -n systemctl restart bastion.service` — bypassing the auth layer entirely.
- Vision D (MCP adapter, planned per S122 council Step 3) will expose `/broker/control/*` over an MCP transport that may or may not run on localhost.

The decision: what auth model gates v0.5? Three credible options:

**(a) Token-based with loopback exemption (default).** A signed bearer token issued at first run, written to `~/.config/bastion/token` (0600). Requests to `127.0.0.1`/`::1` are exempt unless `--require-auth-loopback` is set. Non-loopback binds (`--bind 0.0.0.0`) require the token and refuse to start without one.

**(b) mTLS.** Mutual-TLS with broker-issued client certificates. Highest security; highest operator-friction. Justified if BASTION ships as a team service. Overkill for the single-operator workstation target.

**(c) OAuth/OIDC delegation.** Defer to an external IdP. Justified if BASTION integrates into a corporate auth fabric. Adds an external dependency in BASTION's most basic install path.

The council's adversarial lens warned that Vision A (autonomous policy) introduces "prompt-injection via state data" — externally-writable inputs (queue depths, error strings, model names) can influence policy outputs. Auth boundaries matter because every networked surface that bypasses them adds attack surface.

## Decision

**v0.5 ships option (a): token-based with loopback exemption.**

Concretely:

1. **First-run token issuance.** `bastion init` (new subcommand) generates a 256-bit random token, writes to `${XDG_CONFIG_HOME:-~/.config}/bastion/token` mode 0600, owned by the invoking user. If the file exists, `init` is a no-op unless `--rotate` is passed.

2. **Default bind: `127.0.0.1` only.** Existing single-port and admin-port modes default to loopback. Loopback requests are exempt from auth UNLESS the operator passes `--require-auth-loopback` (intended for shared-host installs).

3. **Non-loopback bind requires auth.** `bastion serve --bind 0.0.0.0` (or any non-loopback address) refuses to start if `auth.enabled: false`. The error is fatal — not a warning — and includes the exact remediation: *"Set auth.enabled: true in broker.yaml, then verify ~/.config/bastion/token exists, then re-launch."*

4. **Token presented as `Authorization: Bearer <token>`.** The existing `verify_admin`/`verify_a2a` helpers gain a third path (after FastAPI's existing query-param + cookie fallbacks): inspect the `Authorization` header and accept a token whose constant-time-compared digest matches the on-disk token's digest.

5. **Call-7 systemd unit replaces sudo path.** Drop the `sudo -n systemctl restart` shell-out. The replacement: a `bastion-restart.service` systemd unit that the running broker can invoke via the system-bus DBus interface, OR an in-process `os.execvp(sys.argv)` self-restart triggered by a signed control request on `POST /broker/control/restart`. Selection between the two is a v0.5 implementation decision; either is acceptable; the sudo path is not.

6. **Rate limit on auth failures.** `verify_admin` already returns 401 on bad tokens; add a per-IP rolling-window counter that escalates to 30s lockout after 5 consecutive failures. Operators can disable via `auth.lockout.enabled: false`.

7. **mTLS deferral.** mTLS is not v0.5 scope. ADR-006-B (future) will revisit if/when BASTION ships as a multi-operator team service.

## Consequences

**Accepted:**

- Single-operator workstation installs see no auth friction — loopback exemption preserves the current "double-click and go" UX.
- Non-loopback exposure becomes a deliberate, auth-gated decision rather than a default.
- Call-7 systemd-unit replacement closes the "sudo for restart" hole permanently. The dashboard no longer needs sudo grants.
- MCP adapter (ADR-007) authenticates with the same token; no parallel auth scheme to maintain.
- `bastion init` becomes the canonical first-run command; documented in `docs/getting-started.md` and `docs/security.md`.

**Rejected risk:**

- Loopback exemption is NOT a back-door for shared-host installs. The decision explicitly empowers operators on shared hosts via `--require-auth-loopback`. This is a deliberate trade-off: the workstation case is overwhelmingly more common; the shared-host case must opt in.
- mTLS deferral is NOT a permanent rejection. The gating event below specifies revisit conditions.

**Gating event for revisiting (ADR-006-B):**

This ADR is reopened — and mTLS or OIDC re-evaluated — when any of:

1. A second BASTION operator is added to the same host (RBAC requirement emerges).
2. The MCP adapter is hosted on a non-loopback bind in production (token model insufficient).
3. The token model is shown to be compromised in a security incident (audit log analysis would surface this).

## Alternatives Considered

**mTLS now (rejected — overkill for the target).** Generates broker-CA + per-client certificate provisioning friction that does not benefit the single-operator case. The work pays for itself only when there are 3+ operators or a network exposure that mTLS specifically addresses. Today's single-operator workstation is neither.

**No-auth + firewall (rejected — defense-in-depth violation).** "Just rely on the firewall and bind to 127.0.0.1" is the status quo and is exactly what the council rejected. Single-bind-flag-flip from secure to insecure is too fragile a control.

**Per-endpoint auth selection (rejected — ship-trap per council).** "Auth on /control/* only" splits the auth model into a per-surface decision. Council lens parallel-merge: *"Per-surface auth is a trap: single-dev velocity under deadline pressure produces 'I'll add auth next release' and it doesn't ship."*

**OAuth/OIDC delegation now (rejected — corporate-fabric dependency).** Adds a dependency on an external IdP that ~zero workstation users want. Deferred to ADR-006-B alongside mTLS.

## Implementation Notes

Code surfaces touched in v0.5:

- `src/bastion/auth.py` — extend `verify_admin`/`verify_a2a` to read `Authorization: Bearer` first.
- `src/bastion/__main__.py` — add `init` subcommand; refuse non-loopback bind without auth.
- `src/bastion/paths.py` — new helper `token_path()` returning `${XDG_CONFIG_HOME:-~/.config}/bastion/token`.
- `src/bastion/dashboard/app.py:997` — `action_service_restart` rewritten to POST `/broker/control/restart` (or invoke DBus, depending on systemd implementation). Sudo path deleted.
- `src/bastion/server.py` — new `POST /broker/control/restart` endpoint (Call-7 from S121 design review); admin-auth-gated; auditable; returns `202 Accepted`.
- `config/broker.yaml` — `auth.enabled: false` default unchanged; new `auth.token_path:` defaults to `${XDG_CONFIG_HOME:-~/.config}/bastion/token`; new `auth.lockout:` block.
- `docs/security.md` — full rewrite of auth section to reflect new defaults.
- `docs/getting-started.md` — `bastion init` documented as first-run step (but not required on loopback).
- `tests/` — cover: first-run init creates token, loopback-no-token-OK, non-loopback-no-auth-refuses-start, bearer-token-roundtrip, lockout-after-5-failures, --require-auth-loopback enforcement.

Token format: `bst_` prefix + 256-bit base64url (no padding). Length 47 chars total. Constant-time compare via `hmac.compare_digest`.

Storage: file mode 0600 enforced on read (if other modes, refuse to start with a clear error). Owner check (if file owner != `os.geteuid()`, refuse). Never read from environment variables or command-line args — file-only.

## References

- S122 plan-C council FINAL_RECOMMENDATION.md Step 2 — auth-as-milestone-gate framing.
- security-deployment-reviewer lens dissent — sudo path = "load-bearing debt."
- parallel-merge-safety-engineer lens — per-surface auth as ship-trap.
- adversarial-failure-mode-auditor lens — prompt-injection via externally-writable broker state.
- `tmp_S121_PLAN.md` §1 finding F2 → adjacent (broker.yaml hygiene reminder that config defaults matter).
