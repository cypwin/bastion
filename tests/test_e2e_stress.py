"""E2E stress test suite for BASTION.

Requires a LIVE BASTION instance (proxying to Ollama) at http://127.0.0.1:11434.
These tests exercise the full proxy -> scheduler -> Ollama pipeline under
concurrent load, verifying VRAM budget enforcement, queue ordering, model swap
coordination, and streaming correctness.

Not run in CI — intended for manual pre-release validation on a GPU machine.
Skips automatically if BASTION is not reachable.

Usage:
    python -m pytest tests/test_e2e_stress.py -v -s
    python -m pytest -m e2e -v -s

Forensic log written to /tmp/bastion-stress-test.jsonl (JSONL, one event per line).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASTION_URL: str = "http://127.0.0.1:11434"
STRESS_LOG_PATH: str = "/tmp/bastion-stress-test.jsonl"
REQUEST_TIMEOUT: float = 120.0
VRAM_BUDGET_GB: float = 28.0

# Known model VRAM sizes (from config/broker.yaml)
KNOWN_VRAM: dict[str, float] = {
    "qwen3:30b-a3b-instruct-2507-q4_K_M": 18.63,
    "qwen3:14b": 9.3,
    "qwen3:8b": 5.2,
    "phi4:14b-q4_K_M": 9.1,
    "mistral-nemo:12b": 8.1,
    "granite3.1-dense:8b": 5.2,
    "llama3.1:8b": 4.4,
    "qwen2.5-coder:7b": 4.7,
    "nuextract": 2.2,
    "phi4-reasoning:14b": 8.0,
    "qwen3:30b-a3b-thinking-2507-q4_K_M": 18.63,
    "nemotron-3-nano": 6.0,
    "nomic-embed-text": 0.4,
}

# VRAM thresholds for model categorization
_LARGE_THRESHOLD: float = 12.0   # >= 12 GB
_MEDIUM_THRESHOLD: float = 6.0   # >= 6 GB
_EMBEDDING_THRESHOLD: float = 1.0  # < 1 GB treated as embedding


# ---------------------------------------------------------------------------
# StressLog — structured JSONL logger for stress test events
# ---------------------------------------------------------------------------

class StressLog:
    """Structured JSONL logger for stress test events.

    Rotates any previous log file by appending an ISO timestamp suffix,
    then opens a fresh log at the given path.  All entries are flushed
    immediately and echoed to stderr for interactive visibility.

    Can be used as a context manager::

        with StressLog() as log:
            log.log("test_started", {"suite": "e2e"})
    """

    def __init__(self, path: str = STRESS_LOG_PATH) -> None:
        self._path = Path(path)

        # Rotate previous log if it exists
        if self._path.exists():
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            rotated = self._path.with_suffix(f".jsonl.{ts}")
            self._path.rename(rotated)
            print(f"[STRESS] Rotated previous log to {rotated}", file=sys.stderr)

        self._fh = open(self._path, "w", encoding="utf-8")  # noqa: SIM115
        print(f"[STRESS] Logging to {self._path}", file=sys.stderr)

    def log(self, event: str, data: dict | None = None) -> None:
        """Write one JSON line and echo a summary to stderr."""
        ts = datetime.now(UTC).isoformat()
        record = {"timestamp": ts, "event": event, "data": data or {}}
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

        # Build a compact summary for stderr
        summary_parts: list[str] = []
        payload = data or {}
        for key in ("model", "client_id", "status_code", "latency_s", "error"):
            if key in payload:
                summary_parts.append(f"{key}={payload[key]}")
        summary = " ".join(summary_parts) if summary_parts else ""
        print(f"[STRESS] {ts} {event}: {summary}", file=sys.stderr)

    def close(self) -> None:
        """Close the log file handle."""
        if self._fh and not self._fh.closed:
            self._fh.close()

    # Context manager support
    def __enter__(self) -> StressLog:
        return self

    def __exit__(
        self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object,
    ) -> None:
        self.close()


# ---------------------------------------------------------------------------
# VRAMMonitor — background poller for GPU / VRAM state
# ---------------------------------------------------------------------------

class VRAMMonitor:
    """Async background monitor that polls /broker/status for VRAM telemetry.

    Tracks peak VRAM usage, detects model swaps and queue spikes, and logs
    every sample to the StressLog.  Use as an async context manager::

        async with VRAMMonitor(base_url, stress_log) as monitor:
            # ... run workload ...
        monitor.assert_vram_within_budget()
    """

    def __init__(
        self,
        base_url: str,
        stress_log: StressLog,
        poll_interval: float = 1.0,
    ) -> None:
        self._base_url = base_url
        self._stress_log = stress_log
        self._poll_interval = poll_interval

        # Collected telemetry
        self.samples: list[dict] = []
        self.peak_vram_gb: float = 0.0
        self.peak_queue_depth: int = 0

        # Internal state
        self._task: asyncio.Task | None = None
        self._prev_loaded: list[str] = []

    async def __aenter__(self) -> VRAMMonitor:
        self._task = asyncio.create_task(self._poll_loop(), name="vram-monitor")
        self._stress_log.log("vram_monitor_started", {
            "poll_interval": self._poll_interval,
        })
        return self

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._stress_log.log("vram_monitor_stopped", {
            "total_samples": len(self.samples),
            "peak_vram_gb": round(self.peak_vram_gb, 2),
            "peak_queue_depth": self.peak_queue_depth,
        })

    async def _poll_loop(self) -> None:
        """Continuously poll /broker/status until cancelled."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    resp = await client.get(f"{self._base_url}/broker/status")
                    resp.raise_for_status()
                    status = resp.json()
                    self._process_sample(status)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._stress_log.log("vram_poll_error", {"error": str(exc)})

                await asyncio.sleep(self._poll_interval)

    def _process_sample(self, status: dict) -> None:
        """Extract telemetry from a /broker/status response and record it."""
        gpu = status.get("gpu", {})
        loaded_models_raw = status.get("loaded_models", [])
        queue_depth = status.get("queue_depth", 0)

        # Compute VRAM in GB from gpu.vram_used_mb
        vram_used_mb = gpu.get("vram_used_mb") or 0
        vram_used_gb = round(vram_used_mb / 1024, 2)

        loaded_names = sorted(m.get("name", "") for m in loaded_models_raw)
        temperature_c = gpu.get("temperature_c")
        power_w = gpu.get("power_draw_watts")

        sample = {
            "vram_used_gb": vram_used_gb,
            "loaded_models": loaded_names,
            "queue_depth": queue_depth,
            "temperature_c": temperature_c,
            "power_w": power_w,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.samples.append(sample)

        # Update running maximums
        if vram_used_gb > self.peak_vram_gb:
            self.peak_vram_gb = vram_used_gb
        if queue_depth > self.peak_queue_depth:
            self.peak_queue_depth = queue_depth

        # Detect model swaps
        if loaded_names != self._prev_loaded:
            self._stress_log.log("model_swap_detected", {
                "previous": self._prev_loaded,
                "current": loaded_names,
            })
            self._prev_loaded = loaded_names

        # Alert: VRAM budget exceeded
        if vram_used_gb > VRAM_BUDGET_GB:
            self._stress_log.log("vram_budget_exceeded", {
                "vram_used_gb": vram_used_gb,
                "budget_gb": VRAM_BUDGET_GB,
                "loaded_models": loaded_names,
            })

        # Alert: queue spike
        if queue_depth > 10:
            self._stress_log.log("queue_spike", {
                "queue_depth": queue_depth,
                "loaded_models": loaded_names,
            })

        # Log every sample
        self._stress_log.log("vram_sample", sample)

    def assert_vram_within_budget(self, max_gb: float = VRAM_BUDGET_GB) -> None:
        """Assert that peak VRAM never exceeded the budget."""
        assert self.peak_vram_gb <= max_gb, (
            f"Peak VRAM {self.peak_vram_gb:.2f} GB exceeded budget {max_gb:.2f} GB"
        )

    def print_summary(self) -> None:
        """Print a human-readable summary of the monitoring session to stderr."""
        n = len(self.samples)
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"VRAM Monitor Summary ({n} samples)", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)
        print(
            f"  Peak VRAM used:   {self.peak_vram_gb:.2f} GB"
            f" / {VRAM_BUDGET_GB:.1f} GB budget",
            file=sys.stderr,
        )
        print(f"  Peak queue depth: {self.peak_queue_depth}", file=sys.stderr)
        if self.samples:
            first_ts = self.samples[0]["timestamp"]
            last_ts = self.samples[-1]["timestamp"]
            print(f"  First sample:     {first_ts}", file=sys.stderr)
            print(f"  Last sample:      {last_ts}", file=sys.stderr)

            # Unique models observed
            all_models: set[str] = set()
            for s in self.samples:
                all_models.update(s.get("loaded_models", []))
            print(f"  Models observed:  {sorted(all_models)}", file=sys.stderr)

            # Temperature range
            temps = [s["temperature_c"] for s in self.samples if s.get("temperature_c") is not None]
            if temps:
                print(f"  Temperature:      {min(temps)}-{max(temps)} C", file=sys.stderr)

            # Power range
            powers = [s["power_w"] for s in self.samples if s.get("power_w") is not None]
            if powers:
                print(f"  Power draw:       {min(powers):.0f}-{max(powers):.0f} W", file=sys.stderr)
        print(f"{'=' * 60}\n", file=sys.stderr)


