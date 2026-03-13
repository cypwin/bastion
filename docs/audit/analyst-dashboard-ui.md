# Dashboard & UI Analyst Report -- BASTION

**Generated**: 2026-03-13
**Analyst**: Dashboard & UI Analyst (Claude Opus 4.6)
**Scope**: Textual TUI dashboard (dashboard.py), admin API surface consumed by the dashboard, visualization gaps, and future UI opportunities
**Key Files**: `src/bastion/dashboard.py` (2159 lines), `src/bastion/server.py` (1561 lines)
**Scout Dependencies**: Code Cartography, API Surface, Data Model reports

---

## Executive Summary

BASTION's dashboard is a **mature, well-isolated Textual TUI** with 15 visual widgets (14 data panels + 1 status bar), 6 modal dialogs, and 11 keyboard shortcuts. It connects to BASTION purely over HTTP -- zero internal imports -- which is an exemplary architectural decision that enables the dashboard to run as a separate process, potentially on a different machine.

**Key Findings**:

- The dashboard consumes **6 of 23 admin API endpoints** -- 17 endpoints go unused
- **7 critical data dimensions** available via API are not visualized (intents, config, residency, inflight details, session profiles, Prometheus metrics, scheduler diagnostics)
- A2A task streams (SSE via `/a2a/tasks/{id}/stream`) are **not consumed** despite the A2A panel existing
- The audit stream panel **fakes audit data** by transforming recent requests rather than reading real audit events
- **No web-based alternative** exists, limiting remote access (the TUI requires SSH)
- The dashboard has **no historical persistence** -- all trend data (sparklines) is lost on restart
- **Fan control and GPU process kill** are the most advanced interactive features, going beyond monitoring into active GPU management

---

## 1. Panel Inventory: What 15 Widgets Exist and What Each Shows

The dashboard is organized in a three-column layout with a status bar at top and footer at bottom.

### Status Bar (`StatusBar`) -- Top Dock
- **Data shown**: Connection indicator (`[*]`/`[X]`), `STALE` badge, GPU temperature, VRAM usage (GB), scheduler state, current time, last successful poll time
- **Data source**: Composite from `/broker/status` response
- **Color coding**: Temperature (green < 50C, yellow < 70C, orange < 80C, red >= 80C), VRAM utilization percentage, scheduler state

### Left Column

| # | Panel | Widget Class | Data Shown | API Source |
|---|-------|-------------|------------|------------|
| 1 | **GPU** | `GPUPanel` | Temperature, VRAM bar (used/total GB), utilization %, power draw (W), safety status (OK/UNSAFE), VRAM sparkline (60 samples), temperature sparkline (60 samples) | `/broker/status` -> `gpu` |
| 2 | **Models Loaded** | `ModelsPanel` | List of loaded models with VRAM per model, active model highlight (`*` prefix) | `/broker/status` -> `loaded_models`, `current_model` |
| 3 | **Safety Limits Bar** | `SafetyLimitsBar` | Horizontal VRAM budget bar (26 GB budget), percentage-based coloring | `/broker/status` -> `gpu.vram_used_mb` |
| 4 | **Alerts** | `AlertPanel` | Severity-tiered alert list (INFO/WARN/CRIT) with auto-dismiss timers | Computed client-side from thresholds |

### Middle Column

| # | Panel | Widget Class | Data Shown | API Source |
|---|-------|-------------|------------|------------|
| 5 | **Queue** | `QueuePanel` | Per-model queue depth, total depth, scheduler state, in-flight count, stall reason + cooldown remaining, queue depth sparkline (60 samples) | `/broker/status` + `/broker/queue` |
| 6 | **Scheduler** | `SchedulerPanel` | Uptime (formatted), requests served, model swaps, state (running/draining) | `/broker/status` |
| 7 | **Circuit Breaker** | `CircuitBreakerPanel` | Circuit state (closed/open/half_open), health (healthy/unhealthy), reason string, scheduler running status | `/broker/health` |
| 8 | **Watchdog** | `WatchdogPanel` | Ollama state, GPU state, Ollama latency (ms), GPU query latency (ms), consecutive failure counts, scheduler paused status, last check time | `/broker/watchdog` |
| 9 | **VRAM Ledger** | `VRAMLedgerPanel` | Total/safety/allocated/reserved/available VRAM (bytes -> GB/MB), active reservation count, utilization bar, individual reservations (model, VRAM, age, committed/pending) | `/broker/vram` |

