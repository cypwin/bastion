# BASTION v0.4 — Vision C: Grafana-Native Observability

**Date:** 2026-05-14 (amended 2026-05-15 with council corrections from plan review)
**Status:** Draft (codifies WT-D-01 council pick)
**Origin:** multi-lens design council, 2026-05-14.
**Supersedes:** §4 vision-pick decision in `tmp_S121_PLAN.md`. Does not modify Phase A/B/C of that plan.

---

## Problem Statement

BASTION computes far more state than it surfaces, and the only consumer today is a single-operator TUI. As the project moves toward public ship, three operator populations need surfaces the TUI alone cannot serve:

1. **Solo desktop operator** — already has Grafana on their machine for other services; wants BASTION to plug in.
2. **Small team / dedicated server** — has an ops stack; needs Alertmanager paging integration for 3am incidents.
3. **Multi-node future** — needs Prometheus federation.

The TUI remains correct for SSH-only and headless boxes (AFM constraint: must never be the sole surface). The gap is an observability substrate that **exports** rather than **displays**.

This spec codifies the design review pick: **populate the already-stubbed `/metrics` and `telemetry.py`, ship in-tree Grafana dashboards + a turnkey docker-compose, wire one Alertmanager rule, keep the TUI permanent.**

## Design Goal

`docker compose up` against a fresh BASTION install yields:

- Prometheus scraping BASTION's `/metrics` on the operator-defined interval.
- Grafana pre-loaded with `bastion-overview.json` (the canonical dashboard).
- Alertmanager wired with one rule (`increase(bastion_thrashing_detector_halt_total{verdict="HALTED"}[5m]) > 0`) that POSTs the auth-gated `/broker/control/restart` endpoint as webhook target. The bare `> 0` form would fire permanently after the first halt because the underlying metric is a non-decrementing Counter; the `increase(...[5m])` window self-resolves when thrashing stops (see plan §Risk R2).
- OTLP traces flowing to the operator-configured collector when `BASTION_OTLP_ENDPOINT` is set.

The TUI continues to work unchanged. Nothing in this spec deprecates, demotes, or modifies the Phase C dashboard work that closes v0.3.

## Scope

| In | Out |
|---|---|
| Full `/metrics` population (5 named metrics below) | Vision B web SPA / WebSocket / narration |
| `dashboards/grafana/bastion-overview.json` in-tree | Vision A autonomous policy engine |
| `docker-compose.yml` with Prometheus + Grafana + Alertmanager | Vision D MCP / voice control |
| One Alertmanager rule + `/broker/control/restart` webhook wiring | Multi-tenancy, RBAC, OAuth |
| OTLP export config in `broker.yaml` | TUI deprecation timeline |
| `/metrics` schema freeze at the v0.3 git tag | Backwards-incompatible metric renames |

## Non-Goals

1. **No TUI deprecation.** Permanent coexistence per AFM hard constraint. A failed Grafana container must not blind the operator.
2. **No Vision B work in v0.4.** Queued for v0.4.1, gated on three open ADRs (auth model, in-repo vs. separate package, asset-server crash isolation).
3. **No new push protocol.** `/broker/stream` SSE remains deferred per the 8-call council; Prometheus scrape is sufficient.
4. **No BastionPanel refactor as a precondition.** Subscriber pattern is captured in ADR-005 for the next-surface boundary, not v0.4.

## Architectural Decisions (from `decision_dag.json`)

| Node | Decision | Choice | Rationale |
|---|---|---|---|
| n1 | Push mechanism | Polling (Prometheus scrape) | Zero new code; SSE/WebSocket deferred to Vision B. |
| n3 | TUI coexistence | Permanent | AFM hard constraint; bounded blast radius. |
| n4 | Vision primary | Vision C (Grafana) | Reuses existing `metrics.py` + `telemetry.py`; read-only attack surface; no new toolchain. |
| n5 | Metric completeness | Full signal set | Enables Alertmanager on all three failure-mode signals. |
| n6 | Docker-compose turnkey | Yes | Forecloses "Grafana burden" objection; SOC endorses. |
| n7 | Grafana JSON location | In-tree at `dashboards/grafana/` | Single repo; versioned with broker. |
| n8 | Alertmanager rules location | Docker-compose volume (`alerting/bastion.rules.yml`) | Turnkey with compose; available out of box. |
| n10 | API schema freeze | At v0.3 tag | Clean hand-off; gates Vision B without touching v0.4 work. |