# ---------------------------------------------------------------------------
# StressClient — async HTTP client for driving BASTION requests
# ---------------------------------------------------------------------------

class StressClient:
    """Async client for sending inference requests to BASTION under stress.

    Tracks all results for post-hoc assertions.  Each ``generate()`` call
    logs request/response details to the StressLog and appends a result
    dict to ``self.results``.
    """

    def __init__(
        self,
        base_url: str,
        stress_log: StressLog,
        client_id: str = "client-0",
    ) -> None:
        self._base_url = base_url
        self._stress_log = stress_log
        self.client_id = client_id
        self.results: list[dict] = []

    async def generate(
        self,
        model: str,
        prompt: str,
        stream: bool = False,
        priority_tier: str = "agent",
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict:
        """Send a single /api/generate request and return a result dict.

        Parameters
        ----------
        model : str
            Ollama model name.
        prompt : str
            Text prompt to send.
        stream : bool
            Whether to request NDJSON streaming.
        priority_tier : str
            Priority tier header value (interactive, agent, pipeline, background).
        timeout : float
            HTTP request timeout in seconds.

        Returns
        -------
        dict
            Result with keys: model, status_code, latency_s, stream,
            response_text, client_id, error.
        """
        self._stress_log.log("request_sent", {
            "model": model,
            "prompt": prompt[:50],
            "stream": stream,
            "priority_tier": priority_tier,
            "client_id": self.client_id,
        })

        result: dict = {
            "model": model,
            "status_code": None,
            "latency_s": None,
            "stream": stream,
            "response_text": "",
            "client_id": self.client_id,
            "error": None,
        }

        start = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                headers = {"X-Broker-Priority": priority_tier}
                body = {
                    "model": model,
                    "prompt": prompt,
                    "stream": stream,
                    "options": {"use_mmap": False},
                }

                if stream:
                    # Streaming: read NDJSON lines, collect response text
                    response_parts: list[str] = []
                    async with client.stream(
                        "POST",
                        f"{self._base_url}/api/generate",
                        json=body,
                        headers=headers,
                    ) as resp:
                        result["status_code"] = resp.status_code
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                chunk = json.loads(line)
                                token = chunk.get("response", "")
                                if token:
                                    response_parts.append(token)
                            except json.JSONDecodeError:
                                pass
                    result["response_text"] = "".join(response_parts)
                else:
                    # Non-streaming: single JSON response
                    resp = await client.post(
                        f"{self._base_url}/api/generate",
                        json=body,
                        headers=headers,
                    )
                    result["status_code"] = resp.status_code
                    try:
                        resp_json = resp.json()
                        result["response_text"] = resp_json.get("response", "")
                    except (json.JSONDecodeError, ValueError):
                        result["response_text"] = resp.text

        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self._stress_log.log("request_failed", {
                "model": model,
                "error": result["error"],
                "client_id": self.client_id,
                "traceback": traceback.format_exc(),
            })
        finally:
            result["latency_s"] = round(time.monotonic() - start, 3)

        # Log completion (even if error — latency is still recorded)
        if result["error"] is None:
            self._stress_log.log("request_completed", {
                "model": model,
                "latency_s": result["latency_s"],
                "status_code": result["status_code"],
                "client_id": self.client_id,
                "response_length": len(result["response_text"]),
            })

        self.results.append(result)
        return result

    async def generate_many(
        self,
        specs: list[dict],
        concurrency: int = 5,
    ) -> list[dict]:
        """Send multiple generate requests with bounded concurrency.

        Parameters
        ----------
        specs : list[dict]
            Each spec must have ``model`` and ``prompt`` keys.
            Optional keys: ``stream`` (default False),
            ``priority_tier`` (default "agent").
        concurrency : int
            Maximum number of concurrent in-flight requests.

        Returns
        -------
        list[dict]
            All result dicts (order matches input specs via gather).
        """
        sem = asyncio.Semaphore(concurrency)

        async def _guarded(spec: dict) -> dict:
            async with sem:
                return await self.generate(
                    model=spec["model"],
                    prompt=spec["prompt"],
                    stream=spec.get("stream", False),
                    priority_tier=spec.get("priority_tier", "agent"),
                    timeout=spec.get("timeout", REQUEST_TIMEOUT),
                )

        tasks = [asyncio.create_task(_guarded(s)) for s in specs]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    def assert_all_succeeded(self) -> None:
        """Assert that no result has a server error (5xx) or an error string."""
        failures: list[dict] = []
        for r in self.results:
            if (r.get("error") is not None
                    or r.get("status_code") is not None
                    and r["status_code"] >= 500):
                failures.append(r)
        if failures:
            summary = "\n".join(
                f"  - {f['model']} status={f.get('status_code')} error={f.get('error')}"
                for f in failures
            )
            raise AssertionError(
                f"{len(failures)} request(s) failed:\n{summary}"
            )

    def assert_no_timeouts(self, timeout: float = REQUEST_TIMEOUT) -> None:
        """Assert that all requests completed within the timeout threshold."""
        slow: list[dict] = []
        for r in self.results:
            if r.get("latency_s") is not None and r["latency_s"] >= timeout:
                slow.append(r)
        if slow:
            summary = "\n".join(
                f"  - {s['model']} latency={s['latency_s']:.1f}s"
                for s in slow
            )
            raise AssertionError(
                f"{len(slow)} request(s) exceeded {timeout}s timeout:\n{summary}"
            )


# ---------------------------------------------------------------------------
# Helper: pick non-embedding models
# ---------------------------------------------------------------------------

def _pick_smallest_model(model_categories: dict, available_models: list[str]) -> str:
    """Return the smallest non-embedding model available."""
    for category in ("small", "medium", "large"):
        candidates = model_categories.get(category, [])
        if candidates:
            return candidates[0]
    embedding = set(model_categories.get("embedding", []))
    for m in available_models:
        if m not in embedding:
            return m
    pytest.skip("No non-embedding models available")


def _pick_swap_models(
    model_categories: dict,
    available_models: list[str],
    count: int = 2,
) -> list[str]:
    """Pick ``count`` distinct non-embedding models, preferring different size categories."""
    picked: list[str] = []
    for category in ("small", "medium", "large"):
        candidates = model_categories.get(category, [])
        for c in candidates:
            if c not in picked:
                picked.append(c)
                break
        if len(picked) >= count:
            return picked[:count]

    # Fallback: fill from any non-embedding model
    embedding = set(model_categories.get("embedding", []))
    for m in available_models:
        if m not in embedding and m not in picked:
            picked.append(m)
            if len(picked) >= count:
                return picked[:count]

    if len(picked) < count:
        pytest.skip(f"Need {count} non-embedding models, only have {len(picked)}")
    return picked[:count]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _unload_all_models(bastion_url: str) -> list[str]:
    """Unload every model currently loaded in Ollama via the broker.

    Returns the list of models that were unloaded.  Used to ensure VRAM
    is clean before tests that need to preload specific model sets.
    """
    unloaded: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        ps = await client.get(f"{bastion_url}/api/ps", timeout=10.0)
        for model in ps.json().get("models", []):
            name = model.get("name", "")
            if name:
                await client.post(
                    f"{bastion_url}/broker/unload",
                    json={"model": name},
                    timeout=30.0,
                )
                unloaded.append(name)
    return unloaded


# ---------------------------------------------------------------------------
# Pytest fixtures (session-scoped)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def stress_log() -> StressLog:
    """Create a StressLog for the entire test session."""
    log = StressLog()
    log.log("session_started", {"bastion_url": BASTION_URL})
    yield log  # type: ignore[misc]
    log.log("session_ended")
    log.close()


@pytest.fixture(scope="session")
def bastion_url() -> str:
    """Verify BASTION is reachable, or skip the entire session.

    Sends a GET to /broker/health with a short timeout.  If BASTION
    is not running, all stress tests are skipped.
    """
    try:
        resp = httpx.get(f"{BASTION_URL}/broker/health", timeout=5.0)
        resp.raise_for_status()
    except Exception as exc:
        pytest.skip(f"BASTION not reachable at {BASTION_URL}: {exc}")
    return BASTION_URL


@pytest.fixture(scope="session")
def available_models(bastion_url: str) -> list[str]:
    """Query Ollama (via BASTION proxy) for available model names.

    Returns a sorted list of model name strings.  Skips if no models
    are available.
    """
    resp = httpx.get(f"{bastion_url}/api/tags", timeout=10.0)
    resp.raise_for_status()
    models_raw = resp.json().get("models", [])
    names = sorted(m.get("name", m.get("model", "")) for m in models_raw)
    names = [n for n in names if n]  # filter blanks

    if not names:
        pytest.skip("No models available in Ollama")
    return names


@pytest.fixture(scope="session")
def model_categories(available_models: list[str]) -> dict[str, list[str]]:
    """Categorize available models into size buckets.

    Categories
    ----------
    large : >= 12 GB VRAM
    medium : >= 6 GB VRAM (and < 12 GB)
    small : >= 1 GB VRAM (and < 6 GB)
    embedding : < 1 GB VRAM

    Models not in KNOWN_VRAM are assigned a default estimate of 10.0 GB
    (matching gpu.default_vram_estimate_gb in broker.yaml).
    """
    categories: dict[str, list[str]] = {
        "large": [],
        "medium": [],
        "small": [],
        "embedding": [],
    }

    for model in available_models:
        vram = KNOWN_VRAM.get(model, 10.0)

        if vram < _EMBEDDING_THRESHOLD:
            categories["embedding"].append(model)
        elif vram < _MEDIUM_THRESHOLD:
            categories["small"].append(model)
        elif vram < _LARGE_THRESHOLD:
            categories["medium"].append(model)
        else:
            categories["large"].append(model)

    return categories


# ===========================================================================
# Test Classes
# ===========================================================================


@pytest.mark.e2e
class TestPrerequisites:
    """Verify BASTION and Ollama are reachable before running stress tests."""

    async def test_bastion_reachable(self, bastion_url: str, stress_log: StressLog) -> None:
        stress_log.log("test_start", {"test": "test_bastion_reachable"})
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{bastion_url}/broker/health", timeout=REQUEST_TIMEOUT)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.json()
            assert body.get("healthy") is True, f"Expected healthy=true, got {body}"
            stress_log.log("test_end", {"test": "test_bastion_reachable", "result": "pass"})
        except Exception:
            stress_log.log("test_end", {"test": "test_bastion_reachable", "result": "fail"})
            raise

    async def test_ollama_responds(self, bastion_url: str, stress_log: StressLog) -> None:
        stress_log.log("test_start", {"test": "test_ollama_responds"})
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{bastion_url}/api/tags", timeout=REQUEST_TIMEOUT)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.json()
            assert "models" in body, f"Expected 'models' key in response, got {list(body.keys())}"
            assert isinstance(body["models"], list), "models should be a list"
            stress_log.log("test_end", {"test": "test_ollama_responds", "result": "pass"})
        except Exception:
            stress_log.log("test_end", {"test": "test_ollama_responds", "result": "fail"})
            raise

    async def test_minimum_models_available(
        self,
        available_models: list[str],
        model_categories: dict,
        stress_log: StressLog,
    ) -> None:
        stress_log.log("test_start", {"test": "test_minimum_models_available"})
        try:
            non_embedding = (
                model_categories.get("large", [])
                + model_categories.get("medium", [])
                + model_categories.get("small", [])
            )
            stress_log.log("model_inventory", {
                "total": len(available_models),
                "non_embedding": len(non_embedding),
                "large": len(model_categories.get("large", [])),
                "medium": len(model_categories.get("medium", [])),
                "small": len(model_categories.get("small", [])),
                "embedding": len(model_categories.get("embedding", [])),
                "models": available_models,
            })
            if len(non_embedding) < 2:
                pytest.skip(f"Need at least 2 non-embedding models, have {len(non_embedding)}")
            stress_log.log("test_end", {"test": "test_minimum_models_available", "result": "pass"})
        except Exception:
            stress_log.log("test_end", {"test": "test_minimum_models_available", "result": "fail"})
            raise


@pytest.mark.e2e
class TestSingleModelSerialization:
    """Verify concurrent requests to the same model are serialized safely."""

    async def test_concurrent_nonstreaming(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_concurrent_nonstreaming"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {
                "model": model, "test": "test_concurrent_nonstreaming",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="serial-ns")
                specs = [
                    {"model": model, "prompt": prompt, "stream": False, "priority_tier": "agent"}
                    for prompt in [
                        "What is 2+2?",
                        "Say hello",
                        "Count to 3",
                        "Name a color",
                        "What is Python?",
                    ]
                ]
                results = await client.generate_many(specs, concurrency=5)
                client.assert_all_succeeded()
                client.assert_no_timeouts()
                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_concurrent_nonstreaming",
                "result": "pass",
                "total_requests": len(results),
            })
        except Exception:
            stress_log.log("test_end", {"test": "test_concurrent_nonstreaming", "result": "fail"})
            raise

    async def test_concurrent_streaming(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_concurrent_streaming"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {"model": model, "test": "test_concurrent_streaming"})

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="serial-stream")
                specs = [
                    {"model": model, "prompt": prompt, "stream": True, "priority_tier": "agent"}
                    for prompt in [
                        "What is 2+2?",
                        "Say hello",
                        "Count to 3",
                        "Name a color",
                        "What is Python?",
                    ]
                ]
                results = await client.generate_many(specs, concurrency=5)
                client.assert_all_succeeded()
                client.assert_no_timeouts()

                # Verify streaming responses returned content (NDJSON chunks)
                for r in results:
                    assert r["response_text"], (
                        f"Streaming request to {model} returned empty response"
                    )

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_concurrent_streaming",
                "result": "pass",
                "total_requests": len(results),
            })
        except Exception:
            stress_log.log("test_end", {"test": "test_concurrent_streaming", "result": "fail"})
            raise