### Right Column

| # | Panel | Widget Class | Data Shown | API Source |
|---|-------|-------------|------------|------------|
| 10 | **Request Trace** | `TracePanel` | Last 20 requests: timestamp, model, tier, queue wait (s), duration (s), HTTP status code | `/broker/recent` |
| 11 | **A2A Tasks** | `A2ATaskPanel` | Task counts (total, working, queued, done, failed), individual task details (ID, skill, state) -- up to 5 tasks | `/broker/status` -> `a2a_tasks`, `a2a_summary` |
| 12 | **Leases** | `LeasePanel` | Active lease count, per-lease details (ID, model, remaining requests, TTL countdown) -- up to 5 leases | `/broker/status` -> `leases` |
| 13 | **Audit Events** | `AuditStreamPanel` | Last 10 events: timestamp, event type, detail string (model, severity, status code, VRAM) | Synthesized from `/broker/recent` |
| 14 | **Connection** | `ConnectionPanel` | Connection status (CONNECTED/DISCONNECTED), error message, consecutive failure count, retry countdown, URL, last successful time | Client-side tracking |

### Additional Non-Panel Widget

| Widget | Class | Purpose |
|--------|-------|---------|
| **Footer** | Textual `Footer` | Shows keyboard binding hints |

---

## 2. Admin API Endpoints: Consumed vs. Unconsumed

### Endpoints the Dashboard DOES Consume (6/23)

| Endpoint | Method | Dashboard Method | Usage |
|----------|--------|-----------------|-------|
| `/broker/status` | GET | `BastionClient.poll()` | Primary data source for GPU, models, queue, scheduler, A2A tasks, leases |
| `/broker/queue` | GET | `BastionClient.get_queue()` | Stall diagnostics (stall_reason, cooldown_remaining, inflight_total) |
| `/broker/health` | GET | `BastionClient.get_health()` | Circuit breaker state, health status |
| `/broker/vram` | GET | `BastionClient.get_vram_ledger()` | VRAM Manager ledger (allocated/reserved/available) |
| `/broker/watchdog` | GET | `BastionClient.get_watchdog()` | Ollama + GPU health monitoring |
| `/broker/recent` | GET | `BastionClient.get_recent()` | Request trace viewer, also synthesized into audit events |

### Action Endpoints the Dashboard Calls (4)

| Endpoint | Method | Dashboard Method | Trigger |
|----------|--------|-----------------|---------|
| `/broker/preload` | POST | `BastionClient.post_preload()` | `[p]` key |
| `/broker/unload` | POST | `BastionClient.post_unload()` | `[u]` key |
| `/broker/drain` | POST | `BastionClient.post_drain()` | `[d]` key |
| `/broker/resume` | POST | `BastionClient.post_resume()` | `[d]` key (toggle) |

### Endpoints the Dashboard Does NOT Consume (13/23)

| Endpoint | What It Returns | Dashboard Impact |
|----------|----------------|------------------|
| `/broker/metrics` | Prometheus text exposition | Could power metric trend graphs |
| `/broker/livez` | Liveness probe (`ok`) | Could supplement connection panel |
| `/broker/readyz` | Readiness probe (checks scheduler, proxy, circuit breaker) | Could show readiness status prominently |
| `/broker/intent` | POST: Register intent declaration | No intent management UI |
| `/broker/intents` | GET: List active intents | Intent state invisible |
| `/broker/intent/{id}/complete` | POST: Complete intent | No intent lifecycle management |
| `/broker/intent/{id}` | DELETE: Cancel intent | No intent lifecycle management |
| `/broker/docs` | Swagger UI HTML | Not applicable for TUI |
| `/broker/redoc` | ReDoc HTML | Not applicable for TUI |
| `/broker/openapi.json` | OpenAPI schema | Not applicable for TUI |
| `/a2a/tasks/{id}/stream` | SSE task stream | Could show live A2A task progress |
| `/a2a/extended-card` | Extended agent card | Could show A2A capabilities |
| `/.well-known/agent-card.json` | Public agent card | Could show agent identity |

