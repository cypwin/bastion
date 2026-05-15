# BASTION v0.4 Vision C — Grafana-Native Observability Implementation Plan

**Date:** 2026-05-15
**Status:** Ready for execution
**Spec:** `docs/design/specs/2026-05-14-dashboard-v0.4-vision-c.md`
**Branches:** `feat/vision-c-metrics-schema`, `feat/vision-c-grafana-overview`, `feat/vision-c-docker-compose`, `feat/vision-c-alertmanager-rule`, `feat/vision-c-otlp-export`

---

## Dependency DAG

```
v0.3 tag (schema-freeze commit)
        |
       WT-E1  feat/vision-c-metrics-schema
     (merge + tag v0.3-schema-freeze)
        |          \
       WT-E2       WT-E5   feat/vision-c-otlp-export
  feat/vision-c-grafana-overview
        |
       WT-E3  feat/vision-c-docker-compose
        |
       WT-E4  feat/vision-c-alertmanager-rule
              (also needs WT-C-Y restart endpoint)
```

**Critical correction to spec:** The spec labels WT-E5 as "parallel-safe" and "none" for dependencies. This is wrong. Both WT-E1 and WT-E5 write `config/broker.yaml` (E1 adds the `observability:` section; E5 would also need to touch that same section for OTLP config). Opening WT-E5 before E1 merges produces a three-way YAML conflict at adjacent keys. The corrected merge sequence opens E5 only after E1 is merged to main. The logical dependency is still weak (E5 does not need E1's metric schema), but the file-touch overlap is concrete and the conflict resolution is not mechanical.

**Concrete merge order:**
1. Merge WT-E1 to main; tag commit as `v0.3-schema-freeze`.
2. Open WT-E2 and WT-E5 simultaneously from the freeze tag.
3. Merge WT-E2; open WT-E3 from post-E2 main.
4. Merge WT-E5 (no shared files with E3/E4 after E1 merged; order relative to E3/E4 is flexible).
5. Merge WT-E3; open WT-E4 from post-E3 main.
6. Merge WT-E4.

---

## Pre-work: Integration Contract

Before any worktree branches, lock the following in a short spec appendix or a comment block in `metrics.py`. These decisions affect every downstream worktree and must not be renegotiated mid-flight.

**Label sets (locked):**

| Metric | Labels | Cardinality note |
|---|---|---|
| `bastion_model_swap_total` | `from_model`, `to_model`, `reason` | Bounded by `models:` registry in `broker.yaml`. `reason` enum: `scheduler_pick`, `affinity_miss`, `eviction`. `from_model="_none"` when loading from idle. |
| `bastion_request_queue_wait_seconds` | `priority`, `model` | `priority` is the 4-value enum (`interactive`, `agent`, `pipeline`, `background`). |
| `bastion_vram_used_mb` | `gpu_index` | Single-GPU deployments use `gpu_index="0"`. |
| `bastion_thrashing_detector_halt_total` | `agent_id`, `verdict` | `verdict` enum: `WARNED`, `HALTED`. `agent_id` MUST be a registered agent name or source IP — never a task UUID. See Risk R3. |
| `bastion_concurrent_requests_active` | (none) | Pure gauge; no labels needed. |

**Networking variable:** `prometheus.yml` targets `${BASTION_HOST:-host-gateway}:11434`. On Linux Docker Engine, `host-gateway` resolves to the host via the `extra_hosts` mechanism. On Docker Desktop (Mac/Windows), `host.docker.internal` is available but `host-gateway` also works. The `docker-compose.yml` must add `extra_hosts: ["host.docker.internal:host-gateway"]` to the Prometheus service.

**Alertmanager idempotency:** `repeat_interval: 4h`, `group_wait: 30s`, `group_interval: 5m` must be committed in `alerting/bastion.rules.yml`. Not left to operator Alertmanager defaults.

**Datasource provisioning:** Commit `grafana/provisioning/datasources/prometheus.yaml`. The turnkey promise fails without it.

---

## WT-E1 — Schema-freeze the 5 metrics in `metrics.py`

**Branch:** `feat/vision-c-metrics-schema`
**Forks from:** Last v0.3 commit (or whatever commit is tagged as the v0.3 release base)

### File changes

#### `src/bastion/metrics.py` (modify)

The five spec-named metrics already partially exist under different names. The work here is:

1. **Add `bastion_model_swap_total` with `reason` label.** Current `MODEL_SWAP_TOTAL` has labels `from_model`, `to_model` but no `reason`. Add `reason` to the label list. Update `record_model_swap()` signature to accept `reason: str = "scheduler_pick"`.

   ```python
   MODEL_SWAP_TOTAL = Counter(
       "bastion_model_swap_total",
       "Total model transitions; reason in {scheduler_pick, affinity_miss, eviction}",
       labelnames=["from_model", "to_model", "reason"],
   )

   def record_model_swap(
       from_model: str | None,
       to_model: str,
       reason: str = "scheduler_pick",
   ) -> None:
       MODEL_SWAP_TOTAL.labels(
           from_model=from_model or "_none",
           to_model=to_model,
           reason=reason,
       ).inc()
   ```

2. **Add `bastion_request_queue_wait_seconds` Histogram.** Current `QUEUE_WAIT_TIME` metric is named `bastion_queue_wait_seconds` and uses labels `model`, `tier`. Rename to `bastion_request_queue_wait_seconds`; relabel `tier` -> `priority` to match spec. This is a breaking rename — it must land before the schema-freeze tag.

   ```python
   REQUEST_QUEUE_WAIT = Histogram(
       "bastion_request_queue_wait_seconds",
       "Time a request waited in the affinity queue before dispatch",
       labelnames=["priority", "model"],
       buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
   )

   def record_queue_wait(model: str, priority: str, wait_seconds: float) -> None:
       REQUEST_QUEUE_WAIT.labels(priority=priority, model=model).observe(wait_seconds)
   ```

   Update all call sites. Current call in `middleware.py` uses `record_queue_wait(model=..., tier=...)` — change `tier` kwarg to `priority`.

3. **Add `bastion_vram_used_mb` Gauge.** Current `VRAM_USED_BYTES` is named differently and uses bytes. Add the MB gauge alongside (do not remove the bytes gauge — it may be in use elsewhere). Add helper `update_vram_used_mb(gpu_index: str, mb: float)`.

   ```python
   VRAM_USED_MB = Gauge(
       "bastion_vram_used_mb",
       "VRAM used in megabytes as reported by nvidia-smi / Ollama fusion",
       labelnames=["gpu_index"],
   )

   def update_vram_used_mb(gpu_index: str, mb: float) -> None:
       VRAM_USED_MB.labels(gpu_index=gpu_index).set(mb)
   ```

4. **Add `bastion_thrashing_detector_halt_total` Counter.** Does not exist yet.

   ```python
   THRASHING_DETECTOR_HALT_TOTAL = Counter(
       "bastion_thrashing_detector_halt_total",
       "Cumulative thrashing verdict transitions per agent (WARNED, HALTED)",
       labelnames=["agent_id", "verdict"],
   )

   def record_thrashing_verdict(agent_id: str, verdict: str) -> None:
       THRASHING_DETECTOR_HALT_TOTAL.labels(
           agent_id=agent_id,
           verdict=verdict,
       ).inc()
   ```

5. **Add `bastion_concurrent_requests_active` Gauge.** Does not exist yet.

   ```python
   CONCURRENT_REQUESTS_ACTIVE = Gauge(
       "bastion_concurrent_requests_active",
       "Number of inference requests currently inflight to Ollama",
   )

   def set_concurrent_requests_active(count: int) -> None:
       CONCURRENT_REQUESTS_ACTIVE.set(count)
   ```

6. **Export new names** in `__all__`.

#### `src/bastion/scheduler.py` (modify)

- Import `record_model_swap`, `set_concurrent_requests_active` from `bastion.metrics`.
- In `_handle_swap_dispatch()`, after `audit.emit(audit.EVENT_SWAP, ...)` at line ~525, add:
  ```python
  from bastion.metrics import record_model_swap
  record_model_swap(
      from_model=from_model,
      to_model=candidate.model,
      reason="scheduler_pick",
  )
  ```
  Use `reason="eviction"` in the eviction path (`_unload_model` call site).
- In the scheduling loop, call `set_concurrent_requests_active(self._inflight_count_fn())` after each dispatch decision. The `_inflight_count_fn` is already wired in `__init__`.

#### `src/bastion/thrashing.py` (modify)

- Import `record_thrashing_verdict` from `bastion.metrics`.
- In `check()`, after `self._total_halts += 1` (line ~148), add:
  ```python
  from bastion.metrics import record_thrashing_verdict
  record_thrashing_verdict(agent_id=agent_id, verdict="HALTED")
  ```
- After `self._total_warnings += 1` in the WARN branch (line ~151), add:
  ```python
  record_thrashing_verdict(agent_id=agent_id, verdict="WARNED")
  ```
  Note: halt also increments warnings in the current code — do not double-emit. Emit `WARNED` only in the pure-warn branch; emit `HALTED` only in the halt branch.

#### `src/bastion/vram.py` (modify)

- Import `update_vram_used_mb` from `bastion.metrics`.
- In `VRAMTracker.get_loaded_vram_gb()` or the ledger refresh path, after computing the nvidia-smi/Ollama fusion result, call:
  ```python
  from bastion.metrics import update_vram_used_mb
  update_vram_used_mb(gpu_index="0", mb=used_mb)
  ```
  The `used_mb` value is the same quantity already computed for the vram ledger. No new GPU queries.

#### `src/bastion/middleware.py` (modify)

- The queue-wait emit currently records to `QUEUE_WAIT_TIME` (old name). After E1, call sites must use the renamed `record_queue_wait(model=..., priority=...)` helper. The middleware does not currently call `record_queue_wait` — it only calls `record_request`. The queue wait should be recorded at the point a request exits the queue and enters dispatch. That call site is in `scheduler.py`'s dispatch path, not the middleware. Add the call there:
  ```python
  from bastion.metrics import record_queue_wait
  record_queue_wait(
      model=candidate.model,
      priority=candidate.tier,
      wait_seconds=time.time() - candidate.enqueue_time,
  )
  ```
  Verify `QueuedRequest` has an `enqueue_time` field (it should — check `models.py`). If missing, add it in `models.py`.

#### `config/broker.yaml` (modify)

Add `observability:` section after the existing `telemetry:` block:

```yaml
# ── Observability (Prometheus / Grafana) ───────────────────────────
# Scrape hint for prometheus.yml generation. BASTION itself does not
# read this — it is documentation for operators and the docker-compose stack.
observability:
  metrics_path: "/metrics"
  scrape_interval_seconds: 15
  # BASTION_OTLP_ENDPOINT env var activates OTLP export in telemetry.py.
  # Set to empty string (default) to disable. See WT-E5.
  otlp_endpoint_env: "BASTION_OTLP_ENDPOINT"
```

### Test strategy

**Existing test to update:** `tests/test_metrics.py`

- `TestMetricsIncrement::test_model_swap_counter_with_swap` — update call signature to include `reason` kwarg.
- `TestMetricsIncrement::test_queue_wait_time_histogram` — update kwarg from `tier=` to `priority=`.
- Add `TestMetricsIncrement::test_vram_used_mb_gauge` — call `update_vram_used_mb("0", 8192.0)`, assert no exception.
- Add `TestMetricsIncrement::test_thrashing_halt_counter` — call `record_thrashing_verdict("agent-1", "HALTED")`, `record_thrashing_verdict("agent-1", "WARNED")`, assert no exception.
- Add `TestMetricsIncrement::test_concurrent_requests_active_gauge` — call `set_concurrent_requests_active(3)`, assert no exception.

**New test:** `tests/test_metrics_schema_freeze.py`

```python
def test_five_public_metric_names_present():
    """Assert the five schema-frozen metric names exist and carry correct label keys."""
    from bastion.metrics import (
        CONCURRENT_REQUESTS_ACTIVE,
        MODEL_SWAP_TOTAL,
        REQUEST_QUEUE_WAIT,
        THRASHING_DETECTOR_HALT_TOTAL,
        VRAM_USED_MB,
        get_metrics_text,
    )
    # Emit one observation for each
    MODEL_SWAP_TOTAL.labels(from_model="_none", to_model="qwen3:8b", reason="scheduler_pick").inc()
    REQUEST_QUEUE_WAIT.labels(priority="agent", model="qwen3:8b").observe(0.1)
    VRAM_USED_MB.labels(gpu_index="0").set(8192)
    THRASHING_DETECTOR_HALT_TOTAL.labels(agent_id="test-agent", verdict="HALTED").inc()
    CONCURRENT_REQUESTS_ACTIVE.set(1)
    text = get_metrics_text().decode()
    for name in [
        "bastion_model_swap_total",
        "bastion_request_queue_wait_seconds",
        "bastion_vram_used_mb",
        "bastion_thrashing_detector_halt_total",
        "bastion_concurrent_requests_active",
    ]:
        assert name in text, f"Schema-frozen metric {name!r} missing from exposition"
```

This test is the CI enforcement of the schema-freeze social contract.

**Existing tests to check for regressions:** `tests/test_thrashing.py`, `tests/test_scheduler.py`, `tests/test_vram.py` — run the full suite; no new assertions needed there unless call signatures break.

---

## WT-E2 — Ship `dashboards/grafana/bastion-overview.json`

**Branch:** `feat/vision-c-grafana-overview`
**Forks from:** post-E1 merge commit (after `v0.3-schema-freeze` tag)

### File changes

#### `dashboards/grafana/bastion-overview.json` (create)

Grafana dashboard JSON with the following panels. Use Grafana's native JSON format (schema version 38+). The UID must be `bastion-overview` (matching the route in acceptance criterion 3).

Panel layout (12-column grid, 8-row canvas):

| Panel | Row | Type | Query |
|---|---|---|---|
| VRAM Used (MB) | 1 | Time series | `bastion_vram_used_mb{gpu_index="0"}` |
| Concurrent Requests | 1 | Stat | `bastion_concurrent_requests_active` |
| Model Swaps / 5m | 2 | Time series | `rate(bastion_model_swap_total[5m])` |
| Queue Wait p99 | 2 | Time series | `histogram_quantile(0.99, rate(bastion_request_queue_wait_seconds_bucket[5m]))` |
| Thrashing Halts | 3 | Stat | `sum(bastion_thrashing_detector_halt_total{verdict="HALTED"})` |
| Swap Heatmap | 3 | Table | `topk(5, sum by (from_model, to_model) (bastion_model_swap_total))` |

Dashboard-level variables:
- `$datasource` — Prometheus datasource variable (required for multi-datasource installs)

The JSON file must have `"uid": "bastion-overview"` so the provisioning URL `/d/bastion-overview` resolves correctly.

Concrete datasource reference in all panels: `"datasource": { "type": "prometheus", "uid": "${datasource}" }`.

### Test strategy

No Python test. The acceptance check is manual: after `docker compose up`, `curl http://localhost:3000/d/bastion-overview` returns 200. A shallow automated check can be added to CI with `python -c "import json; d=json.load(open('dashboards/grafana/bastion-overview.json')); assert d['uid']=='bastion-overview'"`.

Add that one-liner to a new `tests/test_grafana_dashboard.py`:

```python
def test_dashboard_uid_is_bastion_overview():
    import json, pathlib
    d = json.loads(pathlib.Path("dashboards/grafana/bastion-overview.json").read_text())
    assert d["uid"] == "bastion-overview"

def test_dashboard_has_required_panels():
    import json, pathlib
    d = json.loads(pathlib.Path("dashboards/grafana/bastion-overview.json").read_text())
    titles = [p["title"] for p in d.get("panels", [])]
    for required in ["VRAM Used", "Concurrent Requests", "Model Swaps", "Queue Wait"]:
        assert any(required in t for t in titles), f"Panel containing {required!r} missing"
```

---

## WT-E3 — Ship `docker-compose.yml` and config dirs

**Branch:** `feat/vision-c-docker-compose`
**Forks from:** post-E2 merge commit

### File changes

#### `docker-compose.yml` (create, top-level)

```yaml
version: "3.9"

# BASTION observability stack.
# Prerequisites: BASTION broker running on the host at port 11434.
# Usage: docker compose up -d
# Grafana: http://localhost:3000 (admin/admin)
# Prometheus: http://localhost:9090

services:
  prometheus:
    image: prom/prometheus:v2.52.0
    container_name: bastion-prometheus
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.retention.time=30d"
    ports:
      - "9090:9090"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped

  grafana:
    image: grafana/grafana:10.4.2
    container_name: bastion-grafana
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./dashboards:/var/lib/grafana/dashboards:ro
      - grafana_data:/var/lib/grafana
    environment:
      GF_SECURITY_ADMIN_PASSWORD: admin
      GF_USERS_ALLOW_SIGN_UP: "false"
    ports:
      - "3000:3000"
    depends_on:
      - prometheus
    restart: unless-stopped

  alertmanager:
    image: prom/alertmanager:v0.27.0
    container_name: bastion-alertmanager
    volumes:
      - ./alerting/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
      - ./alerting/bastion.rules.yml:/etc/alertmanager/bastion.rules.yml:ro
    command:
      - "--config.file=/etc/alertmanager/alertmanager.yml"
    ports:
      - "9093:9093"
    restart: unless-stopped

volumes:
  prometheus_data:
  grafana_data:
```

#### `prometheus/prometheus.yml` (create)

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "/etc/alertmanager/bastion.rules.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]

scrape_configs:
  - job_name: "bastion"
    # On Linux Docker Engine, host.docker.internal resolves via extra_hosts.
    # If that fails, override with: BASTION_HOST=172.17.0.1 docker compose up
    static_configs:
      - targets: ["host.docker.internal:11434"]
    metrics_path: /metrics
```

#### `grafana/provisioning/datasources/prometheus.yaml` (create)

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    uid: prometheus
    jsonData:
      timeInterval: "15s"
```

#### `grafana/provisioning/dashboards/bastion.yaml` (create)

```yaml
apiVersion: 1

providers:
  - name: bastion
    type: file
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
      foldersFromFilesStructure: false
```

#### `alerting/alertmanager.yml` (create)

Minimal Alertmanager config. Operators override `receivers` for their notification channel.

```yaml
route:
  group_by: ["alertname"]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  receiver: "bastion-webhook"

receivers:
  - name: "bastion-webhook"
    webhook_configs:
      - url: "http://host.docker.internal:11434/broker/control/restart"
        http_config:
          authorization:
            type: Bearer
            # Set BASTION_RESTART_TOKEN or replace inline:
            credentials: "${BASTION_RESTART_TOKEN}"
        send_resolved: false

# Replace bastion-webhook with your preferred receiver (PagerDuty, Slack, etc.)
# and leave the webhook as a secondary receiver or remove it if unused.
```

#### `alerting/bastion.rules.yml` (create — stub; WT-E4 populates the rule)

```yaml
groups: []
# Populated by WT-E4
```

### Test strategy

```python
# tests/test_docker_compose.py
import yaml, pathlib

def test_docker_compose_parses():
    d = yaml.safe_load(pathlib.Path("docker-compose.yml").read_text())
    assert "services" in d
    for svc in ["prometheus", "grafana", "alertmanager"]:
        assert svc in d["services"]

def test_prometheus_config_parses():
    d = yaml.safe_load(pathlib.Path("prometheus/prometheus.yml").read_text())
    jobs = [c["job_name"] for c in d["scrape_configs"]]
    assert "bastion" in jobs

def test_datasource_provisioning_parses():
    d = yaml.safe_load(
        pathlib.Path("grafana/provisioning/datasources/prometheus.yaml").read_text()
    )
    names = [ds["name"] for ds in d["datasources"]]
    assert "Prometheus" in names
```

---

## WT-E4 — Alertmanager rule + `/broker/control/restart` webhook

**Branch:** `feat/vision-c-alertmanager-rule`
**Forks from:** post-E3 merge commit
**Also requires:** WT-C-Y (the Phase B restart endpoint) to be merged before E4 merges to main. If WT-C-Y is not yet merged, E4 can be developed against a stub; add a gating note in the PR description.

### File changes

#### `alerting/bastion.rules.yml` (modify — replaces stub from E3)

```yaml
groups:
  - name: bastion_thrashing
    interval: 30s
    rules:
      - alert: BastionThrashingHalt
        expr: sum(increase(bastion_thrashing_detector_halt_total{verdict="HALTED"}[5m])) > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "BASTION thrashing detector fired a HALT verdict"
          description: >
            At least one agent has been halted for swap thrashing in the last 5 minutes.
            Alertmanager will POST /broker/control/restart (idempotent).
            repeat_interval is 4h — this alert will not re-fire for 4 hours.
```

The expression uses `increase(...[5m])` rather than the raw counter, so it fires on new halts within a 5-minute window, not on the cumulative value being nonzero forever. This addresses the failure mode raised by both the adversarial-failure-mode-auditor and sre-incident-operator-3am: a bare `> 0` on a Counter that never decrements would fire permanently after the first halt.

#### `alerting/alertmanager.yml` (verify, not modify)

The `repeat_interval: 4h` committed in WT-E3 is the idempotency guard. WT-E4 does not need to modify it, but the engineer must verify the file is correct before merging E4.

#### `src/bastion/server.py` (modify — if `/broker/control/restart` not yet present)

If WT-C-Y has not landed, add a minimal restart endpoint stub that returns 200 immediately:

```python
@router.post("/broker/control/restart", dependencies=[Depends(require_auth)])
async def restart_broker() -> dict[str, str]:
    """Idempotent restart signal. Sets a restart-pending flag; the watchdog acts on it."""
    # TODO: wire to actual restart logic when WT-C-Y merges
    return {"status": "restart_queued"}
```

The endpoint must be idempotent — two concurrent POSTs must not produce two restarts. The flag pattern or a debounce with `asyncio.Lock` is sufficient.

### Test strategy

```python
# tests/test_alerting_rules.py
import yaml, pathlib

def test_rules_file_parses():
    d = yaml.safe_load(pathlib.Path("alerting/bastion.rules.yml").read_text())
    assert "groups" in d
    assert len(d["groups"]) > 0

def test_thrashing_halt_rule_present():
    d = yaml.safe_load(pathlib.Path("alerting/bastion.rules.yml").read_text())
    rules = [r["alert"] for g in d["groups"] for r in g.get("rules", [])]
    assert "BastionThrashingHalt" in rules

def test_thrashing_halt_uses_increase_not_raw_counter():
    d = yaml.safe_load(pathlib.Path("alerting/bastion.rules.yml").read_text())
    for g in d["groups"]:
        for r in g.get("rules", []):
            if r.get("alert") == "BastionThrashingHalt":
                assert "increase(" in r["expr"], (
                    "Alert must use increase() not raw counter to avoid permanent firing"
                )

def test_alertmanager_config_has_repeat_interval():
    d = yaml.safe_load(pathlib.Path("alerting/alertmanager.yml").read_text())
    assert d["route"]["repeat_interval"] == "4h"
```

For the restart endpoint, add to the existing server test suite:

```python
# In tests/test_server_status.py or a new tests/test_restart_endpoint.py
async def test_restart_endpoint_returns_200_with_auth(client_with_auth):
    resp = await client_with_auth.post("/broker/control/restart")
    assert resp.status_code == 200

async def test_restart_endpoint_is_idempotent(client_with_auth):
    r1 = await client_with_auth.post("/broker/control/restart")
    r2 = await client_with_auth.post("/broker/control/restart")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Must not raise or 500 on duplicate call
```

---

## WT-E5 — OTLP export via `BASTION_OTLP_ENDPOINT`

**Branch:** `feat/vision-c-otlp-export`
**Forks from:** post-E1 merge commit (opens simultaneously with E2, after the freeze tag)

### File changes

#### `src/bastion/telemetry.py` (modify)

The current `init_telemetry(config)` reads `config.endpoint` from `BrokerConfig.telemetry.endpoint`. Add env-var override: if `BASTION_OTLP_ENDPOINT` is set in the environment, it takes precedence over `config.endpoint`.

```python
import os

def init_telemetry(config: Any) -> None:
    # ...existing enabled/OTEL_AVAILABLE checks...

    # Env var override: BASTION_OTLP_ENDPOINT wins over config file
    env_endpoint = os.environ.get("BASTION_OTLP_ENDPOINT", "").strip()
    if env_endpoint and config.exporter.lower() != "otlp":
        # Env var implies OTLP even if config says "none"
        effective_exporter = "otlp"
        effective_endpoint = env_endpoint
    else:
        effective_exporter = config.exporter.lower().strip()
        effective_endpoint = env_endpoint or config.endpoint or "http://localhost:4317"

    # ...rest of exporter dispatch using effective_exporter and effective_endpoint...
```

Key constraint: when `BASTION_OTLP_ENDPOINT` is unset and `config.exporter` is `"none"`, the function must exit cleanly with zero startup cost. The existing no-op path already handles this; do not break it.

When the OTLP endpoint is set but the `opentelemetry-exporter-otlp` package is missing, log a warning and set `_enabled = False`. Do not block startup with a TCP timeout.

#### `config/broker.yaml` (modify)

Add a comment to the existing `telemetry:` section noting the env override:

```yaml
telemetry:
  enabled: false
  exporter: "none"           # "none", "console", "otlp"
  endpoint: ""               # OTLP endpoint. Also settable via BASTION_OTLP_ENDPOINT env var.
                             # BASTION_OTLP_ENDPOINT env var takes precedence over this field.
  service_name: "bastion"
```

Also confirm the `observability:` section added in E1 is present (it will be, since E5 forks from post-E1 main).

### Test strategy

Update `tests/test_telemetry.py`:

```python
def test_otlp_endpoint_env_overrides_config(monkeypatch):
    """BASTION_OTLP_ENDPOINT env var should override config.endpoint."""
    monkeypatch.setenv("BASTION_OTLP_ENDPOINT", "http://collector:4318")
    from bastion import telemetry
    # Build a minimal config with exporter=none
    cfg = MagicMock()
    cfg.enabled = True
    cfg.exporter = "none"
    cfg.endpoint = ""
    cfg.service_name = "bastion-test"
    # Should activate OTLP path (will fail gracefully if package missing)
    # We only assert it does not raise and does not block
    import bastion.telemetry as tel
    # Re-init with env set; if _OTLP_AVAILABLE is False, should log warning not crash
    tel.init_telemetry(cfg)  # no exception

def test_unset_otlp_endpoint_is_noop(monkeypatch):
    """When BASTION_OTLP_ENDPOINT is unset and config is disabled, startup is instant."""
    monkeypatch.delenv("BASTION_OTLP_ENDPOINT", raising=False)
    import bastion.telemetry as tel
    cfg = MagicMock()
    cfg.enabled = False
    cfg.exporter = "none"
    cfg.endpoint = ""
    cfg.service_name = "bastion-test"
    tel.init_telemetry(cfg)
    assert not tel.is_enabled()
```

---

## Risk Register

### R1 — Cardinality explosion (`bastion_model_swap_total`)

| | |
|---|---|
| What could go wrong | Operator adds a 20th model to `broker.yaml`. With `from_model × to_model × reason`, cardinality = 20×20×3 = 1200 series. Prometheus TSDB is fine but alert evaluation slows. |
| Detection signal | Prometheus `/api/v1/status/tsdb` reports high `headChunks`; scrape duration > 5s. |
| Mitigation | WT-E1 test `test_five_public_metric_names_present` establishes the label set. Add a comment in `metrics.py` noting the cardinality formula. No runtime enforcement is feasible without changing the Prometheus model; documentation is the bound here. The `reason` enum is code-controlled (3 values), which caps the multiplicative factor. |

### R2 — Alertmanager idempotency storm

| | |
|---|---|
| What could go wrong | `thrashing_detector_halt_total` is a Counter. A raw `> 0` expression fires permanently after first halt. Even with `repeat_interval: 4h`, if Alertmanager restarts or the alert group resets, the rule re-fires immediately on the permanent nonzero counter. |
| Detection signal | Alertmanager UI shows alert continuously in FIRING state even hours after thrashing stopped. `/broker/control/restart` receives repeated POSTs. |
| Mitigation | Use `increase(...[5m]) > 0` (not the raw counter). This fires only when new halts occur within the 5-minute window, self-resolving when thrashing stops. Committed in `alerting/bastion.rules.yml` by WT-E4. Restart endpoint must be idempotent (in-flight flag). |

### R3 — `agent_id` cardinality explosion (`bastion_thrashing_detector_halt_total`)

| | |
|---|---|
| What could go wrong | If agent_id values are task UUIDs or ephemeral IP addresses, this metric accumulates unbounded series. Prometheus OOM at 2am is silent until scrape fails. |
| Detection signal | Prometheus TSDB head size grows without bound; `/api/v1/label/agent_id/values` returns thousands of values. |
| Mitigation | Define `agent_id` in WT-E1 as: "the value of the `X-Agent-Id` header if present; otherwise the source IP truncated to /24 prefix." Document this in `metrics.py`. Source IPs are bounded by the operator's network size. Task UUIDs must never be used as the agent_id label value. This must be enforced at the call site in `thrashing.py`. |

### R4 — `host.docker.internal` fails on Linux Docker Engine

| | |
|---|---|
| What could go wrong | `prometheus.yml` targets `host.docker.internal:11434`. On Linux Docker Engine (non-Desktop), this hostname does not resolve by default. Prometheus silently shows the target as `DOWN`. |
| Detection signal | Prometheus `/targets` page shows bastion target with `connection refused` or `no such host`. |
| Mitigation | `docker-compose.yml` adds `extra_hosts: ["host.docker.internal:host-gateway"]` to the Prometheus service. Document the fallback: if still broken, set `BASTION_HOST=172.17.0.1` and edit `prometheus.yml` manually, or use `network_mode: host` for Prometheus. Add a comment in `prometheus.yml` explaining the Linux fallback. |

### R5 — OTLP startup latency on misconfigured endpoint

| | |
|---|---|
| What could go wrong | `BASTION_OTLP_ENDPOINT` is set to an unreachable host. OTLP's `OTLPSpanExporter` with gRPC may attempt a connection at startup and block for TCP timeout (30s default). |
| Detection signal | BASTION startup takes >30s; logs show OTLP connection attempts. |
| Mitigation | Use `BatchSpanProcessor` (already used in existing `telemetry.py`), which buffers spans and exports asynchronously. Initial connection failure is non-blocking. Log a startup warning "OTLP endpoint configured but unreachable — traces will be buffered until connection succeeds." |

### R6 — Schema freeze is unenforceable without CI gate

| | |
|---|---|
| What could go wrong | A future refactor renames `bastion_model_swap_total` to `bastion_swap_total`. The rename is a breaking change for existing Grafana dashboards and Alertmanager rules but the CI suite passes because no test asserts the metric names. |
| Detection signal | `curl /metrics | grep bastion_model_swap_total` returns nothing after a deploy. |
| Mitigation | `tests/test_metrics_schema_freeze.py` added in WT-E1 asserts all five metric names exist in exposition output. This test is the CI canary for schema drift. |

### R7 — E3/E4 alerting file conflict if merge order is violated

| | |
|---|---|
| What could go wrong | WT-E4 is opened before E3 merges. E4's branch contains its own copy of `alerting/bastion.rules.yml`. When E3 later merges, three-way merge drops one side's content silently if the diff context overlaps. |
| Detection signal | After merge, `alerting/bastion.rules.yml` has `groups: []` instead of the E4 rule. |
| Mitigation | Enforced by the merge order in this plan: E4 forks from the post-E3 commit, not from main at E4-open time. PR template for E4 must include a checklist: "E3 is merged." |

---

## Non-Obvious Tradeoffs

### 1. Prometheus cardinality vs. label richness

**Decision:** Keep `from_model × to_model × reason` for `bastion_model_swap_total`. Drop `agent_id` from this metric (it is on `thrashing_detector_halt_total` only). The swap metric's cardinality is bounded by the model registry (17 models in the current `broker.yaml` = 17×17×3 = 867 series worst case, well within Prometheus defaults). The alternative — dropping `from_model`/`to_model` — eliminates the ability to identify which model pair causes thrashing, which is the primary diagnostic value.

### 2. `host.docker.internal` vs. container networking for `/metrics` scrape

**Decision:** Use `host.docker.internal` with `extra_hosts: host-gateway` in the compose file. The alternative (putting BASTION itself in the compose network) would require operators to Dockerize BASTION, contradicting the goal of "drop-in beside an existing BASTION install." The `host-gateway` mechanism works on Linux Docker Engine 20.10+ and Docker Desktop. Older Docker versions need the documented fallback (`172.17.0.1`).

### 3. Alertmanager idempotency

**Decision:** Use `increase(...[5m]) > 0` in the rule expression, not the raw counter. This self-resolves when thrashing stops (the increase window empties). `repeat_interval: 4h` in `alertmanager.yml` provides the additional guard against notification floods. The restart webhook's idempotency (in-flight flag on the endpoint) is the third layer. All three layers are required; any single layer alone is insufficient.

### 4. OTLP collector resilience

**Decision:** Honor `BASTION_OTLP_ENDPOINT` as an env-var override that activates OTLP even when `config.exporter = "none"`. When the endpoint is unset, the path is a pure no-op with zero startup cost. When set but unreachable, `BatchSpanProcessor` handles retries asynchronously — startup is not blocked. The operator is responsible for running a collector; BASTION does not ship one. The docker-compose stack does not include a collector because the OTLP destination is operator-specific.

### 5. Grafana datasource provisioning

**Decision:** Commit `grafana/provisioning/datasources/prometheus.yaml`. The alternative — leaving datasource configuration to the operator — breaks the `docker compose up` turnkey promise. The datasource file is safe to commit because it contains no credentials and references only the internal Prometheus service name. Operators who already have Grafana can disable auto-provisioning by removing the volume mount; the committed file does not conflict with existing datasource configurations by default (it provisions under the name "Prometheus" only if no datasource with that name exists).

---

## Acceptance Criteria Mapping

| # | Criterion | Worktree(s) |
|---|---|---|
| AC1 | `curl /metrics \| grep -c '^bastion_'` returns ≥ 5 | WT-E1 |
| AC2 | `docker compose up` brings stack healthy in 30s | WT-E3 |
| AC3 | `http://localhost:3000/d/bastion-overview` shows non-empty panels after 60s warm-up | WT-E2, WT-E3 |
| AC4 | Synthetic thrashing event fires alert; webhook POSTs restart endpoint with bearer token | WT-E1 (metric emit), WT-E4 (rule + endpoint) |
| AC5 | With `BASTION_OTLP_ENDPOINT` set, traces appear for task submit, queue wait, model swap | WT-E5 |
| AC6 | `python -m pytest tests/ -v` passes (no regressions) | All worktrees |
| AC7 | TUI renders correctly with new metric emit sites | WT-E1 (must not import TUI; metrics.py must remain importable without Textual) |

---

## Dissent Log

The four lenses reached substantial consensus on the corrected merge order and the `increase()` fix for the alert rule. Recorded disagreements:

**On merge order (E5 parallelism):** The spec says "E5 is parallel-safe." The parallel-merge-safety-engineer disagrees: E1 and E5 both touch `config/broker.yaml`. This plan follows the engineer's recommendation: open E5 after E1 merges. The synthesizer did not contest this. No dissent from the adversarial or SRE lenses on this point.

**On `agent_id` cardinality:** The SRE-incident-operator-3am and adversarial-failure-mode-auditor both independently flagged `agent_id` as potentially unbounded if task UUIDs are used. The spec does not constrain this. This plan adds the constraint (registered agent names or /24 IP prefix) as a hard implementation requirement in WT-E1, which the spec does not mention. This is an addition to the spec, not a deviation.

**On datasource provisioning:** The spec says "Create `grafana/provisioning/dashboards/bastion.yaml`" but does not mention the datasource provisioning file. The synthesizer and SRE-3am both argued that omitting the datasource file breaks the turnkey promise. This plan adds `grafana/provisioning/datasources/prometheus.yaml` in WT-E3. This is an addition to the spec's file list.

**On alert expression:** The spec says "thrashing_detector_halt_total > 0." The adversarial and SRE lenses both identified this as permanently-firing after first halt. This plan uses `increase(bastion_thrashing_detector_halt_total{verdict="HALTED"}[5m]) > 0` instead. This is a correction to the spec's stated alert expression.

**The original council R4 fragment** (recovered from `council.log`) began: "E5 is NOT fully parallel. Both E1 and E5 write `config/broker.yaml`. Opening E5 before E1 merges produces a [conflict]." This plan reflects that finding fully.

---

## Executing This Plan

Run the full test suite before opening each worktree and after each merge:

```
/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/ -v
```

Worktree commands (example for E1):

```bash
git worktree add ../bastion-e1 -b feat/vision-c-metrics-schema
cd ../bastion-e1
# implement WT-E1 changes
/home/cyprian/miniforge3/envs/phenotype/bin/python -m pytest tests/ -v
git add src/bastion/metrics.py src/bastion/scheduler.py src/bastion/thrashing.py \
    src/bastion/vram.py src/bastion/middleware.py config/broker.yaml \
    tests/test_metrics.py tests/test_metrics_schema_freeze.py
git commit -m "feat(E1): schema-freeze 5 Vision C metrics; add reason label, rename queue-wait, add vram/thrashing/concurrent"
```

After merging E1 to main:

```bash
git tag v0.3-schema-freeze
git push origin v0.3-schema-freeze
```

Only then open E2 and E5 worktrees.