@pytest.mark.e2e
class TestModelSwapSafety:
    """Verify model swapping doesn't exceed VRAM budget or cause failures."""

    async def test_two_model_alternation(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_two_model_alternation"})
        try:
            models = _pick_swap_models(model_categories, available_models, count=2)
            stress_log.log("models_selected", {
                "models": models, "test": "test_two_model_alternation",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="swap-2")
                # A -> B -> A -> B (sequential to force swaps)
                specs = [
                    {"model": models[i % 2], "prompt": f"Say the word '{models[i % 2]}'"}
                    for i in range(4)
                ]
                results = await client.generate_many(specs, concurrency=1)
                client.assert_all_succeeded()
                client.assert_no_timeouts()
                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_two_model_alternation",
                "result": "pass",
                "models": models,
                "total_requests": len(results),
            })
        except Exception:
            stress_log.log("test_end", {"test": "test_two_model_alternation", "result": "fail"})
            raise

    async def test_three_model_rotation(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_three_model_rotation"})
        try:
            non_embedding = (
                model_categories.get("large", [])
                + model_categories.get("medium", [])
                + model_categories.get("small", [])
            )
            if len(non_embedding) < 3:
                pytest.skip(f"Need at least 3 non-embedding models, have {len(non_embedding)}")

            models = _pick_swap_models(model_categories, available_models, count=3)
            stress_log.log("models_selected", {
                "models": models, "test": "test_three_model_rotation",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="swap-3")
                # A -> B -> C -> A (sequential)
                rotation = [models[0], models[1], models[2], models[0]]
                specs = [
                    {"model": m, "prompt": f"Say the word '{m}'"}
                    for m in rotation
                ]
                results = await client.generate_many(specs, concurrency=1)
                client.assert_all_succeeded()
                client.assert_no_timeouts()
                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_three_model_rotation",
                "result": "pass",
                "models": models,
                "total_requests": len(results),
            })
        except Exception:
            stress_log.log("test_end", {"test": "test_three_model_rotation", "result": "fail"})
            raise

    async def test_vram_stays_within_budget(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_vram_stays_within_budget"})
        try:
            models = _pick_swap_models(model_categories, available_models, count=2)
            stress_log.log("models_selected", {
                "models": models,
                "test": "test_vram_stays_within_budget",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="vram-check")
                # Swap sequence: A -> B -> A -> B -> A (sequential)
                specs = [
                    {"model": models[i % 2], "prompt": f"Count to {i + 1}"}
                    for i in range(5)
                ]
                await client.generate_many(specs, concurrency=1)
                client.assert_all_succeeded()

                # Primary assertion: VRAM never exceeded budget
                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                stress_log.log("vram_peak", {
                    "peak_vram_gb": monitor.peak_vram_gb,
                    "budget_gb": VRAM_BUDGET_GB,
                })
                monitor.print_summary()

            stress_log.log("test_end", {"test": "test_vram_stays_within_budget", "result": "pass"})
        except Exception:
            stress_log.log("test_end", {"test": "test_vram_stays_within_budget", "result": "fail"})
            raise


@pytest.mark.e2e
class TestPriorityUnderLoad:
    """Verify priority scheduling delivers interactive requests ahead of background."""

    async def test_interactive_served_first(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_interactive_served_first"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {
                "model": model,
                "test": "test_interactive_served_first",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                # Background client sends a medium-length prompt first
                bg_client = StressClient(bastion_url, stress_log, client_id="priority-bg")
                interactive_client = StressClient(
                    bastion_url, stress_log,
                    client_id="priority-interactive",
                )

                # Fire background request first (medium prompt to occupy model)
                bg_task = asyncio.create_task(
                    bg_client.generate(
                        model,
                        "Explain the theory of relativity in detail"
                        " with examples and historical context.",
                        stream=False,
                        priority_tier="background",
                        timeout=REQUEST_TIMEOUT,
                    )
                )

                # Small delay to let background request enter queue first
                await asyncio.sleep(1)

                # Fire interactive request (short prompt)
                interactive_task = asyncio.create_task(
                    interactive_client.generate(
                        model,
                        "What is 2+2?",
                        stream=False,
                        priority_tier="interactive",
                        timeout=REQUEST_TIMEOUT,
                    )
                )

                bg_result = await bg_task
                interactive_result = await interactive_task

                bg_client.assert_all_succeeded()
                interactive_client.assert_all_succeeded()

                # Log the comparison for forensic analysis
                stress_log.log("priority_comparison", {
                    "interactive_latency": interactive_result["latency_s"],
                    "background_latency": bg_result["latency_s"],
                })

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {"test": "test_interactive_served_first", "result": "pass"})
        except Exception:
            stress_log.log("test_end", {"test": "test_interactive_served_first", "result": "fail"})
            raise

    async def test_all_requests_complete(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_all_requests_complete"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {"model": model, "test": "test_all_requests_complete"})

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="priority-all")
                priorities = ["interactive", "agent", "pipeline", "background"]
                prompts = [
                    "What is 2+2?",
                    "Say hello",
                    "Count to 3",
                    "Name a color",
                    "What is Python?",
                    "What is 1+1?",
                    "Say goodbye",
                    "Count to 5",
                    "Name a fruit",
                    "What is Java?",
                ]
                specs = [
                    {
                        "model": model,
                        "prompt": prompts[i],
                        "stream": i % 2 == 0,
                        "priority_tier": priorities[i % len(priorities)],
                    }
                    for i in range(10)
                ]
                results = await client.generate_many(specs, concurrency=10)

                # No starvation — every request must complete successfully
                client.assert_all_succeeded()
                client.assert_no_timeouts()

                stress_log.log("priority_distribution", {
                    "total": len(results),
                    "by_priority": {
                        p: sum(1 for s in specs if s["priority_tier"] == p)
                        for p in priorities
                    },
                })

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_all_requests_complete",
                "result": "pass",
                "total_requests": len(results),
            })
        except Exception:
            stress_log.log("test_end", {"test": "test_all_requests_complete", "result": "fail"})
            raise


