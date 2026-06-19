# ADR-005-B: MCP `broker_snapshot_v1` as the third operational surface — deferral of the subscriber/pub-sub bus

**Status:** Accepted (deferral record; v0.5/v0.6 window — supersedes nothing, extends ADR-005)
**Date:** 2026-06-19
**Deciders:** Observability-expansion implementer with reference to the 2026-06-19 observatory spec (rev. 3) and the ADR-005 gating clause
**Related:** ADR-005 (BastionPanel direct-accessor contract — the gating clause this ADR answers), ADR-007 (MCP tool schema versioning — the `_v<N>` convention `broker_snapshot_v1` follows), ADR-009 (TUI deprecation trigger — revisit trigger #3 aligns with ADR-005 gating event #1), `docs/design/specs/2026-06-19-observability-expansion.md` §5.6 + §9

## Context

ADR-005 ("BastionPanel data contract — subscriber vs. direct accessor", 2026-05-14) ratified the **direct-accessor** pattern for TUI panels and deferred the in-process **subscriber/pub-sub** pattern (option *b*). It named three gating events that, if any occurred, would reopen the ADR and require drafting **ADR-005-B**. The first of those is verbatim:

> **Gating event #1.** "A third operational surface (beyond TUI and Grafana) is greenlit for the broker. At that point the per-surface accessor cost is paid 3×; (b) becomes worth its upfront cost."

The 2026-06-19 observability-expansion spec catalogues an MCP tool, **`broker_snapshot_v1`** (Tier 3, Phase 4 — spec §5.6), which exposes the broker's correlated `MachineSnapshot` to any MCP-speaking AI client in a single call (versus the 5+ separate `/broker/*` calls an agent makes today). That tool, when it ships, **is** "a third operational surface beyond TUI and Grafana." Per the spec's §9 governance note, shipping it is therefore **ADR-005 gating event #1**, and the trigger "must not be left in an implicitly-fired, unrecorded state." This document is that record.

Two facts shape the decision and keep it from being a literal mandate to build the bus now:

1. **The MCP surface consumes the HTTP snapshot endpoint, not an in-process bus.** `broker_snapshot_v1` is a thin adapter that wraps `GET /broker/snapshot` (the endpoint shipped in Phases 1–3 and registered in both `create_app` and `create_admin_app`). It receives state by polling/requesting that endpoint exactly as Grafana scrapes `/metrics` and the TUI polls `/broker/status`. It imposes **no** subscriber requirement on any panel and reaches into **no** broker internal. The "third surface" is real at the *product* level (a new way operators/agents consume broker state) but at the *data-contract* level it is the same request/response shape ADR-005 already blesses.

2. **The tool is blocked and not built here.** `broker_snapshot_v1` depends on the **`mcp_adapter` package (v0.5, ADR-007)**, which does not yet exist in this tree. The observability expansion ships **only the HTTP endpoint the adapter will wrap**; it ships **no** `mcp_adapter/tools/broker_snapshot_v1.py`, **no** `schemas/broker_snapshot_v1.json`, and **no** stub of either. The gating event therefore has not *actually* fired yet — the spec is explicit that "the trigger is the adapter shipping (Phase 4), not this spec." This ADR is written ahead of that ship so the trigger is governed, not discovered after the fact.

The companion external-surface item, SSE `/broker/snapshot/stream` (also Phase 4, also shipped 501-disabled in this branch), is a FastAPI `StreamingResponse` — an **external** push surface, **not** an in-process pub/sub. Per spec §9 it is distinct from ADR-005's option *b* and **does not** trip this gating event. It is mentioned here only to record that it was considered and excluded from the trigger.

## Decision

**Record that MCP `broker_snapshot_v1` is ADR-005 gating event #1, and keep deferring the subscriber/pub-sub bus. The direct-accessor contract (ADR-005 option *a*) stands.** Concretely:

1. **The trigger is acknowledged, not yet fired.** `broker_snapshot_v1` *would be* the third operational surface. Because the tool is blocked on `mcp_adapter` v0.5 and is not built in the observability expansion, the trigger is **pending**, and this ADR is the pre-positioned record that fires with it.
2. **The subscriber/pub-sub bus remains deferred.** No in-process observer/`asyncio.Queue` bus is introduced. Panels keep `render_data(data: dict)`. The new observatory panels (`ContentionPanel`, `ProcessAttributionPanel`, `CorrelationPanel`) are `BastionPanel` subclasses that accept the snapshot dict, consistent with ADR-005.
3. **MCP is blocked on `mcp_adapter` v0.5 (ADR-007).** The endpoint ships first; the tool wraps it later under the `_v<N>` suffix + committed JSON Schema + adapter-side validation convention ADR-007 mandates.

### Why the bus stays deferred even though the surface count reaches three

The ADR-005 cost/value argument is re-evaluated against the *actual* shape of the third surface, and the deferral holds for three reasons (mirroring spec §9):

- **(a) The chosen architecture is still not event-driven.** Vision E (event-driven policy) is not the selected architecture; building an in-process bus optimizes for a vision not picked — the precise "premature abstraction" ADR-005 rejected.
- **(b) The direct-accessor contract is sufficient for every surface in or scoped by the expansion.** The MCP surface consumes the HTTP snapshot endpoint, not an in-process bus, so it imposes no subscriber requirement. Grafana scrapes `/metrics`; the TUI polls. Three surfaces, three independent request/response acquisition layers — none needs the others' push.
- **(c) The subscriber-pattern cost still exceeds its value at the current panel count.** Rewriting every panel to subscribe, plus the debounce/backpressure/dropped-event machinery a bus implies, is not justified by a third surface that is itself a polling HTTP client.

## Consequences

**Accepted:**

- The ADR-005 gating event #1 is now governed by a written record rather than left implicit. When the `mcp_adapter` package lands and `broker_snapshot_v1` actually ships, **this ADR's status is the deferral of record** — the implementer of that package updates this file (status → "Re-affirmed at v0.x" or "Superseded by a build-the-bus decision") rather than rediscovering the trigger.
- Panels remain direct accessors. No interface change to `BastionPanel.render_data`. No new in-process dependency between the TUI layer and broker internals (the wrong-import-direction hazard ADR-005 and spec §6.5 both flag stays closed).
- `broker_snapshot_v1` ships as a wrapper over the existing HTTP endpoint, honoring ADR-007. The endpoint is the contract; the tool is a thin, versioned, schema-validated client of it.

**Rejected risk:**

- We are **not** silently letting the "third surface" trigger fire unrecorded. The spec called this out as a completeness gap; this ADR closes it.
- We are **not** committing to build the subscriber bus merely because the surface count reaches three. ADR-005's gating language ("becomes worth its upfront cost") is a *prompt to re-evaluate*, and the re-evaluation — against a third surface that is itself a polling HTTP client — concludes "still defer."

**Re-evaluation triggers (inherited and narrowed from ADR-005):**

This deferral is itself reopened when **any** of:

1. `broker_snapshot_v1` (or a successor MCP tool) is reworked to require **synchronous in-process push** rather than wrapping the HTTP endpoint — i.e. a real in-process subscriber need appears, not a polling client.
2. A **fourth** operational surface lands that genuinely needs event-driven consumption (e.g. an in-process policy/autonomy module per ADR-005 trigger #3), so a single bus would serve 2+ in-process subscribers.
3. The `/broker/snapshot` (or `/broker/status`) polling cadence is demonstrated insufficient for an operator workflow where event-driven push beats polling (ADR-005 trigger #2).

If any occur, supersede this ADR with the subscriber design + migration plan.

## Alternatives Considered

**Build the subscriber/pub-sub bus now (rejected).** A literal reading of ADR-005 gating event #1 ("third surface → build *b*") would mandate it. Rejected because the third surface (`broker_snapshot_v1`) is a polling HTTP client of an endpoint that already exists, imposes no subscriber requirement, and the bus cost (panel rewrites + backpressure/debounce machinery) is unchanged from when ADR-005 deferred it. Cost still exceeds value.

**Leave the trigger unrecorded until the adapter ships (rejected).** Simplest, but the spec explicitly flags an implicitly-fired, unrecorded trigger as a governance defect. A pre-positioned record costs one document and removes the "discovered after the fact" failure mode.

**Treat SSE `/broker/snapshot/stream` as the gating event instead (rejected).** SSE is an external `StreamingResponse`, not in-process pub/sub; per spec §9 it does not trip ADR-005 option *b*. Folding it into the trigger would conflate external push with internal observation — the exact conflation ADR-005's "Decision" rationale #2 warned against.

## Implementation Notes

No product code follows from this ADR — it is a governance record, consistent with ADR-005 ("ratifies the status quo"). Relevant surfaces:

- `src/bastion/server.py` — `GET /broker/snapshot` (the HTTP endpoint `broker_snapshot_v1` will wrap), registered in **both** `create_app` and `create_admin_app` per the spec's dual-factory rule (§4.10).
- `mcp_adapter/tools/broker_snapshot_v1.py`, `schemas/broker_snapshot_v1.json` — **do not exist yet**; blocked on `mcp_adapter` v0.5 (ADR-007). When authored, the implementer updates this ADR's status.
- `src/bastion/dashboard/widgets.py` — `BastionPanel.render_data(data: dict)` remains the locked contract (ADR-005).

## References

- ADR-005 §"Gating event for revisiting" #1 — the "third operational surface" clause this ADR answers.
- `docs/design/specs/2026-06-19-observability-expansion.md` §5.6 (MCP `broker_snapshot_v1` catalogue row) and §9 ("explicit governance record" / "the trigger must not be left in an implicitly-fired, unrecorded state").
- ADR-007 — `_v<N>` suffix + committed JSON Schema + adapter-side validation; `broker_snapshot_v1` follows it exactly.
- ADR-009 — TUI-deprecation revisit trigger #3 aligns with ADR-005 gating event #1 (both keyed to a non-TUI operational surface arriving).
