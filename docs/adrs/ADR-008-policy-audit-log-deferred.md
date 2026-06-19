# ADR-008: Policy decision audit log — DEFERRED until Vision A is greenlit

**Status:** Proposed → Deferred (no v0.5 implementation)
**Date:** 2026-05-19
**Deciders:** S122 maintainer with reference to S122 plan-C design review
**Related:** ADR-007 (MCP adapter — Vision A "emerges as AI-client behavior"); S122 plan-C vision-council retro Step 3 + adversarial-failure-mode-auditor dissent (internal artifact, archived)

## Context

The S121 design review (2026-05-14) queued ADR-BASTION-03 for "policy decision audit log format — reuse audit_event or new subtype." This anticipated **Vision A — Autonomous Self-Healing Broker** where a `policy.py` module emits decisions like *"queue_depth > 50 → restart qwen3:30b"* with one-line justifications written to an audit log.

The S122 plan-C council (2026-05-19) **explicitly punted Vision A as a designed module**:

> *"Vision D MCP adapter exposes broker tools to AI clients; Vision A emerges as AI-client behavior — no `policy.py` module required; autonomous decisions are AI-client-initiated tool calls with the operator confirmation prompt as the veto."*

The adversarial-failure-mode-auditor lens (council 2026-05-19) reinforced:

> *"Vision A compounds [the watchdog blindspot] maximally — `policy.py` calls the local LLM to decide whether to restart the local LLM. Bootstrap failure is guaranteed during the exact incident class BASTION exists to prevent."*

Therefore: there is no `policy.py` to audit in v0.5. Policy decisions become MCP tool calls audited via the existing audit_event subsystem under their inherent tool-call shape (caller identity, tool name, arguments, outcome).

The pre-existing audit subsystem at `src/bastion/audit.py` already captures:

- Tool/control invocations (preload, unload, drain, resume, restart)
- Caller identity (via FastAPI dependency injection — currently a placeholder)
- Outcome (success / failure / timeout)
- Timestamp + duration

This shape already covers the MCP adapter's autonomous-decision use case. Each AI-client-initiated tool call IS a policy decision; its audit entry IS the policy audit log.

## Decision

**No new policy-decision audit subtype is introduced in v0.5. The existing `audit_event` schema covers AI-client-initiated tool calls without modification.**

Concretely:

1. **MCP-initiated tool calls audit as `audit_event` with `source: "mcp_adapter"`.** A new optional field `source` on existing `audit_event` distinguishes operator-CLI, dashboard-TUI, and MCP-client origins. This is an additive change, not a new event type.

2. **No `policy_decision` subtype.** The audit pipeline does not need to discriminate "policy" from "operator action" — both are signed authoritative requests reaching the broker. The distinction lives in the `source` field if a consumer cares.

3. **`mcp_caller_id`** — when ADR-006's bearer token has a `client_name` claim (future enhancement to the token format), the audit entry includes the client name. For v0.5 the token is opaque; `mcp_caller_id` is the truncated token hash (8 chars). Sufficient to distinguish multiple AI clients in audit logs.

4. **This ADR is reopened ONLY if Vision A is greenlit as a designed module.** The gating events are listed below.

## Consequences

**Accepted:**

- No `policy.py` module is built in v0.5. The council's recommendation (Vision A as AI-client behavior) is honored without additional infrastructure.
- The audit subsystem gains one optional `source` field and one optional `mcp_caller_id` field. Backwards-compatible; existing audit consumers ignore unknown fields.
- A Vision-A-style "decision log" UI is implementable today by filtering `audit_event` where `source == "mcp_adapter"` — no new event type needed.
- Operators wanting "show me what the AI client decided this hour" would run a planned `bastion audit --source mcp_adapter --since 1h` subcommand (proposed for v0.5; not yet implemented).

**Rejected risk:**

- Not maintaining a distinct policy-decision schema is NOT a security regression. The existing `audit_event` already records all the relevant fields. The risk would be schema drift if a hypothetical `policy.py` later needed extra fields — but that's exactly the gating event below.

**Gating event for revisiting (ADR-008-B):**

This ADR is reopened — and a `policy_decision` schema designed — when any of:

1. A `policy.py` module is added to BASTION as a designed first-class module (council reopens Vision A as designed-module rather than emergent-behavior).
2. An AI-client integration in practice requires a richer "decision context" than tool-call audit entries provide (e.g., the AI client wants to record its reasoning trace, not just the tool call).
3. Compliance/audit requirements emerge (e.g., the broker ships into an environment requiring SOC2-style decision attestation).

If any of (1), (2), (3) hold, draft **ADR-008-B** with the policy-decision schema.

## Alternatives Considered

**New `policy_decision` event subtype now (rejected — premature).** Council picked Vision A as emergent behavior, not designed module. Defining a schema for a module we are not building violates the project's "no premature abstraction" rule.

**Repurpose `audit_event.type` (rejected — semantic drift).** Adding `type: "policy"` as a new enum value would let consumers filter, but the event content is identical to a tool-call audit. The `source` field is the right axis of variation, not the `type` field.

**Separate audit stream for MCP (rejected — duplicate plumbing).** A `mcp_audit_event` parallel pipeline adds infrastructure for zero new behavior. Single audit_event with `source: "mcp_adapter"` filtering is sufficient.

## Implementation Notes

Code changes in v0.5:

- `src/bastion/audit.py` — add optional `source: str` and `mcp_caller_id: str` fields to `audit_event`. Existing constructors continue to work; new fields default to `None`.
- `src/bastion/mcp_adapter/__init__.py` — when handling a tool call, write an `audit_event` with `source="mcp_adapter"` before invoking the broker HTTP API and again after (success/failure).
- `src/bastion/__main__.py` — a planned `bastion audit` subcommand gains a `--source` filter.
- Test: ADR-006-style flow (init token → MCP client → tool call) produces exactly one audit entry with the expected source.

No schema versioning needed since the change is purely additive.

## References

- S122 plan-C council FINAL_RECOMMENDATION Step 3 — Vision A as AI-client behavior.
- S122 council adversarial dissent — Vision A bootstrap failure analysis.
- ADR-007 — MCP tool versioning (sister ADR; defines the tool-call source this ADR audits).
- ADR-006 — auth (defines `mcp_caller_id` provenance).
- `src/bastion/audit.py` — existing audit event subsystem.