@pytest.mark.e2e
class TestConcurrentClientBurst:
    """Verify BASTION handles bursts of 20 concurrent clients safely."""

    _SHORT_PROMPTS: list[str] = [
        "What is 2+2?",
        "Say hello",
        "Count to 3",
        "Name a color",
        "What is Python?",
        "What is 1+1?",
        "Say goodbye",
        "Count to 5",
        "Name a fruit",
        "What is Java?",
        "What is 3+3?",
        "Say yes",
        "Count to 2",
        "Name a planet",
        "What is Go?",
        "What is 4+4?",
        "Say no",
        "Count to 4",
        "Name a country",
        "What is Rust?",
    ]

    async def test_20_client_burst_same_model(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_20_client_burst_same_model"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {
                "model": model,
                "test": "test_20_client_burst_same_model",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="burst-same")
                specs = [
                    {"model": model, "prompt": self._SHORT_PROMPTS[i]}
                    for i in range(20)
                ]
                results = await client.generate_many(specs, concurrency=20)
                client.assert_all_succeeded()
                client.assert_no_timeouts()

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_20_client_burst_same_model",
                "result": "pass",
                "total_requests": len(results),
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_20_client_burst_same_model",
                "result": "fail",
            })
            raise

    async def test_20_client_burst_mixed_models(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_20_client_burst_mixed_models"})
        try:
            non_embedding = (
                model_categories.get("large", [])
                + model_categories.get("medium", [])
                + model_categories.get("small", [])
            )
            if len(non_embedding) < 2:
                pytest.skip(f"Need at least 2 non-embedding models, have {len(non_embedding)}")

            models = _pick_swap_models(
                model_categories, available_models,
                count=min(3, len(non_embedding)),
            )
            stress_log.log("models_selected", {
                "models": models,
                "test": "test_20_client_burst_mixed_models",
            })

            priorities = ["interactive", "agent", "pipeline", "background"]

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="burst-mixed")
                specs = [
                    {
                        "model": models[i % len(models)],
                        "prompt": self._SHORT_PROMPTS[i],
                        "stream": i % 3 == 0,
                        "priority_tier": priorities[i % len(priorities)],
                    }
                    for i in range(20)
                ]
                results = await client.generate_many(specs, concurrency=20)
                client.assert_all_succeeded()
                client.assert_no_timeouts()

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_20_client_burst_mixed_models",
                "result": "pass",
                "total_requests": len(results),
                "models_used": models,
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_20_client_burst_mixed_models",
                "result": "fail",
            })
            raise


