# ADR-010: Grafana JSON compatibility — schema-validated CI fixtures, pinned-compose for dev, drop-on-breakage policy

**Status:** Accepted (v0.5 ship-blocker for any `dashboards/grafana/*.json`)
**Date:** 2026-05-19
**Deciders:** S122 Opus orchestrator with reference to S122 plan-C vision council
**Related:** `_archive/sessions/S122/vision-A-E-retro-20260519-1519/round_4_synthesis/FINAL_RECOMMENDATION.md` Step 5 + adversarial-failure-mode-auditor dissent

## Context

Vision C (v0.4) ships Grafana JSON in-tree at `dashboards/grafana/*.json`. The S122 plan-C council's adversarial lens dissent was unambiguous:

> *"Shipping [Grafana JSON] in-tree without a CI-enforced compatibility fixture guarantees that `dashboards/grafana/*.json` silently stops rendering within two Grafana minor releases."*

The mechanism: Grafana evolves dashboard JSON shape across minor releases. Panel-plugin renames, query-shape changes, deprecated JSON keys silently dropped. Without compatibility validation, the dashboards in-tree become a slowly-rotting artifact. Operators who installed BASTION 6 months ago run a Grafana version that no longer renders BASTION's dashboards. Nobody files a bug because nobody is paid to maintain JSON they didn't write.

The council recommended Step 5: *"One compatibility fixture test per shipped JSON artifact."* This ADR specifies the mechanism.

Three credible compatibility approaches:

**(a) Pinned docker-compose:** Ship a `docker-compose.yml` alongside the JSON that pins Grafana to a known-working version. Operators run the docker-compose to get a working stack.

**(b) Schema validation:** Validate the JSON against Grafana's published dashboard JSON Schema in CI. Multiple Grafana versions validated against; release blockers on validation failure.

**(c) Drop in-tree, provide provisioning guide:** Ship a markdown guide showing operators how to construct equivalent dashboards in their own Grafana instance.

The falsifiable guardrail from the council:

> *"5. Grafana CI fixture fails >1×/quarter. Drop JSON in-tree; ship provisioning guide instead."*

This implies the council's preferred state is (b), with (c) as the drop-on-breakage fallback.

## Decision

**v0.5 adopts (b) — schema validation in CI — as the primary mechanism, paired with (a) — pinned docker-compose — for the developer onboarding path. Failure rate >1×/quarter triggers (c) — drop JSON in-tree.**

Specifically:

1. **JSON Schema validation in CI.** Every `dashboards/grafana/*.json` file is validated against Grafana's official dashboard JSON Schema in CI. The schema is fetched from `https://github.com/grafana/grafana/blob/v<VERSION>/public/api-spec.json` (or equivalent) for each pinned Grafana version.

2. **Multi-version validation.** CI validates against THREE Grafana versions: current-stable, current-1-major (e.g., 11.x), and current-2-major (e.g., 10.x). All three must pass. Tracks Grafana's typical "support last 2 major releases" pattern.

3. **Renderer smoke test.** Beyond schema, CI also runs a renderer smoke test: spin up Grafana in Docker with the pinned-compose, POST the JSON via `/api/dashboards/db`, GET it back, screenshot via Grafana's `/render` endpoint, and confirm:
   - HTTP 200 from the screenshot endpoint
   - PNG output is >5 KB (sanity-check that something rendered)
   - No "Panel plugin not found" strings in the resulting HTML when viewing the dashboard via `/d/<uid>?orgId=1&render=1`
   This is more expensive than schema validation but catches semantic breakage that schema validation misses.

4. **Pinned docker-compose for development.** `dashboards/grafana/docker-compose.yml` pins Grafana and Prometheus versions. Operators can `docker compose up` to get a working stack matching what CI validated. Update the pinned versions in lockstep with the BASTION minor-release cycle.

5. **Failure-rate trigger for drop-in-tree.** If CI compatibility fails MORE THAN ONCE PER QUARTER (i.e., requires a JSON edit to pass on a new Grafana release more than 4× per year), this ADR is reopened — the maintenance burden has exceeded the value. ADR-010-B will then specify the migration to (c) provisioning guide.

6. **Per-JSON-file changelog entry on schema bump.** When CI fails on a new Grafana version and requires JSON edits, the CHANGELOG must explain what changed. Lint rule: if `dashboards/grafana/*.json` modtime is newer than CHANGELOG.md, CI fails until CHANGELOG entry exists.

