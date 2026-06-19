# ADR-007: MCP adapter tool schema versioning — pin via tool-name suffix, deprecation in adapter

**Status:** Accepted (gates `bastion/mcp_adapter.py` v0.5 ship)
**Date:** 2026-05-19
**Deciders:** S122 maintainer with reference to S122 plan-C design review
**Related:** ADR-006 (auth), ADR-005 (BastionPanel contract); S122 plan-C vision-council retro Step 3 (internal artifact, archived)

## Context

The S122 plan-C council recommends Vision D — `bastion/mcp_adapter.py` exposing `/broker/*` and `/broker/control/*` as MCP tools — as v0.5's third step (after endpoint completeness and auth). The synthesizer lens framed it sharply:

> *"Any AI client becomes a first-class dashboard with no UI build cost. Vision A emerges as AI-client behavior — no `policy.py` module required; autonomous decisions are AI-client-initiated tool calls with the operator confirmation prompt as the veto."*

Minimum tool manifest from the council:

```
broker_status, broker_latency, broker_catalog, broker_intents,
broker_counters, broker_restart, broker_reset_epoch
```

The broker HTTP API evolves. `/broker/status` already added `total_dispatched`, `swap_rate_level`, `stall_*` fields between v0.3 and v0.4. `/broker/counters` shipped `reset_epoch` in WT-C-A-05 (commit `03968ad`). Future endpoints will land. MCP tool schemas describe the wire shape of these endpoints back to AI clients. The question: **when the broker API changes, how do MCP clients know?**

Three credible mechanisms:

**(a) Tool-name suffix.** `broker_status_v1`, `broker_status_v2`. AI clients ask for `_v1`; adapter routes to the v1 schema. New version = new tool name; old tool kept alive with translation shim.

**(b) Schema content versioning.** Single tool `broker_status` whose JSON Schema includes a `version` field. AI clients introspect and adapt. Adapter exposes one tool but with internal version logic.

**(c) Adapter-version envelope.** Single tool `broker_status` returns `{adapter_version, payload}`. Client switches on the envelope.