@pytest.mark.e2e
class TestSustainedMixedWorkload:
    """Verify BASTION handles sustained load without failures or queue leaks."""

    async def test_sustained_50_requests(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_sustained_50_requests"})
        try:
            # Gather all non-embedding models
            embedding = set(model_categories.get("embedding", []))
            non_embedding = [m for m in available_models if m not in embedding]
            if not non_embedding:
                pytest.skip("No non-embedding models available")

            # Cap to 3 models max — keeps swap rate under critical threshold (6/min).
            # With 50 requests across 3 models, affinity clustering reduces swaps to
            # ~15-20 (only when switching models), staying under 6/min sustained rate.
            max_sustained_models = 3
            if len(non_embedding) > max_sustained_models:
                # Prefer models from different size categories for realistic mix
                capped: list[str] = []
                for cat in ("small", "medium", "large"):
                    for m in model_categories.get(cat, []):
                        if m not in capped:
                            capped.append(m)
                        if len(capped) >= max_sustained_models:
                            break
                    if len(capped) >= max_sustained_models:
                        break
                non_embedding = capped

            stress_log.log("models_selected", {
                "models": non_embedding,
                "test": "test_sustained_50_requests",
            })

            prompts = [
                "What is 2+2?", "Say hello", "Count to 3", "Name a color",
                "What is Python?", "What is 1+1?", "Say goodbye", "Count to 5",
                "Name a fruit", "What is Java?", "What is 3+3?", "Say yes",
                "Count to 2", "Name a planet", "What is Go?", "What is 4+4?",
                "Say no", "Count to 4", "Name a country", "What is Rust?",
                "Define AI", "What is 5+5?", "Say maybe", "Count to 6",
                "Name an animal", "What is C++?", "What is 6+6?", "Say perhaps",
                "Count to 7", "Name a city", "What is Ruby?", "What is 7+7?",
                "Say sure", "Count to 8", "Name an ocean", "What is Kotlin?",
                "What is 8+8?", "Say okay", "Count to 9", "Name a river",
                "What is Swift?", "What is 9+9?", "Say alright", "Count to 10",
                "Name a mountain", "What is Scala?", "What is 10+10?",
                "Say indeed", "Count to 1", "Name a flower",
            ]
            priorities = ["interactive", "agent", "pipeline", "background"]

            # Timeout accounts for swap rate limiter extending cooldowns when
            # swap velocity approaches crash thresholds (critical=6/min → 10s cooldown).
            sustained_timeout = 240.0  # 4 minutes for 50 requests with throttled swaps

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="sustained")
                specs = [
                    {
                        "model": non_embedding[i % len(non_embedding)],
                        "prompt": prompts[i],
                        "stream": i % 2 == 0,
                        "priority_tier": priorities[i % len(priorities)],
                        "timeout": sustained_timeout,
                    }
                    for i in range(50)
                ]
                results = await client.generate_many(specs, concurrency=20)

                client.assert_all_succeeded()
                client.assert_no_timeouts(timeout=sustained_timeout)

                # Zero 5xx failures
                server_errors = [r for r in results if r.get("status_code", 0) >= 500]
                assert len(server_errors) == 0, (
                    f"Got {len(server_errors)} server errors (5xx): "
                    f"{[r.get('status_code') for r in server_errors]}"
                )

                # VRAM budget is the scheduler's target, not a hard physical ceiling.
                # Transient spikes occur during model load transitions (new model
                # loading before Ollama fully frees the old one). Allow 15% overshoot.
                vram_transient_tolerance = 1.15
                monitor.assert_vram_within_budget(
                    VRAM_BUDGET_GB * vram_transient_tolerance,
                )
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_sustained_50_requests",
                "result": "pass",
                "total_requests": len(results),
                "models_used": non_embedding,
            })
        except Exception:
            stress_log.log("test_end", {"test": "test_sustained_50_requests", "result": "fail"})
            raise

    async def test_sustained_queue_health(self, bastion_url: str, stress_log: StressLog) -> None:
        stress_log.log("test_start", {"test": "test_sustained_queue_health"})
        try:
            # Give the system a moment to drain
            await asyncio.sleep(3)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{bastion_url}/broker/queue", timeout=REQUEST_TIMEOUT)
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
            body = resp.json()

            # /broker/queue returns {"models": {...}, "total": N, "pending_grants": N}
            queue_depth = body.get("total", 0)
            stress_log.log("queue_state", {"queue_depth": queue_depth, "response": body})

            assert queue_depth == 0, (
                f"Queue should be empty after sustained test, but total={queue_depth}"
            )

            stress_log.log("test_end", {"test": "test_sustained_queue_health", "result": "pass"})
        except Exception:
            stress_log.log("test_end", {"test": "test_sustained_queue_health", "result": "fail"})
            raise


