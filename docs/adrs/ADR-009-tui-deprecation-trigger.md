# ADR-009: TUI deprecation trigger — session-count-driven, threshold-gated, multi-signal confirmation

**Status:** Accepted (long-horizon; v0.5+ instrumentation, deprecation no earlier than v0.7)
**Date:** 2026-05-19
**Deciders:** S122 maintainer with reference to S122 plan-C design review
**Related:** ADR-005 (BastionPanel contract — TUI lock-in for v0.4), ADR-007 (MCP adapter — first non-TUI surface), `_archive/sessions/S122/vision-A-E-retro-20260519-1519/round_4_synthesis/FINAL_RECOMMENDATION.md` Step 4

## Context

The Textual TUI dashboard is the only operator surface today. The S122 plan-C council layered Vision D (MCP adapter) on top of Vision C (Grafana) and identified that TUI deprecation, if it happens, comes through *usage erosion* not *strategic deprecation*. The socratic-interrogator lens (council 2026-05-19):

> *"None of the eight calls produces evidence from user research, issue trackers, or operator logs proving which pain is sharpest."*

The sre-incident-operator-3am lens reinforced:

> *"At 3am with a stalled embedding pipeline and a GPU thrashing into swap, I do not want a chat widget."*

Combined message: the TUI has a durable niche (incident response, SSH-only contexts) that MCP adapter (Vision D) and Grafana (Vision C) do NOT replace. TUI deprecation is therefore a **future-state-conditional** decision, not a planned milestone.

The council's Step 4 also triaged Phase C-D polish backlog items against TUI deprecation risk:

- **Ship anyway:** env-var layer (C-D-04), deuteranopia theme (C-D-05) — these survive deprecation.
- **Defer:** ConfirmActionModal (C-D-01), HelpModal glossary (C-D-02), --bell (C-D-06) — these sink with TUI deprecation.

This triage assumes there IS a deprecation trigger. This ADR specifies it.

The question: **what observable signal would justify deprecating the TUI?**

Three candidate signals:

**(a) Session-count metric:** Telemetry on TUI launches per week. If `tui_sessions_per_week / mcp_sessions_per_week` falls below a threshold for N consecutive weeks, deprecate.

**(b) Issue-tracker velocity:** Bugs filed against TUI vs. MCP adapter. If TUI bug-fix work consistently exceeds MCP work AND TUI usage is low, deprecate.

**(c) Operator-survey signal:** Direct ask. Survey operators at version bumps; deprecate if >70% report "I don't use TUI anymore."

## Decision

**v0.5+ instruments all three signals. Deprecation triggers when (a) AND ((b) OR (c)) hold simultaneously for 2 consecutive minor releases.**

Specifically:

1. **Signal (a) — TUI session count.** v0.5's TUI logs one `tui_session_start` audit_event per launch (anonymous; just session-start + duration + version). Aggregated via `bastion audit --type tui_session_start --since 90d | jq 'count'`. **Threshold:** TUI sessions per operator-week falls below 0.5 (i.e., fewer than 1 TUI launch every 2 weeks on average) AND MCP sessions per operator-week exceeds 5.0.

2. **Signal (b) — Issue-tracker velocity.** No instrumentation required; manually tallied at each minor release from GitHub issues with `tui` vs. `mcp` labels. **Threshold:** MCP-labeled merged PR count exceeds TUI-labeled by 3× over the prior minor-release cycle, AND TUI-tagged issues older than 90 days are >30% of open TUI issues (= maintenance debt accumulating without resolution).

3. **Signal (c) — Operator survey.** At every minor release (v0.6, v0.7, ...), the release notes include a one-line opt-in survey link: "How often do you use the BASTION TUI?" Five-bucket Likert; results aggregated in the v0.X+1 release notes. **Threshold:** >70% of respondents report "rarely" or "never" AND respondent count >20 (sample sufficiency floor).

4. **Trigger combination: (a) AND ((b) OR (c)).** Telemetry alone is not sufficient — telemetry could collapse for reasons unrelated to deprecation merit (e.g., temporary MCP-client hype that fades). Pairing usage erosion with maintenance debt OR operator preference confirms the trend.

5. **Two-consecutive-release rule.** The trigger must hold for 2 consecutive minor releases. A single noisy data point does not justify deprecation; sustained signal does.

6. **Deprecation process when triggered.**
   - v0.X release notes announce planned deprecation in v0.(X+2).
   - v0.(X+1) prints a launch-time warning when the TUI starts: *"BASTION TUI scheduled for removal in v0.(X+2). MCP adapter and Grafana remain. See <link>."*
   - v0.(X+2) removes the TUI package; `bastion dashboard` returns a clear "removed; see <link>" message.

7. **Earliest possible deprecation: v0.7.** v0.5 ships instrumentation. v0.6 collects first full release cycle. v0.7 could in principle trigger if (a) AND (b/c) hold AND v0.5 also showed the trend. This is intentionally slow — TUI removal is a one-way door.

## Consequences

**Accepted:**

- C-D-04 (env-var layer) and C-D-05 (deuteranopia theme) ship in v0.5 alongside MCP adapter — they survive deprecation, so the work is durable.
- C-D-01 (ConfirmActionModal), C-D-02 (HelpModal glossary), C-D-06 (--bell) are deferred to "open-issue-but-low-priority" status — they're not removed from the backlog, just unprioritized until the trigger conditions are clearly NOT met.
- Instrumentation overhead is small: one audit_event per TUI launch. Privacy-preserving (no command captures, just session-start).
- The trigger is hard to game. All three signals come from different populations (telemetry / maintainer effort / operator opinion). A single bad-faith signal cannot tip the decision.

**Rejected risk:**