## Metric Schema (frozen at v0.3 tag)

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `bastion_model_swap_total` | Counter | `from_model`, `to_model`, `reason` | Thrashing detection input; cardinality bounded by configured model registry |
| `bastion_request_queue_wait_seconds` | Histogram | `priority`, `model` | Tail latency for queueing; buckets per `metrics.py` config |
| `bastion_vram_used_mb` | Gauge | `gpu_index` | VRAM ledger as scraped state |
| `bastion_thrashing_detector_halt_total` | Counter | `agent_id`, `verdict` | Alert input (verdict ∈ `WARNED`, `HALTED`). **`agent_id` MUST be a registered agent name OR the source IP truncated to /24; never a task UUID** (cardinality bound — see plan §Risk R3) |
| `bastion_concurrent_requests_active` | Gauge | (none) | Inflight gauge for capacity dashboards |

These five constitute the **public contract at v0.3 tag**. Any rename or label change after the tag is a breaking schema change (see §Falsifiable Guardrails).

## Worktree Backlog — Phase E

Each item is a single-purpose worktree. Branches under `feat/vision-c-*`.

| WT | Title | Effort | Branch | Depends on |
|---|---|---|---|---|
| **WT-E1** | Schema-freeze the 5 metrics in `metrics.py` (cardinality bounds + emit sites) | Sonnet · 3 h | `feat/vision-c-metrics-schema` | v0.3 tag (or last v0.3 commit) |
| **WT-E2** | Ship `dashboards/grafana/bastion-overview.json` | Sonnet · 2 h | `feat/vision-c-grafana-overview` | WT-E1 |
| **WT-E3** | Ship `docker-compose.yml` + `prometheus/`, `grafana/`, `alerting/` config dirs | Sonnet · 2 h | `feat/vision-c-docker-compose` | WT-E2 |
| **WT-E4** | Add Alertmanager rule `thrashing_detector_halt_total > 0` + wire `POST /broker/control/restart` webhook target | Sonnet · 1 h | `feat/vision-c-alertmanager-rule` | WT-E3, WT-C-Y (restart endpoint from Phase B) |
| **WT-E5** | Enable OTLP export via `BASTION_OTLP_ENDPOINT` env + `telemetry.py` activation | Sonnet · 1 h | `feat/vision-c-otlp-export` | WT-E1 (file-touch overlap on `config/broker.yaml`) |