---

## 3. Data Available via API but Not Visualized

### 3.1 Active Intent Declarations (`/broker/intents`)

The intent API tracks which clients have declared upcoming model sequences and their resolved priorities. This data exists and is served but the dashboard has no panel for it. Visualizing intents would show operators what pipeline stages are in progress and what models to expect next.

### 3.2 Scheduler Diagnostics (Partially via `/broker/queue`)

The `/broker/queue` endpoint returns `stall_reason`, `stall_since`, `cooldown_remaining`, `pending_grants`, and per-model `inflight` counts. The dashboard consumes `stall_reason`, `cooldown_remaining`, and `inflight_total` but ignores:
- `stall_since` -- when the stall began (duration not shown)
- `pending_grants` -- how many requests are waiting for the scheduler to grant them
- Per-model `inflight` dict -- which specific models have active inferences

### 3.3 Session Profiles (Config Data, No Endpoint)

Session profiles configured in `broker.yaml` describe named model sequences with priorities. No endpoint exists to query them, and the dashboard cannot display pipeline templates.

### 3.4 Residency Cache State (No Endpoint)

The `ResidencyState` model tracks per-model VRAM usage, last refresh time, and cache staleness. The Data Model Scout notes this is "invisible to clients" -- no endpoint returns it, so the dashboard cannot show which models are truly co-resident vs. what the scheduler believes.

### 3.5 VRAM Budget Computation

`GPUConfig.max_vram_gb` (total minus headroom) is the actual scheduling budget but is never returned by any endpoint. The `SafetyLimitsBar` hardcodes `VRAM_BUDGET_GB = 26.0` instead of reading it from the API. If the configuration changes, the dashboard display will be wrong.

### 3.6 Scheduler Swap Rate State

The Data Model Scout identifies `_swap_rate_level` ("normal"/"warn"/"critical"), `_swap_timestamps` (rolling window), and cooldown computation as critical crash prevention state that is never surfaced. The dashboard cannot display whether the swap rate limiter is actively throttling.

### 3.7 TaskStore Backpressure Level

`TaskStore.stats()` returns `pressure_level` ("normal"/"pressure"/"overloaded") but no endpoint calls it. The A2A task panel shows task counts but cannot warn about approaching backpressure limits.

---

## 4. A2A Task Streams (SSE): Current State and Potential

### Current Implementation

The A2A task panel (`A2ATaskPanel`) displays task summary counts and up to 5 individual tasks by reading `a2a_tasks` and `a2a_summary` from the `/broker/status` response. This is poll-based with the global refresh interval (default 2 seconds).

### SSE Stream Endpoint Exists

`GET /a2a/tasks/{task_id}/stream` is fully implemented in server.py. It returns SSE events with:
- Task state transitions (submitted -> working -> completed)
- Heartbeat comments every 15 seconds
- Real-time artifact delivery for batch_infer (each prompt result as it arrives)

### Why It Is Not Used

The dashboard's `BastionClient` class does not have an SSE stream method. The Textual framework supports async workers that could consume SSE streams via `httpx.AsyncClient.stream()`, but this was never implemented.

### What SSE Integration Would Enable

1. **Real-time batch_infer progress**: A batch of 50 prompts could show a progress bar updating as each result arrives, rather than waiting for the full poll cycle.
2. **Instant state transitions**: Task state changes would appear immediately rather than with up to 2-second lag.
3. **Reduced polling overhead**: Tasks being actively monitored could use SSE instead of polling.

### Implementation Path

A `StreamingPanel` widget could:
1. Accept a task_id from user selection (click or keyboard)
2. Open an SSE connection via `httpx.AsyncClient.stream("GET", f"/a2a/tasks/{task_id}/stream")`
3. Parse `data: {json}\n\n` lines and update a scrollable log widget
4. Handle reconnection on connection loss
5. Close the stream when the task completes or the user dismisses it