- The "two minor releases" cadence may be slow if the trigger is unambiguous. Mitigation: this ADR may be reopened to accelerate IF a future incident makes TUI maintenance untenable (e.g., a Textual breaking change that would cost weeks).

**Gating event for revisiting (ADR-009-B):**

This ADR is reopened when any of:

1. The trigger holds for 2 consecutive minor releases — at that point, the action is "execute deprecation," not "reconsider the trigger." Reopen for the deprecation plan itself, not the threshold.
2. Textual (the TUI framework) ships a breaking change that would require >2 weeks of broker-side maintenance — at that point, ADR-009-B reconsiders whether to accelerate deprecation, fork the TUI to a separate package, or absorb the maintenance.
3. A SECOND non-TUI surface (beyond MCP and Grafana) is greenlit — at that point, "TUI as one of N" loses the "TUI is the durable incident surface" argument the SRE lens made.

## Alternatives Considered

**No deprecation trigger — TUI is permanent (rejected — open-ended maintenance commitment).** Locks BASTION into supporting three surfaces forever. Council socratic dissent: "None of the calls produces evidence" — but the response is NOT "lock in everything"; it is "instrument and let evidence drive."

**Telemetry-only trigger (rejected — single-signal fragility).** TUI sessions could drop because of a transient outage in the AI-client landscape, or because the operator population shifts to a different workflow temporarily. Pairing telemetry with maintenance-debt OR operator-survey signal confirms the trend isn't an artifact.

**Survey-only trigger (rejected — sample bias).** Operators who fill out surveys are not representative. A 70% threshold reduces but does not eliminate this. Pairing with telemetry catches the case where survey respondents say "I never use it" but telemetry shows they do (the typical "I value but rarely use" bias).

**Per-version trigger (rejected — over-frequent).** Triggering on a single minor release would let noise dominate. Two-release rule provides natural debouncing.

**Hard date deprecation (rejected — premature commitment).** "TUI deprecated 2027-12-31" decides today what only future evidence can decide.

## Implementation Notes

Code changes in v0.5:

- `src/bastion/dashboard/__main__.py` — at TUI startup, write `audit.event("tui_session_start", duration_estimate=None)`. At normal exit (Ctrl-C, ESC, error), update with actual duration.
- `src/bastion/audit.py` — `tui_session_start` is a new event type but reuses the existing event schema; no schema change.
- `docs/operations.md` — new section "How TUI deprecation is decided" explaining the three signals and trigger combination.
- `README.md` release-notes template — add survey link placeholder for v0.6+.
- `docs/adrs/` — this ADR + ADR-009-B placeholder file (`ADR-009-B-tui-deprecation-plan.md`, status: "Pending trigger").

GitHub label conventions for signal (b): `area:tui`, `area:mcp` (or `area:adapter`). Existing labels are insufficient — add at v0.5 ship.

Privacy: `tui_session_start` events stay local in BASTION's audit log. No telemetry is sent off-host. Operators read their own aggregates via `bastion audit`.

## References

- S122 plan-C council Step 4 — C-D triage rationale.
- sre-incident-operator-3am lens — durable TUI niche argument.
- socratic-interrogator lens — evidence-based decision pattern.
- ADR-005 (BastionPanel direct-accessor contract) — locks panel data shape during the TUI lifetime; ADR-005 gating event #1 ("third operational surface") aligns with this ADR's revisit trigger #3.
- ADR-007 — MCP adapter ships in v0.5; instrumentation surface ratio starts then.

---

## Addendum — 2026-06-19: observability expansion as the TUI-instrumentation baseline reference

The 2026-06-19 **inference-correlated observatory** expansion (`docs/design/specs/2026-06-19-observability-expansion.md`, rev. 3; Phases 1–4) lands a large block of new TUI surface — three new panels (`ContentionPanel`, `ProcessAttributionPanel`, `CorrelationPanel`) plus extended rows on existing panels (GPU clocks/throttle/PCIe/mem-junction, RiskIndex bar, thermal-coupling, stall-reason enrichment). For the purposes of this ADR, that expansion is the **TUI-instrumentation baseline reference**: the population of TUI panels against which future `tui_session_start` telemetry (this ADR's signal *a*) is interpreted.

Three points are fixed here so the deprecation-trigger evidence stays well-defined as the TUI grows:

1. **No new TUI-deprecation signals are added.** Per spec §9, the expansion is *orthogonal* to ADR-009 — it introduces no new deprecation telemetry and does not change the trigger combination (telemetry + maintenance-debt OR survey, two-release rule). The new panels are instrumented from the **same `tui_session_start` baseline** defined in this ADR's Implementation Notes; they do not get their own per-panel session counters.

2. **The expansion is the reference panel set for "what the TUI is" at v0.5/v0.6.** When signal *a* (TUI session erosion) is later evaluated against signals *b*/*c*, "the TUI" means the panel set as of this expansion — including the observatory panels. A drop in TUI usage is read relative to a TUI that already offers the correlation/contention/process surfaces, not the narrower v0.4 panel set. This guards against a false "TUI is unused" reading taken before the richer surface had time to land in operators' workflows.

3. **The third-surface alignment is unchanged and now has its own record.** This ADR's revisit trigger #3 (a non-TUI operational surface arriving) aligns with ADR-005 gating event #1. The expansion's MCP `broker_snapshot_v1` tool is that prospective third surface; its deferral/governance is now recorded in **ADR-005-B** (2026-06-19). ADR-009's trigger combination is not altered by ADR-005-B — the two ADRs share the same keying event but govern different decisions (TUI-deprecation evidence vs. subscriber-bus deferral).

Status of this ADR is **unchanged** by the addendum (still Accepted, long-horizon; instrumentation v0.5+, deprecation no earlier than v0.7). The addendum records the baseline, not a new decision.