**Sequencing**: E1 → E2 → E3 → E4 (linear). E5 forks from the post-E1 merge commit and runs parallel to E2/E3/E4 from that point. The logical coupling between E1 and E5 is weak (E5 does not consume E1's metric schema), but both worktrees write the same `config/broker.yaml` block, so opening E5 before E1 merges produces a three-way YAML conflict at adjacent keys. None depend on the BastionPanel refactor or any Phase C-C/D layout work.

## Files Touched

| Action | File | Responsibility |
|---|---|---|
| Modify | `src/bastion/metrics.py` | Add 5 named metrics; remove dead-code stubs flagged in observability audit |
| Modify | `src/bastion/middleware.py` | Record `request_queue_wait_seconds` at queue-exit point |
| Modify | `src/bastion/scheduler.py` | Increment `model_swap_total` on swap; emit reason label |
| Modify | `src/bastion/thrashing.py` | Increment `thrashing_detector_halt_total` on verdict transition |
| Modify | `src/bastion/vram.py` | Update `vram_used_mb` gauge on ledger refresh |
| Modify | `src/bastion/telemetry.py` | Honor `BASTION_OTLP_ENDPOINT` env; wire active span propagation |
| Create | `dashboards/grafana/bastion-overview.json` | Canonical operator dashboard |
| Create | `docker-compose.yml` | Top-level turnkey stack |
| Create | `prometheus/prometheus.yml` | Scrape config for `host.docker.internal:11434/metrics` |
| Create | `grafana/provisioning/dashboards/bastion.yaml` | Auto-load overview JSON |
| Create | `grafana/provisioning/datasources/prometheus.yaml` | Auto-provision Prometheus datasource (required for turnkey promise; omitted in original spec) |
| Create | `alerting/bastion.rules.yml` | One thrashing-halt alert |
| Modify | `config/broker.yaml` | Add `observability:` section (OTLP endpoint, scrape interval hints) |
| Modify | `docs/releasing.md` | Document `docker compose up` first-run experience |

## Acceptance Criteria

1. `curl http://localhost:11434/metrics | grep -c '^bastion_'` returns ≥ 5 distinct metric names.
2. `docker compose up` brings Prometheus + Grafana + Alertmanager to healthy state inside 30s on a fresh clone.
3. Opening `http://localhost:3000/d/bastion-overview` after a 60s warm-up shows non-empty VRAM/swap/queue panels.
4. Triggering a synthetic thrashing event (test helper) fires the Alertmanager rule and the webhook POSTs the restart endpoint with the configured bearer token.
5. With `BASTION_OTLP_ENDPOINT=http://localhost:4318` and a local collector, traces appear for at least: task submission, queue wait, model swap.
6. `python -m pytest tests/ -v` passes (no regressions).
7. The TUI continues to render correctly with the new metric emit sites in place.

## Falsifiable Guardrails (from council §5)

Re-validation criteria for the vision pick after v0.4 ships.

| Observation | Threshold that invalidates C | Triggered action |
|---|---|---|
| Operator session type | >60% browser-session in survey/telemetry | Promote B to primary; revisit TUI status |
| Grafana adoption | <25% of v0.4 adopters run docker-compose after 90 days | Drop in-tree JSON; shift to B |
| `/metrics` schema churn | >2 breaking metric renames between v0.4 and v0.5 | Versioned `/metrics/v1/` endpoint |
| Incident response time | Median thrashing-onset → operator-alert unchanged | Re-evaluate Vision A |
| Alert fatigue | Halt alert fires >3×/week as false positive | ADR on thrashing detector threshold |
| Onboarding friction | >50% of new installs cite Prometheus/Grafana as blocker | Ship Vision B immediately |

## Dissent Log

Recorded for posterity.

- **CC** would ship Vision B as primary; argues C is a free dividend of B's API surface.
- **SYN** would ship E-scoped (headless refactor + B); flags BastionPanel subscriber pattern as the pivot.
- **PMS** would defer all vision work until v0.3 is tagged clean.
- **AFM** endorses C conditionally; would prefer B + TUI coexistence if asset-server crash isolation can be guaranteed.

## ADR Queue (deferred to their gating events)

- **ADR-004** — Auth model for Vision B admin port. **Write before any v0.4.1 B-work starts.**
- **ADR-005** — BastionPanel data contract: subscriber vs. direct accessor. **Write before the second surface ships** (whichever comes after the TUI).
- **ADR-006** — In-repo vs. separate-package boundary for `bastion-web`. **Write alongside ADR-004.**
- **ADR-007** — Vision A bootstrap policy when Ollama unreachable. Deferred until Vision A is considered.

## What This Spec Does NOT Do

- Does not commit `docs/dashboard-redesign-spec` (the v0.3 redesign spec is on its own branch; that work is separate and ships first).
- Does not touch `src/bastion/dashboard/` — TUI work is Phase C of the v0.3 plan.
- Does not modify the 8-call council synthesis amendments tracked for Phase B/C of the dashboard redesign.
- Does not pre-commit any specific Grafana panel layout; that's a WT-E2 implementation detail.

---

## Council Corrections (amendment 2026-05-15)

The implementation-plan review (4 lenses: parallel-merge-safety-engineer, adversarial-failure-mode-auditor, sre-incident-operator-3am, synthesizer) flagged four spec gaps. All four are folded into the body above and are restated here so the spec/plan delta is auditable.

1. **WT-E5 dependency.** Spec originally said "parallel-safe, depends on: none." Corrected: WT-E5 forks from the post-E1 merge commit because both E1 and E5 write the `observability:` / `telemetry:` blocks of `config/broker.yaml`. The logical coupling is weak; the file-touch overlap is concrete. See Worktree Backlog table.

2. **`agent_id` cardinality bound.** Spec originally did not constrain the `agent_id` label values on `bastion_thrashing_detector_halt_total`. Corrected: `agent_id` MUST be a registered agent name OR the source IP truncated to /24 prefix; task UUIDs are forbidden. See Metric Schema table and plan §Risk R3.

3. **Grafana datasource provisioning file.** Spec's Files Touched table listed `grafana/provisioning/dashboards/bastion.yaml` but omitted `grafana/provisioning/datasources/prometheus.yaml`. Without the datasource file, the `docker compose up` turnkey promise breaks (Grafana has no Prometheus to query). Added to Files Touched.

4. **Alert expression.** Spec originally specified `thrashing_detector_halt_total > 0`. Corrected: `increase(bastion_thrashing_detector_halt_total{verdict="HALTED"}[5m]) > 0`. The bare form would fire permanently after the first halt (Counter never decrements). The `increase()` window self-resolves when thrashing stops. See Design Goal and plan §Risk R2.

---

*Spec authored S122 (2026-05-14) by Opus 4.7. Amended S122 (2026-05-15) with plan-review council corrections. Hand-off to implementation worktrees once v0.3 closes and the schema-freeze tag lands.*