---

## 5. Interactive Controls: Current and Potential

### Current Controls (11 Keyboard Bindings)

| Key | Action | Implementation | Safety |
|-----|--------|---------------|--------|
| `q` | Quit dashboard | Direct exit | None needed |
| `r` | Force refresh all panels | Resets exponential backoff, fires poll | Safe |
| `h` | Help overlay | Modal dialog with all bindings | Safe |
| `p` | Preload model | Modal: select from loaded + known models -> POST `/broker/preload` | VRAM budget check server-side |
| `u` | Unload model | Modal: select from loaded models -> POST `/broker/unload` | Confirmation modal |
| `d` | Toggle drain mode | Confirmation modal -> POST `/broker/drain` or `/broker/resume` | Confirmation required |
| `s` | Restart bastion.service | Confirmation modal -> `sudo systemctl restart bastion.service` | Confirmation + sudo required |
| `f` | Fan control | Modal: 30/50/70/90/100%/auto + auto-trigger toggle | Runs via sudo wrapper script |
| `g` | Kill GPU process | Modal: list nvidia-smi processes -> confirm kill (SIGTERM or SIGKILL) | Two-step confirmation |
| `a` | Focus A2A panel | Scrolls to A2A task panel | Safe |
| `c` | Focus circuit breaker | Scrolls to circuit breaker panel | Safe |

### Auto-Fan Trigger (Background Feature)

The dashboard runs an automatic fan control loop (`_check_auto_fan()`) that:
- Reads CPU temperature from `/sys/class/hwmon/` (k10temp for AMD, coretemp for Intel)
- When CPU >= 80C: sets GPU fan to 90%
- When CPU drops below 75C (hysteresis): resets GPU fan to auto
- Toggleable via `[f]` modal

### Potential Controls That Could Be Added

1. **Intent Management** (`i` key):
   - View active intents
   - Cancel/complete intents
   - Register ad-hoc intents
   - Requires: `/broker/intents`, `/broker/intent/{id}`, `/broker/intent/{id}/complete`

2. **A2A Task Management** (`t` key):
   - View task details (full artifacts)
   - Cancel running tasks
   - Subscribe to task SSE stream
   - Requires: `/a2a/tasks/{id}`, `/a2a/tasks/{id}/stream`, DELETE `/a2a/tasks/{id}`

3. **Lease Management** (`l` key):
   - View lease details (fencing token, expiry, idle timeout)
   - Release leases manually
   - Requires: DELETE `/a2a/leases/{id}`

4. **Config Inspection** (`x` key):
   - Display effective running configuration
   - Would require new `/broker/config` endpoint (does not exist yet)

5. **Metrics Export** (`m` key):
   - Parse and display Prometheus metrics in a formatted table
   - Requires: `/broker/metrics` with text parsing

6. **Queue Detail Drill-Down** (`Q` key):
   - Show per-request breakdown: model, age, effective priority, client info, tier
   - Would require new `/broker/queue/details` endpoint

7. **Audit Log Viewer** (`A` key):
   - View real audit events instead of synthesized ones
   - Would require new `/broker/audit` endpoint

---

## 6. Web-Based Dashboard Alternative

### Current Limitation

The Textual TUI requires a terminal. Remote access requires SSH, which introduces latency and limits access for non-technical operators. There is no web UI.

### Why a Web Dashboard Would Be Valuable

1. **Remote access without SSH**: Any browser can view the dashboard
2. **Mobile monitoring**: Check GPU status from a phone
3. **Team visibility**: Multiple people can view simultaneously
4. **Richer visualization**: Charts, graphs, and interactive elements that exceed terminal capabilities
5. **Notification integration**: Browser notifications for alerts

### Architectural Readiness

BASTION is **architecturally well-prepared** for a web dashboard because:
- The TUI already communicates via HTTP (zero internal imports)
- All data sources are REST endpoints with JSON responses
- SSE streaming is already implemented for A2A tasks
- The admin API has auth (Bearer tokens) for security
- FastAPI already serves OpenAPI docs at `/broker/docs`