The MCP spec (per Claude Code's plugin schema) supports JSON Schema for tool input/output and ALLOWS multiple tools with related names. The spec does NOT mandate a versioning convention.

Two real failure modes shape the decision:

1. **Schema field rename:** `swap_rate_level` becomes `swap_rate_category`. Existing AI clients break silently if they string-match the value but break loudly if they extract a field by name. Either way, the client author needs a signal.

2. **Endpoint deletion:** Hypothetical retirement of `/broker/recent` in favor of `/broker/latency` (which doesn't exist yet — WT-C-A-06). MCP clients still pointing at `broker_recent` should fail loudly, not silently.

## Decision

**v0.5 adopts pattern (a): tool-name suffix versioning. Specifically: `broker_<name>_v<N>` for every tool. Old versions kept in the adapter for two minor releases past deprecation, then removed in the next major release.**

Specifically:

1. **Initial tool set ships at `_v1`.** All seven tools: `broker_status_v1`, `broker_latency_v1`, `broker_catalog_v1`, `broker_intents_v1`, `broker_counters_v1`, `broker_restart_v1`, `broker_reset_epoch_v1`.

2. **No unversioned alias.** No `broker_status` without a `_v<N>` suffix. AI clients MUST commit to a version. Anti-pattern explicitly chosen: there is no "latest" alias because "latest" is exactly the silent-breakage path.

3. **JSON Schema in tool definition is the source of truth.** Each `_v1` tool ships a complete JSON Schema for its output. Schema lives alongside the adapter at `src/bastion/mcp_adapter/schemas/broker_status_v1.json` etc. Adapter validates the broker's HTTP response against the schema before returning to the client; mismatch = adapter-side error (loud, with a clear "broker API drift" message).

4. **Versioning trigger: ANY breaking change.** A breaking change is: field removal, field rename, type change, semantic change (same field, different meaning), or required-vs-optional flip. Adding new optional fields is NOT breaking — old clients ignore the new field, schema validation passes if `additionalProperties: true` in v1.

5. **Lifecycle: v(N) lives 2 minor releases past v(N+1) ship. v(N) removed in the next major release.** Concretely: if `broker_status_v2` ships in v0.6, `broker_status_v1` stays available through v0.7 and is removed in v1.0. Removal noted in CHANGELOG; adapter raises a clear "tool removed; migrate to v(N+1)" error when called.

6. **Adapter handles translation, not the broker.** When `broker_status_v2` exists, the broker HTTP API returns the latest schema. The adapter's `_v1` shim wraps the latest broker response and converts it to v1 shape. Broker stays single-version-of-truth; adapter is the translation layer.

7. **Per-tool versions, not per-adapter version.** Different tools can be at different versions (`broker_status_v2` and `broker_counters_v1` coexist). This avoids the "must bump everything in lockstep" pressure that causes versioning to skip ahead silently.

## Consequences

**Accepted:**

- AI clients explicitly choose a version; no silent schema drift.
- Adapter codebase grows by one translation shim per breaking change per tool. Estimated ~50 lines per shim.
- CHANGELOG entries for adapter tool changes are mandatory; lint check verifies one entry per new `_v<N>` directory.
- MCP tool count grows over time. After three breaking changes per tool, the manifest has ~21 entries. Acceptable — better than schema drift.
- Schema files in `src/bastion/mcp_adapter/schemas/` are committed; CI validates the broker's actual response against the schema for each version on every PR.

**Rejected risk:**

- Tool-name proliferation is acceptable. The mitigation is the lifecycle (v(N) removed at next major). MCP clients enumerate tools at session start; they discover what's available.
- "Latest alias" is explicitly rejected. Ergonomic friction is the point — it forces the client author to commit.

**Gating event for revisiting (ADR-007-B):**

This ADR is reopened when any of:

1. The adapter codebase carries >5 shim layers per tool. The lifecycle policy is failing.
2. MCP clients in practice ignore versioning and probe for capabilities dynamically (a signal that the versioning model is wrong for the protocol's actual usage pattern).
3. The MCP spec gains a first-class versioning primitive (subsume our convention into the standard).

## Alternatives Considered

**Schema-content versioning (b — rejected).** Single tool with internal `version` field. Issue: AI clients must introspect schema on every call to decide which fields to read. Wire shape is more compact but client logic is more complex. Schema-validation as a static contract is more debuggable than runtime-version-dispatch.

**Adapter-version envelope (c — rejected).** Single tool returning `{adapter_version, payload}`. Issue: payload shape varies per adapter-version, which means the tool's JSON Schema can't pin payload shape. Schema becomes effectively `additionalProperties: true` and version-discrimination moves to client code. Worst of both worlds.

**Schema-only versioning via JSON Schema `$id` (rejected).** Use JSON Schema `$id: "https://bastion.dev/schemas/broker_status/v1"`. Issue: MCP clients don't necessarily dereference `$id`; the version is invisible at the tool-listing layer. Schema-internal versioning fails the discoverability test.

**Semver everywhere (`broker_status_1.0.0`) (rejected).** Major.minor.patch in tool names is more granularity than needed and creates fragmentation. v1, v2 is the right denominator for **breaking** changes only.

## Implementation Notes

Code surfaces (v0.5):

- `src/bastion/mcp_adapter/__init__.py` — main MCP server bootstrap. Reads `BROKER_URL` env (default `http://127.0.0.1:11434`) and bearer token via ADR-006 mechanism.
- `src/bastion/mcp_adapter/tools/broker_status_v1.py` — one file per tool version. Each file: tool definition (MCP), JSON Schema, async handler.
- `src/bastion/mcp_adapter/schemas/broker_status_v1.json` — JSON Schema files committed to repo.
- `tests/mcp_adapter/test_*.py` — for each tool: roundtrip against a fake broker; schema validation; missing-broker error path; auth failure path.

Adapter packaging: separate optional install `bastion[mcp]` so the base broker install doesn't pull `mcp` SDK. The adapter is a not-yet-shipped v0.5 proposal, to be documented in `docs/getting-started.md` when the adapter ships.

Adapter startup: refuses to start without a bearer token (per ADR-006). Loopback-only by default; non-loopback bind requires auth-gated.

Tool listing example (v0.5 ship target):

```python
tools = [
    BrokerStatusV1Tool(),
    BrokerLatencyV1Tool(),
    BrokerCatalogV1Tool(),
    BrokerIntentsV1Tool(),
    BrokerCountersV1Tool(),
    BrokerRestartV1Tool(),
    BrokerResetEpochV1Tool(),
]
```

Naming: `broker_<resource>_v<N>` snake-case. Tool description always carries `(v<N>)` suffix in the human-readable description for client UI clarity.

## References

- S122 plan-C council Step 3 — MCP tool manifest.
- S122 plan-C vision-council retro Step 3 (internal artifact, archived).
- ADR-006 (auth) — adapter uses the same bearer token; no separate auth surface.
- MCP spec — tool definition format (JSON Schema for input/output, name uniqueness).