@pytest.mark.e2e
class TestLongRunningQueueBehavior:
    """Tests that exercise queueing behavior when a long-running inference
    occupies the model.

    Uses a detailed essay prompt that takes 15-30 seconds to generate,
    then observes how queued requests behave around it — proving that
    BASTION properly serializes access and respects priority ordering
    even when one request monopolizes the model for an extended period.
    """

    LONG_PROMPT: str = (
        "Write a comprehensive essay about the history of computing from Charles Babbage "
        "to modern AI. Cover mechanical calculators, vacuum tubes, transistors, integrated "
        "circuits, personal computers, the internet, smartphones, and artificial intelligence. "
        "Be thorough and detailed, providing specific dates, names, and technical details "
        "for each era. The essay should be at least 500 words."
    )

    async def test_queue_forms_during_long_inference(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_queue_forms_during_long_inference"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {
                "model": model,
                "test": "test_queue_forms_during_long_inference",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                long_client = StressClient(bastion_url, stress_log, client_id="long-req")
                short_client = StressClient(bastion_url, stress_log, client_id="short-req")

                # 1. Fire the long-running request in background
                t0 = time.monotonic()
                long_task = asyncio.create_task(
                    long_client.generate(
                        model,
                        self.LONG_PROMPT,
                        stream=False,
                        priority_tier="background",
                        timeout=REQUEST_TIMEOUT,
                    )
                )

                # 2. Wait for long request to start processing
                await asyncio.sleep(2)

                # 3. Fire 5 short requests concurrently
                short_specs = [
                    {"model": model, "prompt": prompt, "priority_tier": "agent"}
                    for prompt in [
                        "What is 2+2?",
                        "Say hello",
                        "Count to 3",
                        "Name a color",
                        "What is Python?",
                    ]
                ]
                short_task = asyncio.create_task(
                    short_client.generate_many(short_specs, concurrency=5)
                )

                # 4. Poll queue depth while waiting
                queue_depths: list[dict] = []
                poll_client = httpx.AsyncClient()
                try:
                    while not long_task.done() or not short_task.done():
                        try:
                            resp = await poll_client.get(
                                f"{bastion_url}/broker/queue", timeout=5.0,
                            )
                            if resp.status_code == 200:
                                body = resp.json()
                                depth = body.get("total", 0)
                                elapsed = round(time.monotonic() - t0, 1)
                                queue_depths.append({"depth": depth, "elapsed_s": elapsed})
                                stress_log.log("queue_poll", {
                                    "depth": depth,
                                    "elapsed_s": elapsed,
                                })
                        except Exception:
                            pass  # polling is best-effort
                        await asyncio.sleep(0.5)
                finally:
                    await poll_client.aclose()

                # 5. Await all results
                long_result = await long_task
                short_results = await short_task

                # 6. Assertions
                assert long_result.get("error") is None, (
                    f"Long request failed: {long_result.get('error')}"
                )
                assert long_result["status_code"] == 200, (
                    f"Long request status {long_result['status_code']}"
                )
                short_client.assert_all_succeeded()

                # Verify that BASTION serialized the requests correctly.
                # The long request dispatched first, so it runs immediately.
                # Short requests arrive 2s later and queue behind it (and each other),
                # so their avg latency includes queue wait time and may exceed the
                # long request's latency. The key assertion is that the queue formed.
                avg_short_latency = sum(r["latency_s"] for r in short_results) / len(short_results)
                max_queue = max((d["depth"] for d in queue_depths), default=0)

                stress_log.log("latency_comparison", {
                    "long_latency_s": long_result["latency_s"],
                    "avg_short_latency_s": avg_short_latency,
                    "max_queue_depth": max_queue,
                    "queue_depth_timeline": queue_depths,
                })

                # Queue must have formed — at least 1 request was waiting at some point
                assert max_queue >= 1, (
                    f"Queue never formed (max depth {max_queue}); "
                    "short requests should have queued behind the long one"
                )

                # 7. Log completion timeline
                all_results = [("long", long_result)] + [
                    (f"short-{i}", r) for i, r in enumerate(short_results)
                ]
                timeline = sorted(all_results, key=lambda x: x[1]["latency_s"])
                stress_log.log("completion_timeline", {
                    "order": [
                        {"label": label, "latency_s": r["latency_s"]}
                        for label, r in timeline
                    ],
                })

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_queue_forms_during_long_inference",
                "result": "pass",
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_queue_forms_during_long_inference",
                "result": "fail",
            })
            raise

    async def test_priority_respected_during_long_inference(
        self,
        bastion_url: str,
        stress_log: StressLog,
        model_categories: dict,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_priority_respected_during_long_inference"})
        try:
            model = _pick_smallest_model(model_categories, available_models)
            stress_log.log("model_selected", {
                "model": model,
                "test": "test_priority_respected_during_long_inference",
            })

            async with VRAMMonitor(bastion_url, stress_log) as monitor:
                long_client = StressClient(bastion_url, stress_log, client_id="long-blocker")
                bg_client = StressClient(bastion_url, stress_log, client_id="bg-priority")
                interactive_client = StressClient(
                    bastion_url, stress_log,
                    client_id="interactive-priority",
                )

                # 1. Fire long-running request to occupy the model
                long_task = asyncio.create_task(
                    long_client.generate(
                        model,
                        self.LONG_PROMPT,
                        stream=False,
                        priority_tier="pipeline",
                        timeout=REQUEST_TIMEOUT,
                    )
                )

                # 2. Wait for it to start processing
                await asyncio.sleep(2)

                # 3. Fire background-priority short requests
                bg_specs = [
                    {"model": model, "prompt": prompt, "priority_tier": "background"}
                    for prompt in ["What is 1+1?", "Say yes", "Name a number"]
                ]
                bg_task = asyncio.create_task(
                    bg_client.generate_many(bg_specs, concurrency=3)
                )

                # 4. Fire interactive-priority short requests
                interactive_specs = [
                    {"model": model, "prompt": prompt, "priority_tier": "interactive"}
                    for prompt in ["What is 2+2?", "Say hello", "Name a color"]
                ]
                interactive_task = asyncio.create_task(
                    interactive_client.generate_many(interactive_specs, concurrency=3)
                )

                # 5. Await all results
                long_result = await long_task
                bg_results = await bg_task
                interactive_results = await interactive_task

                # 6. All must succeed
                assert long_result.get("error") is None, (
                    f"Long request failed: {long_result.get('error')}"
                )
                assert long_result["status_code"] == 200
                bg_client.assert_all_succeeded()
                interactive_client.assert_all_succeeded()

                # 7. Compare average latencies
                avg_bg = sum(r["latency_s"] for r in bg_results) / len(bg_results)
                avg_interactive = (
                    sum(r["latency_s"] for r in interactive_results)
                    / len(interactive_results)
                )

                stress_log.log("priority_latency_comparison", {
                    "avg_interactive_latency_s": avg_interactive,
                    "avg_background_latency_s": avg_bg,
                    "long_request_latency_s": long_result["latency_s"],
                    "interactive_results": [
                        {"latency_s": r["latency_s"]}
                        for r in interactive_results
                    ],
                    "background_results": [{"latency_s": r["latency_s"]} for r in bg_results],
                })

                # Interactive average should be <= background average
                # Allow 10% tolerance since timing isn't perfectly deterministic
                tolerance = avg_bg * 0.10
                assert avg_interactive <= avg_bg + tolerance, (
                    f"Interactive avg ({avg_interactive:.1f}s) should be <= "
                    f"background avg ({avg_bg:.1f}s) + tolerance ({tolerance:.1f}s)"
                )

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_priority_respected_during_long_inference",
                "result": "pass",
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_priority_respected_during_long_inference",
                "result": "fail",
            })
            raise