### Missing Pieces for Web Dashboard

1. **CORS middleware**: The API Surface Scout identified this gap -- BASTION does not set CORS headers, so browser clients from different origins would be blocked. The fix is straightforward (add `CORSMiddleware`).

2. **SSE endpoint for live status**: Currently, the dashboard polls `/broker/status` every 2 seconds. A web dashboard would benefit from a server-push `GET /broker/status/stream` SSE endpoint to reduce HTTP overhead and latency.

3. **Static file serving**: FastAPI can serve static files, but BASTION would need a `StaticFiles` mount for a bundled web UI.

### Possible Implementation Approaches

| Approach | Complexity | Benefit |
|----------|-----------|---------|
| **Grafana + Prometheus** | Low (config only) | Leverages existing `/broker/metrics` endpoint. No code changes. Grafana provides dashboarding, alerting, and historical trends. |
| **Simple HTML + SSE** | Medium | Single-page HTML file served by BASTION itself. Uses EventSource API for live updates. No build system needed. |
| **React/Vue SPA** | High | Rich interactive UI with charts (Chart.js, D3). Requires build pipeline, but maximizes UX. |
| **Textual Web** | Medium | Textual's `--web` mode can serve the existing TUI as a web application via a browser. Requires `textual-serve` package. |

### Recommendation

The lowest-friction path is **Grafana + Prometheus** for metrics visualization (already has an endpoint), combined with **Textual Web** for the existing TUI. Textual's web serving capability (`textual-serve`) can expose the current TUI to browsers with minimal code changes. This gives both a rich metrics platform and a web-accessible version of the existing dashboard without building a new UI from scratch.

---

## 7. Historical/Trend Data That Could Be Graphed

### Currently Tracked (In-Memory, Lost on Restart)

The dashboard maintains three `deque(maxlen=60)` buffers that feed sparkline widgets:

| Buffer | Contents | Sparkline Width | Panel |
|--------|----------|-----------------|-------|
| `vram_history` | VRAM used (MB) | 20 chars | GPU Panel |
| `temp_history` | GPU temp (C) | 20 chars | GPU Panel |
| `queue_history` | Queue depth (int) | 20 chars | Queue Panel |

These represent approximately 2 minutes of data at the default 2-second poll interval. All data is lost when the dashboard restarts.

### Data That Could Be Graphed With Persistence

1. **VRAM usage over time** (hours/days)
   - Source: `/broker/status` -> `gpu.vram_used_mb`
   - Value: Identify VRAM leak patterns, load cycling behavior, peak usage windows

2. **GPU temperature over time**
   - Source: `/broker/status` -> `gpu.temperature_c`
   - Value: Correlate temperature spikes with swap storms, identify cooling issues

3. **Queue depth over time**
   - Source: `/broker/status` -> `queue_depth`
   - Value: Identify demand patterns, capacity planning, detect queueing pathologies

4. **Model swap rate** (swaps per minute)
   - Source: `/broker/status` -> `total_model_swaps` (delta over time)
   - Value: Detect thrashing, validate cooldown effectiveness, crash risk assessment

5. **Request latency distribution**
   - Source: `/broker/recent` -> `duration_s` per request
   - Value: P50/P95/P99 latency, identify degradation, per-model latency comparison

6. **Queue wait time distribution**
   - Source: `/broker/recent` -> `queue_wait_s` per request
   - Value: Scheduling effectiveness, starvation detection, per-model wait comparison

7. **Model residency time** (how long each model stays loaded)
   - Source: Derived from swap events in audit log
   - Value: Model affinity effectiveness, identify thrashing models

8. **A2A task throughput** (tasks per minute by state)
   - Source: `/broker/status` -> `a2a_summary` (delta over time)
   - Value: A2A workload characterization, capacity planning

9. **Power draw over time**
   - Source: `/broker/status` -> `gpu.power_draw_watts`
   - Value: Correlate power spikes with swaps, PSU headroom monitoring

10. **Circuit breaker state transitions**
    - Source: `/broker/health` -> `circuit` field
    - Value: Backend reliability, failure pattern identification