7. **Operator-side guidance.** `docs/deployment.md` documents the supported Grafana version range. Falls back to (c) provisioning guide if the operator's Grafana is outside that range — guide takes over where JSON validation can't.

## Consequences

**Accepted:**

- Every Grafana JSON in `dashboards/grafana/` is CI-validated. Silent rot is impossible — CI fails first.
- Renderer smoke test (point 3) adds ~30s to CI per JSON file. Acceptable.
- Pinned docker-compose lets operators reproduce CI's environment one-command. Onboarding path matches what CI validates.
- Three-version validation matrix means we discover breaking changes 1-2 releases before they hit operators on current-stable.
- CHANGELOG enforcement keeps the audit trail visible.
- The drop-on-breakage trigger is automatic: 4 failures/year and (b) → (c) without further negotiation.

**Rejected risk:**

- Three-version validation requires CI runners that can pull three Grafana images. Increases CI minutes. Acceptable for a quarterly release cadence.
- Renderer smoke test is screenshot-based; image-diff is NOT part of the validation. The check is "renders something" not "renders correctly." Image-diff is a hidden ADR-010-B if needed.

**Gating event for revisiting (ADR-010-B):**

This ADR is reopened when any of:

1. The CI failure rate exceeds 1×/quarter for 2 consecutive quarters. Migration to (c) provisioning guide is triggered; this ADR is sunset.
2. Grafana ships a stable Provisioning-API that subsumes the JSON-in-tree model entirely. At that point, the JSON files become unnecessary; the provisioning config replaces them.
3. BASTION ships a built-in metrics dashboard (e.g., embedded chartjs frontend) that obviates the Grafana export path. ADR-010 becomes moot.

## Alternatives Considered

**Pinned docker-compose ONLY (rejected — operator-friction).** Forces every operator to run Docker. Operators with existing Grafana installs (the most common case) get nothing useful from a pinned-compose alone.

**Schema validation ONLY (rejected — false-positives).** JSON Schema is structural, not semantic. A JSON that schema-validates can still render to "Panel plugin not found." Renderer smoke test is needed alongside.

**Drop in-tree now, ship provisioning guide (rejected — premature).** This is the council's drop-on-breakage fallback. The trigger condition (1×/quarter CI failure) has not been observed yet. Premature drop sacrifices the immediate-onboarding value of JSON-in-tree.

**Image-diff in CI (rejected — flaky).** Pixel-perfect dashboards are too brittle. Renderer smoke test (renders ANYTHING > 5 KB) catches the failure mode that matters (panel-plugin-missing); image-diff catches changes that don't matter (anti-aliasing differences across Grafana minor versions).

## Implementation Notes

CI workflow surfaces in v0.5:

- `.github/workflows/grafana-compat.yml` — new workflow. Matrix over [current-stable, current-1, current-2] Grafana versions. Each row: schema validation + renderer smoke test.
- `tests/grafana_compat/test_schemas.py` — fetches schema for each pinned Grafana version; validates every JSON in `dashboards/grafana/`.
- `tests/grafana_compat/test_render.py` — docker-up Grafana, post JSON, GET render, assert PNG > 5 KB.
- `dashboards/grafana/docker-compose.yml` — pinned-version compose, network-isolated, includes Prometheus pointing at BASTION's `/metrics`.
- `dashboards/grafana/CHANGELOG.md` — per-JSON change log; CI lint rule (`scripts/check_grafana_changelog.sh`) enforces entries.
- `docs/deployment.md` — Grafana version range + drop-back-to-(c) guidance for out-of-range versions.

Renderer smoke-test image: Grafana ships an "image rendering" plugin (`grafana-image-renderer`). Documented installation is via `grafana-cli plugins install grafana-image-renderer`. CI image (`grafana/grafana-image-renderer:latest`) handles this.

Cost estimate: ~3 minutes of additional CI per release, $$ negligible.

## References

- S122 plan-C council FINAL_RECOMMENDATION Step 5 — Grafana JSON CI fixture requirement.
- adversarial-failure-mode-auditor lens dissent — silent-rot mechanism.
- Council guardrail #5 — CI failure >1×/quarter triggers drop-in-tree.
- Grafana documentation — dashboard JSON model, image-renderer plugin, provisioning API.