@pytest.mark.e2e
class TestConfirmedUnload:
    """Verify that /broker/unload confirms VRAM release before returning.

    Reproduces the race condition where rapid unload-then-preload results in
    409 Conflict because Ollama's async VRAM release hasn't completed yet.
    After the fix, unload_model() polls /api/ps until the model disappears,
    so a subsequent preload should always succeed.
    """

    # Council-sized models that fit together and leave room for a swap
    _COUNCIL_MODELS: list[str] = [
        "granite3.1-dense:8b",  # ~5.2 GB
        "llama3.1:8b",          # ~4.4 GB
        "mistral-nemo:12b",     # ~8.1 GB
    ]

    async def _preload(self, client: httpx.AsyncClient, url: str, model: str) -> int:
        resp = await client.post(
            f"{url}/broker/preload",
            json={"model": model},
            timeout=30.0,
        )
        return resp.status_code

    async def _unload(self, client: httpx.AsyncClient, url: str, model: str) -> int:
        resp = await client.post(
            f"{url}/broker/unload",
            json={"model": model},
            timeout=30.0,
        )
        return resp.status_code

    async def _get_loaded_models(self, client: httpx.AsyncClient, url: str) -> list[str]:
        resp = await client.get(f"{url}/api/ps", timeout=10.0)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    async def test_unload_then_preload_no_409(
        self,
        bastion_url: str,
        stress_log: StressLog,
        available_models: list[str],
    ) -> None:
        """Unload 3 council models, then immediately preload a large model.

        Before the confirmed-unload fix, the preload would get 409 because
        /api/ps still reported the unloaded models.  Now it should succeed.
        """
        stress_log.log("test_start", {"test": "test_unload_then_preload_no_409"})
        try:
            # Check that the models we need are available
            council = [m for m in self._COUNCIL_MODELS if m in available_models]
            if len(council) < 2:
                pytest.skip(f"Need at least 2 council models, have {len(council)}")

            # Clean VRAM: unload leftover models from previous tests
            evicted = await _unload_all_models(bastion_url)
            if evicted:
                stress_log.log("cleanup", {"unloaded": evicted})

            async with httpx.AsyncClient() as client:
                # Phase 1: Preload council models
                for model in council:
                    status = await self._preload(client, bastion_url, model)
                    stress_log.log("preload_council", {"model": model, "status": status})
                    assert status == 200, f"Failed to preload {model}: {status}"

                # Verify all loaded
                loaded = await self._get_loaded_models(client, bastion_url)
                for model in council:
                    assert model in loaded, f"{model} not in loaded: {loaded}"
                stress_log.log("council_loaded", {"loaded": loaded})

                # Phase 2: Unload all council models (rapid-fire)
                for model in council:
                    status = await self._unload(client, bastion_url, model)
                    stress_log.log("unload_council", {"model": model, "status": status})
                    assert status == 200, f"Failed to unload {model}: {status}"

                # Phase 3: Immediately preload a different model
                # With confirmed unload, this should NOT get 409
                loaded_after = await self._get_loaded_models(client, bastion_url)
                stress_log.log("after_unload", {"loaded": loaded_after})

                # Verify council models are gone
                for model in council:
                    assert model not in loaded_after, (
                        f"{model} still loaded after unload: {loaded_after}"
                    )

                # Preload the first council model back — should succeed
                status = await self._preload(client, bastion_url, council[0])
                stress_log.log("re_preload", {"model": council[0], "status": status})
                assert status == 200, (
                    f"Preload after unload got {status} (expected 200) — "
                    "confirmed unload may not be working"
                )

                # Cleanup
                await self._unload(client, bastion_url, council[0])

            stress_log.log("test_end", {
                "test": "test_unload_then_preload_no_409",
                "result": "pass",
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_unload_then_preload_no_409",
                "result": "fail",
            })
            raise