### Persistence Options

| Option | Complexity | Storage | Query |
|--------|-----------|---------|-------|
| **Prometheus + Grafana** | Low | Time-series DB | PromQL graphs |
| **SQLite ring buffer** | Medium | Local file | SQL queries |
| **JSONL append log** | Low | Flat file | Grep/jq |
| **InfluxDB** | Medium | Time-series DB | Flux queries |

The Prometheus path is the natural fit since `/broker/metrics` already exists and 35+ metrics are defined in `metrics.py`. Adding Grafana requires zero code changes to BASTION.

---

## 8. Alerting and Notification Capabilities

### Current Alert System

The `AlertPanel` implements a threshold-based alert evaluator (`_evaluate_alerts()`) with:

| Alert Key | Severity | Threshold | Auto-Dismiss |
|-----------|----------|-----------|-------------|
| `vram_warn` | WARN | VRAM >= 85% | 60 seconds |
| `vram_crit` | CRITICAL | VRAM >= 95% | Persists until cleared |
| `temp_warn` | WARN | GPU temp >= 75C | 60 seconds |
| `temp_crit` | CRITICAL | GPU temp >= 82C | Persists until cleared |
| `queue_warn` | WARN | Queue depth >= 10 | 60 seconds |
| `queue_crit` | CRITICAL | Queue depth >= 50 | Persists until cleared |
| `conn_lost` | CRITICAL | Connection lost | Persists until reconnected |

**Auto-dismiss logic**: INFO alerts expire after 30 seconds, WARN after 60 seconds. CRITICAL alerts persist until the triggering condition clears.

**Stall annotation**: Queue alerts include `stall_suffix` with the stall reason and cooldown remaining when available from `/broker/queue`.

### Limitations of Current Alerting

1. **TUI-only**: Alerts are only visible in the terminal. No external notification channel.
2. **No sound**: No audible alert for critical conditions.
3. **No history**: Alert history is stored in a `deque(maxlen=100)` and lost on restart.
4. **No acknowledgment**: Alerts cannot be acknowledged or silenced.
5. **Hardcoded thresholds**: `VRAM_WARN_PCT = 85.0`, `TEMP_CRIT_C = 82`, etc. are hardcoded in the panel class, not read from configuration.
6. **No circuit breaker alerts**: The circuit breaker transitioning to `open` does not trigger an alert.
7. **No watchdog alerts**: Ollama going unhealthy or GPU timing out does not trigger an alert.
8. **No swap rate alerts**: The swap rate limiter entering `warn` or `critical` state is not alerted.

### Potential Alerting Enhancements

1. **Desktop notifications**: Use `notify-send` (Linux) or `osascript` (macOS) for critical alerts when the terminal is not focused.

2. **Webhook integration**: POST alert events to a configurable webhook URL (Slack, Discord, PagerDuty).

3. **Email alerts**: SMTP integration for critical alerts (GPU overheat, VRAM exhaustion).

4. **Alertmanager integration**: Since Prometheus metrics are already exposed, Alertmanager rules can trigger notifications based on any metric threshold. This requires zero BASTION code changes.

5. **Additional alert conditions**:
   - Circuit breaker open (backend failure)
   - Watchdog: Ollama unhealthy for >N seconds
   - Watchdog: GPU timeout
   - Swap rate critical (crash prevention throttle active)
   - A2A task store at backpressure (approaching capacity limit)
   - Stale connection for >N seconds

6. **Configurable thresholds**: Read alert thresholds from `broker.yaml` instead of hardcoding them.

7. **Alert history persistence**: Write alerts to the audit JSONL log so they survive restarts.

---

## 9. Phase 2 Dashboard Plan vs. Implementation

### What Was Planned (ref-phase2-dashboard.md, S5)

The Phase 2 dashboard plan specified:

| Feature | Status | Notes |
|---------|--------|-------|
| Wire existing `sparkline()` into GPUPanel and QueuePanel | DONE | Sparklines render in both panels |
| Add `queue_history` deque | DONE | `deque(maxlen=60)` exists |
| Create `AlertPanel` with severity tiers (info/warn/critical) | DONE | Full severity system with auto-dismiss |
| Create `SafetyLimitsBar` widget (VRAM budget visualization) | DONE | 30-char horizontal bar with percentage coloring |
| Keyboard preload (`p`), unload (`u`), drain (`d`) with modal confirmations | DONE | All three with Textual modal dialogs |
| Request trace viewer panel | DONE | `TracePanel` shows last 20 requests |
| `/broker/recent` endpoint in server.py (in-memory deque of last 50 requests) | DONE | Ring buffer with 7 fields per request |
| ALL existing + Phase 1 tests still pass | DONE (per roadmap) | S5 committed successfully |

### What Was Added Beyond S5 (S11 + S12 Enhancements)

Features added in later sessions that were NOT in the original S5 plan:

| Feature | Session | Description |
|---------|---------|-------------|
| **A2A Task Panel** | S11 | Task count summaries, individual task display |
| **Circuit Breaker Panel** | S11 | Three-state circuit breaker visualization |
| **VRAM Ledger Panel** | S11 | Full VRAMManager ledger with per-reservation details |
| **Lease Panel** | S11 | Active lease display with TTL countdown |
| **Audit Stream Panel** | S11 | Event display (synthesized from recent requests) |
| **Watchdog Panel** | S11 | Ollama/GPU health monitoring |
| **Connection Panel** | S11 | STALE badge, exponential backoff display |
| **Two-port mode** | S11 | `--admin-url` CLI argument for split proxy/admin |
| **Help overlay** (`h`) | S11 | Modal showing all keyboard bindings |
| **Focus navigation** (`a`, `c`) | S11 | Scroll to specific panels |
| **Auth support** | S11 | `--api-key` and `BASTION_API_KEY` env var |
| **GPU Fan Control** (`f`) | S12 | 5 speed presets + auto mode |
| **Auto-fan trigger** | S12 | CPU temp-triggered GPU fan escalation |
| **GPU Process Kill** (`g`) | S12 | List + kill GPU compute processes |
| **Service restart** (`s`) | S12 | systemctl restart via sudo |

### What Was Planned Elsewhere but NOT Implemented in Dashboard

From the roadmap "Enhanced Observability" section and scout recommendations:

| Planned Feature | Source | Status |
|----------------|--------|--------|
| Grafana dashboard template | Roadmap | NOT DONE -- no template provided |
| Historical analytics for scheduling decisions | Roadmap | NOT DONE -- sparklines only (2 min window) |
| Alertmanager integration | Roadmap | NOT DONE -- alerts are TUI-only |
| SSE streaming for A2A task progress | Code Cartography Scout | NOT DONE -- panel polls only |
| `/broker/status/stream` SSE for live dashboard updates | Code Cartography Scout | NOT DONE -- polling only |
| Web-based dashboard alternative | N/A | NOT DONE -- TUI only |
| Dashboard automated tests | ref-phase2-dashboard.md | PARTIAL -- only sparkline/alert threshold unit tests planned |

---

## 10. Architectural Assessment

### Strengths

1. **Perfect isolation**: Zero internal imports. The dashboard is a pure HTTP client. This is the gold standard for monitoring tools -- it can run on a different machine, survive server restarts, and be replaced without touching BASTION core.

2. **Comprehensive panel coverage**: 15 widgets cover GPU, models, queue, scheduler, circuit breaker, VRAM ledger, watchdog, A2A tasks, leases, audit events, request trace, alerts, safety bar, connection status, and a status bar. This is thorough.

3. **Resilient connection handling**: Exponential backoff (2s to 60s), STALE badge on disconnection, graceful degradation showing last-known data, manual refresh to reset backoff.

4. **Severity-aware alerting**: Three-tier alerts with auto-dismiss, condition-clearing for critical alerts, and stall context annotation.

5. **Active GPU management**: Fan control, process kill, and service restart go beyond passive monitoring into operational management.

6. **Sparkline trends**: Even though limited to approximately 2 minutes of data, sparklines provide at-a-glance trend information for VRAM, temperature, and queue depth.

