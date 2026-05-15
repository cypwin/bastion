# ADR-005: BastionPanel data contract — subscriber vs. direct accessor

**Status:** Accepted (v0.3 scope)
**Date:** 2026-05-14
**Deciders:** S122 Opus orchestrator with reference to S121 vision council
**Related:** `docs/design/specs/2026-05-14-dashboard-v0.4-vision-c.md`, `_archive/sessions/S121/vision-council/round_4_synthesis/FINAL_RECOMMENDATION.md` §4 (Synthesizer dissent)

## Context

The `BastionPanel` base class landed in v0.3 (commit `c203a88`, S122) to scope panel CSS away from the global `Static` rule. Today's TUI panel subclasses (GPUPanel, ModelsPanel, TemperaturePanel, etc.) consume broker state via a `render_data(data: dict[str, Any])` method: the dashboard's polling loop fetches `/broker/status` every N seconds and pushes the resulting dict into each panel's `render_data` call. Panels are direct accessors — they read whatever the polling loop hands them.

The S121 vision council (run 2026-05-14) raised a sharper question through the Synthesizer lens:

> "What tips this: if the v0.3 BastionPanel base class is implemented as a broker-state subscriber rather than a direct state accessor, Vision E is nearly free. If panels reach into broker internals directly, every new surface is a fork."

Vision E (hybrid multi-surface — TUI + Grafana + web + voice + autonomy) was not chosen for v0.4 (Vision C won — Grafana-Native Observability). But the SYN dissent observes that the **shape of `BastionPanel`'s data contract** decides the cost of every future surface, not just whether Vision E ships.

Two patterns are in play:

**(a) Direct accessor (status quo).** Panels accept a dict, render. The polling loop owns "when fresh data exists." Each new surface (web, voice, autonomy) writes its own polling/accessor layer.

**(b) Subscriber.** Panels subscribe to a broker-state event stream (in-process pub/sub, e.g., `asyncio.Queue` or observer pattern). The broker pushes state-change events to all subscribed surfaces; panels react. Each new surface is a new subscriber — no per-surface fork.

## Decision

**v0.3 and v0.4 ship with the direct-accessor pattern (a). Subscriber pattern (b) is deferred and rescoped per the gating event below.**

Rationale, in priority order:

1. **Vision C doesn't need (b).** Grafana consumes `/metrics` independently — it does not subscribe to in-process state. Adopting (b) for Vision C delivers no value.
2. **Vision B's needs differ.** When (and if) Vision B (web dashboard with WebSocket narration) is greenlit in v0.4.1, it will need real-time state push — but the right abstraction there is the FastAPI WebSocket layer reading from broker state, not an internal pub/sub bus. The "subscriber pattern" framing conflates two concerns: (i) panels reacting to state changes (internal observation), (ii) external surfaces receiving state changes (WebSocket / SSE). The web work will design (ii) directly; retrofitting (i) onto the TUI in advance buys no leverage.
3. **Vision E is not the v0.4 plan.** Optimizing the TUI's data contract for a vision we did not pick is speculative work — exactly the kind of "premature abstraction for hypothetical future requirements" that project guidelines warn against.
4. **Cost of (b) now is non-trivial.** Every Phase C-C/D panel would need to be rewritten to subscribe. Phase C is mid-flight (BastionPanel base just landed, WT-C-A-01 token plumbing landed, 13+ worktrees queued). Introducing (b) mid-stream risks invalidating in-flight agent work.
5. **Direct accessor is correct for the operator population.** Solo-operator dashboards poll every N seconds. Sub-second state push is not on the requirements list and would surface its own complexity (debouncing, backpressure, dropped events).

## Consequences

**Accepted:**

- Phase C-C/D panels continue to subclass `BastionPanel` and accept a dict in `render_data`. No interface change.
- v0.4 Vision C ships without touching this contract. `/metrics` and the Grafana JSON are independent of `BastionPanel`'s shape.
- Each future non-TUI surface will design its own data-acquisition layer (Grafana scrape, web WebSocket, MCP query) rather than sharing a single in-process bus.
- If Vision E is ever revisited, the refactor cost is one-time, scoped to the panels then in the tree, and pays for itself only if 3+ surfaces are planned.

**Rejected risk:**

- We are not "leaving Vision E nearly free on the table" (per SYN). Vision E was not picked. The free-ness was conditional on already paying the subscriber cost in v0.3 — which we are explicitly choosing not to do.

**Gating event for revisiting:**

This ADR is **reopened** when any of the following hold:

1. A third operational surface (beyond TUI and Grafana) is greenlit for the broker. At that point the per-surface accessor cost is paid 3×; (b) becomes worth its upfront cost.
2. The `/broker/status` polling cadence is shown to be insufficient for an operator workflow (e.g., sub-second alert dwell time matters), and event-driven state push beats polling for the same workflow.
3. Vision A (autonomous policy) is greenlit. Vision A's policy module would benefit from event-driven consumption of state changes — at that point a single internal pub/sub serves both Vision A and any other in-process subscribers.

If any of (1), (2), (3) occur, draft **ADR-005-B** with the subscriber design and migration plan.

## Alternatives Considered

**Hybrid pattern (rejected).** Some "live" panels (thrashing, breaker state) on a subscriber bus, others (memory, network, CPU) on direct dict accessor. Adds two patterns to maintain and a per-panel decision on which to use. The split would also break encapsulation (panels need to know what kind of update cadence they want). Rejected — the cost of two patterns exceeds the value over either one.

**Subscriber now, scoped to one panel (rejected).** Implement subscriber pattern only for the thrashing panel (where it would have the most value), keep the rest as accessors. Same downside as hybrid; also leaves the thrashing panel as an architectural outlier whose conventions might mislead future contributors.

**Polling at the panel level (rejected).** Each panel polls broker state independently. N panels × N HTTP calls is wasteful and out of step with the existing single-poll-loop design. Rejected without further consideration.

## Implementation Notes

No implementation work follows from this ADR — it ratifies the status quo. The relevant code surfaces:

- `src/bastion/dashboard/widgets.py` — defines `BastionPanel`. The `render_data(data: dict[str, Any]) -> Table` shape is the contract this ADR locks for v0.3 and v0.4.
- `src/bastion/dashboard/app.py:530-590` — the polling loop that fetches `/broker/status` and dispatches to each panel's `render_data`.
- Phase C-C/D worktrees: continue as planned, no scope change from this ADR.

## References

- S121 vision council FINAL_RECOMMENDATION §4 (Synthesizer dissent) — the original framing of the subscriber-vs-accessor question.
- S121 vision council decision_dag.json n2 — recorded as `"subscriber pattern (broker-state event stream) — recommended: true"`. This ADR formally overrides that node for the v0.3/v0.4 window because the council's recommendation was conditional on Vision E being picked, which it was not.
- `tmp_S121_PLAN.md` §6 ponder-prompt #2 — "Is Phase A's `_extract_streaming_tokens` wire-up really the highest leverage?" — same shape of question (optimize for a vision we haven't picked vs. ship the vision we did pick). This ADR resolves the analogous question for `BastionPanel`.