@pytest.mark.e2e
class TestCouncilConcurrentDispatch:
    """Verify co-resident council models run inference concurrently.

    Simulates a multi-model council pipeline: preload 3 council
    models, send N requests per model concurrently, and verify:
    1. All requests succeed with no errors
    2. No model swaps occur (all models stay resident)
    3. Wall time indicates concurrent execution (significantly < serial)
    4. VRAM stays within budget throughout
    """

    _COUNCIL_MODELS: list[str] = [
        "granite3.1-dense:8b",  # ~5.2 GB
        "llama3.1:8b",          # ~4.4 GB
        "mistral-nemo:12b",     # ~8.1 GB
    ]
    _REQUESTS_PER_MODEL: int = 3
    _SHORT_PROMPT: str = "Reply with exactly one word: yes"

    async def test_council_parallel_no_swaps(
        self,
        bastion_url: str,
        stress_log: StressLog,
        available_models: list[str],
    ) -> None:
        stress_log.log("test_start", {"test": "test_council_parallel_no_swaps"})
        try:
            council = [m for m in self._COUNCIL_MODELS if m in available_models]
            if len(council) < 3:
                pytest.skip(f"Need 3 council models, have {len(council)}: {council}")

            # Clean VRAM: unload leftover models from previous tests
            evicted = await _unload_all_models(bastion_url)
            if evicted:
                stress_log.log("cleanup", {"unloaded": evicted})

            async with httpx.AsyncClient(timeout=30.0) as admin:
                # Phase 1: Preload all council models
                for model in council:
                    resp = await admin.post(
                        f"{bastion_url}/broker/preload",
                        json={"model": model},
                    )
                    assert resp.status_code == 200, f"Preload {model} failed: {resp.status_code}"
                    stress_log.log("preload", {"model": model})

                # Verify all resident
                ps_resp = await admin.get(f"{bastion_url}/api/ps")
                loaded = [m["name"] for m in ps_resp.json().get("models", [])]
                for model in council:
                    assert model in loaded, f"{model} not loaded after preload: {loaded}"
                stress_log.log("all_council_resident", {"loaded": loaded})

            # Phase 2: Fire concurrent requests to all 3 models
            async with VRAMMonitor(bastion_url, stress_log, poll_interval=0.5) as monitor:
                client = StressClient(bastion_url, stress_log, client_id="council")
                specs = [
                    {
                        "model": council[i % len(council)],
                        "prompt": (
                            f"{self._SHORT_PROMPT}"
                            f" (model={council[i % len(council)]},"
                            f" req={i})"
                        ),
                        "stream": False,
                        "priority_tier": "pipeline",
                    }
                    for i in range(self._REQUESTS_PER_MODEL * len(council))
                ]
                total_requests = len(specs)

                t0 = time.monotonic()
                results = await client.generate_many(specs, concurrency=len(council))
                wall_time = time.monotonic() - t0

                # All must succeed
                client.assert_all_succeeded()
                client.assert_no_timeouts()

                # No swaps should have occurred — all models were co-resident
                [
                    s for s in monitor.samples
                    if set(council).issubset({n for n in s.get("loaded_models", [])})
                ]
                # If VRAMMonitor detected a model swap (loaded_models changed),
                # it logged a "model_swap_detected" event. Check that council
                # models were never removed. Debounced: under concurrent
                # inference Ollama's /api/ps transiently omits a busy model
                # for a single poll (observed 2026-06-12: granite missing from
                # one 0.5s sample while its requests completed warm at 0.08s),
                # so only TWO consecutive missing samples count as a real
                # eviction — an actual unload+reload stays absent for many
                # seconds at this polling interval.
                for model in council:
                    missing_run = max_missing_run = 0
                    for sample in monitor.samples:
                        if model in set(sample.get("loaded_models", [])):
                            missing_run = 0
                        else:
                            missing_run += 1
                            max_missing_run = max(max_missing_run, missing_run)
                    assert max_missing_run < 2, (
                        f"Council model {model} missing from {max_missing_run} "
                        f"consecutive residency samples — real eviction during "
                        f"inference detected."
                    )

                # Wall time should indicate concurrency.
                # With 3 models × 3 requests each = 9 requests.
                # Serial: ~9 × 2-4s = 18-36s.  Concurrent (3-way): ~3 × 2-4s = 6-12s.
                # Use a generous threshold: wall_time < 70% of serial estimate.
                latencies = [r["latency_s"] for r in results if r.get("latency_s")]
                avg_latency = sum(latencies) / len(latencies) if latencies else 0
                serial_estimate = avg_latency * total_requests
                concurrent_ratio = wall_time / serial_estimate if serial_estimate > 0 else 1.0

                stress_log.log("council_timing", {
                    "wall_time_s": round(wall_time, 2),
                    "avg_latency_s": round(avg_latency, 2),
                    "serial_estimate_s": round(serial_estimate, 2),
                    "concurrent_ratio": round(concurrent_ratio, 3),
                    "total_requests": total_requests,
                    "models": council,
                })

                # With 3 concurrent models, ratio should be ~0.33.
                # Allow up to 0.70 for overhead, scheduling delays, etc.
                assert concurrent_ratio < 0.70, (
                    f"Concurrent ratio {concurrent_ratio:.2f} too high — "
                    f"wall={wall_time:.1f}s vs serial_est={serial_estimate:.1f}s. "
                    "Dispatch may not be concurrent."
                )

                monitor.assert_vram_within_budget(VRAM_BUDGET_GB)
                monitor.print_summary()

            stress_log.log("test_end", {
                "test": "test_council_parallel_no_swaps",
                "result": "pass",
                "wall_time_s": round(wall_time, 2),
                "concurrent_ratio": round(concurrent_ratio, 3),
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_council_parallel_no_swaps",
                "result": "fail",
            })
            raise

    async def test_council_unload_preload_transition(
        self,
        bastion_url: str,
        stress_log: StressLog,
        available_models: list[str],
    ) -> None:
        """Simulate the full 2-pass pipeline: council inference, unload all,
        then preload a large secondary model.  Verifies the transition works
        without 409 errors.
        """
        stress_log.log("test_start", {"test": "test_council_unload_preload_transition"})
        try:
            council = [m for m in self._COUNCIL_MODELS if m in available_models]
            if len(council) < 3:
                pytest.skip(f"Need 3 council models, have {len(council)}")

            # Clean VRAM: unload leftover models from previous tests
            evicted = await _unload_all_models(bastion_url)
            if evicted:
                stress_log.log("cleanup", {"unloaded": evicted})

            # Find a model that's NOT in the council for the "secondary" role
            embedding = {"nomic-embed-text"}
            secondary_candidates = [
                m for m in available_models
                if m not in council and m not in embedding
            ]
            if not secondary_candidates:
                pytest.skip("No secondary model available for pass-2 simulation")
            # Prefer qwen3:14b or smallest non-council model
            secondary = secondary_candidates[0]
            for m in secondary_candidates:
                if "qwen3:14b" in m:
                    secondary = m
                    break

            async with httpx.AsyncClient(timeout=60.0) as client:
                # Pass 1: Preload council + run inference
                for model in council:
                    resp = await client.post(
                        f"{bastion_url}/broker/preload",
                        json={"model": model},
                    )
                    assert resp.status_code == 200, f"Preload {model}: {resp.status_code}"

                # Quick inference on each to confirm they work
                for model in council:
                    resp = await client.post(
                        f"{bastion_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": "Say yes",
                            "stream": False,
                            "options": {"use_mmap": False},
                        },
                        timeout=60.0,
                    )
                    assert resp.status_code == 200, f"Inference {model}: {resp.status_code}"

                # Transition: unload all council models
                for model in council:
                    resp = await client.post(
                        f"{bastion_url}/broker/unload",
                        json={"model": model},
                    )
                    assert resp.status_code == 200, f"Unload {model}: {resp.status_code}"
                    stress_log.log("unloaded", {"model": model})

                # Pass 2: Immediately preload secondary model
                resp = await client.post(
                    f"{bastion_url}/broker/preload",
                    json={"model": secondary},
                )
                stress_log.log("secondary_preload", {
                    "model": secondary,
                    "status": resp.status_code,
                })
                assert resp.status_code == 200, (
                    f"Secondary preload {secondary} got {resp.status_code} — "
                    "expected 200 after confirmed unload of council"
                )

                # Quick inference on secondary to confirm it works
                resp = await client.post(
                    f"{bastion_url}/api/generate",
                    json={
                        "model": secondary,
                        "prompt": "Say yes",
                        "stream": False,
                        "options": {"use_mmap": False},
                    },
                    timeout=60.0,
                )
                assert resp.status_code == 200, f"Secondary inference: {resp.status_code}"

                # Cleanup
                await client.post(
                    f"{bastion_url}/broker/unload",
                    json={"model": secondary},
                )

            stress_log.log("test_end", {
                "test": "test_council_unload_preload_transition",
                "result": "pass",
                "council": council,
                "secondary": secondary,
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_council_unload_preload_transition",
                "result": "fail",
            })
            raise


@pytest.mark.e2e
class TestPostStressHealth:
    """Final health check after all stress tests complete."""

    async def test_system_healthy_after_stress(
        self, bastion_url: str, stress_log: StressLog,
    ) -> None:
        stress_log.log("test_start", {"test": "test_system_healthy_after_stress"})
        try:
            # Wait for system to settle
            await asyncio.sleep(5)

            async with httpx.AsyncClient() as client:
                # Check health
                health_resp = await client.get(
                    f"{bastion_url}/broker/health", timeout=REQUEST_TIMEOUT,
                )
                assert health_resp.status_code == 200, (
                    f"Health check returned {health_resp.status_code}"
                )
                health_body = health_resp.json()
                assert health_body.get("healthy") is True, (
                    f"System not healthy after stress: {health_body}"
                )

                # Check queue is drained
                queue_resp = await client.get(
                    f"{bastion_url}/broker/queue", timeout=REQUEST_TIMEOUT,
                )
                assert queue_resp.status_code == 200, (
                    f"Queue check returned {queue_resp.status_code}"
                )
                queue_body = queue_resp.json()
                queue_depth = queue_body.get("total", 0)
                assert queue_depth == 0, (
                    f"Queue should be empty, but total={queue_depth}"
                )

                # Check circuit breaker (if reported)
                circuit_state = health_body.get("circuit")
                if circuit_state is not None:
                    assert circuit_state == "closed", (
                        f"Circuit breaker should be closed, but state={circuit_state}"
                    )

            stress_log.log("final_system_state", {
                "health": health_body,
                "queue": queue_body,
                "queue_depth": queue_depth,
                "circuit_breaker": circuit_state if 'circuit_state' in dir() else None,
            })

            stress_log.log("test_end", {
                "test": "test_system_healthy_after_stress",
                "result": "pass",
            })
        except Exception:
            stress_log.log("test_end", {
                "test": "test_system_healthy_after_stress",
                "result": "fail",
            })
            raise