### Weaknesses

1. **Fake audit data**: The `AuditStreamPanel` does not display real audit events. It calls `_build_audit_events()` which transforms recent request records into pseudo-audit events. Real audit events are written to `/tmp/bastion-audit.jsonl` but no API endpoint reads them, so the dashboard cannot access them.

2. **Hardcoded VRAM budget**: `SafetyLimitsBar.VRAM_BUDGET_GB = 26.0` is hardcoded. If `gpu.total_vram_gb` or `gpu.headroom_gb` change in config, the dashboard will show incorrect budget information.

3. **A2A data availability gap**: The A2A task panel expects `a2a_tasks` and `a2a_summary` keys in the `/broker/status` response, but `BrokerStatus` (the Pydantic model) does not define these fields. The panel will always show "(none)" unless the server adds these fields beyond the model definition.

4. **Lease data availability gap**: Similarly, the `LeasePanel` expects a `leases` key in status data, but `BrokerStatus` does not define it. The panel will always show "(none)".

5. **No data persistence**: All trend data, alert history, and audit events are in-memory deques lost on restart.

6. **Sequential supplemental fetches**: The main poll (`/broker/status`) happens first, then health, VRAM, watchdog, and queue are fetched in parallel. But recent requests are fetched sequentially after that. This could be parallelized.

7. **`total_requests_served` always 0**: The Data Model Scout notes this field in `BrokerStatus` is never populated. The Scheduler panel shows this value, so it will always display 0.

---

## 11. Recommendations

### High Priority

1. **Wire A2A and lease data into `/broker/status` response** or create dedicated polling endpoints, so the A2A task panel and lease panel actually display data.

2. **Add real audit event endpoint** (`GET /broker/audit?limit=10`) to replace the synthesized audit events with genuine audit log entries.

3. **Make VRAM budget configurable** in the dashboard by reading it from the `/broker/status` response (requires adding `vram_budget_gb` to the API).

4. **Add Grafana dashboard template** to leverage the existing Prometheus metrics endpoint for historical visualization without any code changes.

### Medium Priority

5. **Implement SSE streaming** for A2A task progress in a new panel or overlay, using the existing `/a2a/tasks/{id}/stream` endpoint.

6. **Add CORS middleware** to enable future web-based dashboard access.

7. **Create `/broker/status/stream` SSE endpoint** for push-based dashboard updates (eliminates polling overhead and reduces latency).

8. **Extend alert conditions** to cover circuit breaker open, watchdog unhealthy, and swap rate critical states.

9. **Read alert thresholds from configuration** instead of hardcoding them in `AlertPanel`.

### Low Priority

10. **Explore Textual Web** (`textual-serve`) to expose the existing TUI as a web application with minimal effort.

11. **Add per-request queue detail view** with age, priority scores, and client info (requires new endpoint).

12. **Persist sparkline data** to a local file or SQLite DB for cross-restart trend continuity.

13. **Add desktop notification integration** for critical alerts when the terminal is not focused.

---

## 12. Summary Statistics

| Metric | Count |
|--------|-------|
| Total widget classes | 20 (15 panels + 5 modals) |
| Keyboard bindings | 11 |
| API endpoints consumed (read) | 6 of 23 admin endpoints |
| API endpoints consumed (write) | 4 action endpoints |
| API endpoints NOT consumed | 13 |
| Data dimensions not visualized | 7 (intents, config, residency, per-request inflight, session profiles, Prometheus, scheduler diagnostics) |
| Alert conditions | 7 (VRAM warn/crit, temp warn/crit, queue warn/crit, connection lost) |
| Sparkline buffers | 3 (VRAM, temp, queue -- 60 samples each) |
| Modal dialogs | 6 (confirm, model select, help, fan control, GPU process list, GPU kill confirm) |
| Lines of code | 2,159 |
| Internal imports from bastion.* | 0 (perfect isolation) |

---

**End of Report**

Generated by Dashboard & UI Analyst
Session: Audit Phase (Analyst)
Depends on: Code Cartography Scout, API Surface Scout, Data Model Scout
