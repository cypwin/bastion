"""A2A (Agent-to-Agent) protocol handler for BASTION.

Implements the A2A protocol interface, turning BASTION into a discoverable
GPU broker that A2A-compliant agents can find and use for model inference,
batch processing, and model reservations.

The implementation provides four main skills:
  - infer: Single-prompt inference through the scheduler pipeline
  - status: Current broker/queue state (wraps /broker/status)
  - batch_infer: Batch inference with single-model-load guarantee (stub)
  - preload: Model reservation to prevent eviction (stub)

Task lifecycle follows the A2A spec:
  submitted -> working -> completed (or failed)
  submitted -> canceled (via DELETE)

SSE streaming is supported via /a2a/tasks/{task_id}/stream for real-time
status and artifact updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from bastion import audit
from bastion.circuitbreaker import CircuitBreaker, CircuitOpenError
from bastion.metrics import (
    emit_a2a_error,
    emit_a2a_task,
    observe_a2a_queue_wait,
    observe_a2a_task_duration,
    observe_llm_ttft,
    update_a2a_tasks_active,
)
from bastion.models import (
    A2ATaskRecord,
    A2ATaskState,
    BrokerConfig,
    LeaseState,
    ModelLease,
    PriorityTier,
    QueuedRequest,
    Reservation,
)
from bastion.taskstore import CompactedResult, TaskStore, TaskStoreFullError
from bastion.telemetry import end_span, record_task_process, record_task_submit
from bastion.vram import VRAMTracker

logger = logging.getLogger(__name__)


class _NoopAsyncCm:
    """Async context-manager wrapper that yields a pre-existing object.

    Used to wrap a shared ``httpx.AsyncClient`` so that it can be used in
    the same ``async with`` pattern as a freshly created client, without
    actually closing the shared instance on ``__aexit__``.
    """

    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *args: Any) -> None:
        pass  # Do NOT close the shared client


# Detect A2A SDK availability without importing unused names
try:
    import a2a.types  # noqa: F401

    A2A_SDK_AVAILABLE = True
except ImportError:
    A2A_SDK_AVAILABLE = False


class A2AHandler:
    """A2A protocol handler for BASTION.

    Manages the A2A task lifecycle, routes incoming tasks to skill handlers,
    and integrates with the existing AffinityQueue/Scheduler pipeline.

    Parameters
    ----------
    config : BrokerConfig
        Broker configuration (a2a section provides A2A-specific settings).
    enqueue_fn : callable
        Callback to place requests in the AffinityQueue. Same function used
        by OllamaProxy. Signature: async def(QueuedRequest) -> asyncio.Event
    vram_tracker : VRAMTracker
        VRAM state tracker for capability negotiation.
    scheduler : Scheduler
        Scheduler reference (for reservation priority elevation if needed).
    circuit_breaker : CircuitBreaker, optional
        Circuit breaker instance for fast-fail when Ollama is down.
        When the circuit is open, ``create_task`` returns a JSON-RPC
        error (-32050) instead of accepting new inference tasks.
    http_client : httpx.AsyncClient, optional
        Shared httpx client (optionally wrapped with CircuitBreakerTransport).
        When provided, A2A handlers use this instead of creating per-request
        clients.
    """

    def __init__(
        self,
        config: BrokerConfig,
        enqueue_fn: Callable[
            [QueuedRequest],
            Awaitable[tuple[asyncio.Event, Callable[[], None], Callable[[], None]]],
        ],
        vram_tracker: VRAMTracker,
        scheduler: Any,  # Avoid circular import
        circuit_breaker: CircuitBreaker | None = None,
        http_client: httpx.AsyncClient | None = None,
        task_store: TaskStore | None = None,
    ) -> None:
        self._config = config
        self._enqueue_fn = enqueue_fn
        self._vram = vram_tracker
        self._scheduler = scheduler
        self._circuit_breaker = circuit_breaker
        self._http_client = http_client

        # Hardened task store — accepts pre-wrapped PersistentTaskStore
        self._store = task_store or TaskStore(
            maxsize=10_000,
            task_ttl_seconds=config.a2a.task_ttl_seconds,
            completed_ttl_seconds=config.a2a.task_ttl_seconds,
        )
        self._store.start_cleanup()

        # Active model reservations (reservation_id -> Reservation)
        self._reservations: dict[str, Reservation] = {}

        # Hybrid leases (lease_id -> ModelLease) — upgrade from simple reservations
        self._leases: dict[str, ModelLease] = {}
        self._fencing_counter: int = 0

        # Skill routing table
        self._skill_handlers: dict[str, Callable] = {
            "infer": self._handle_infer,
            "status": self._handle_status,
            "batch_infer": self._handle_batch_infer,
            "preload": self._handle_preload,
        }

        # GC prevention: hold strong references to fire-and-forget tasks
        self._background_tasks: set[asyncio.Task] = set()

        # Start background cleanup task for expired reservations
        self._spawn_background_task(self._cleanup_expired_reservations())

        logger.info("A2A handler initialized (enabled=%s)", config.a2a.enabled)

    def _spawn_background_task(self, coro: Any) -> asyncio.Task:
        """Create an asyncio Task with GC prevention.

        Stores a strong reference in ``_background_tasks`` and auto-removes
        it via ``add_done_callback`` when the task finishes.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ── Task Lifecycle ──────────────────────────────────────────────────

    async def create_task(self, message: dict) -> dict:
        """Create a new A2A task from an incoming message.

        Parameters
        ----------
        message : dict
            A2A Message object (simplified format or full SDK format).

        Returns
        -------
        dict
            A2A Task object with id, status, contextId.

        Flow
        ----
        1. Generate task_id (uuid4 hex, 12 chars)
        2. Extract skill_id from message
        3. Validate skill_id against handlers
        4. Create A2ATaskRecord in "submitted" state
        5. Launch skill handler as asyncio.Task (fire-and-forget)
        6. Return A2A Task object
        """
        # Fast-fail if the Ollama backend circuit breaker is open.
        # Return a JSON-RPC -32050 error so A2A clients can back off.
        if self._circuit_breaker and self._circuit_breaker.state == "open":
            remaining = self._circuit_breaker._recovery_remaining()
            emit_a2a_error(method="create_task", error_code="-32050")
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32050,
                    "message": "Backend resource unavailable",
                    "data": {
                        "reason": "LLM service temporarily unavailable",
                        "retryAfter": int(remaining),
                    },
                },
            }

        task_id = uuid.uuid4().hex[:12]
        context_id = message.get("contextId", uuid.uuid4().hex[:12])

        # Extract skill_id and input_params from message
        skill_id = message.get("skill_id") or message.get("skillId", "")
        input_params = message.get("input_params") or message.get("params", {})

        # Fallback: try to extract from parts if present (A2A SDK format)
        if not skill_id and "parts" in message:
            for part in message["parts"]:
                if part.get("kind") == "data":
                    data = part.get("data", {})
                    skill_id = data.get("skill_id") or data.get("skillId", "")
                    if not input_params:
                        input_params = data.get("params", {})
                    break

        # Validate skill_id
        if not skill_id:
            # Create task in failed state
            record = A2ATaskRecord(
                task_id=task_id,
                context_id=context_id,
                state=A2ATaskState.FAILED,
                skill_id="unknown",
                input_params={},
                error="Missing skill_id in message",
            )
            try:
                self._store.create(record)
            except TaskStoreFullError as e:
                return {"error": str(e), "retry_after": e.retry_after}
            emit_a2a_task(skill="unknown", state="failed")
            emit_a2a_error(method="create_task", error_code="missing_skill")
            return self._task_to_dict(record)

        if skill_id not in self._skill_handlers:
            record = A2ATaskRecord(
                task_id=task_id,
                context_id=context_id,
                state=A2ATaskState.FAILED,
                skill_id=skill_id,
                input_params=input_params,
                error=f"Unknown skill: {skill_id}",
            )
            try:
                self._store.create(record)
            except TaskStoreFullError as e:
                return {"error": str(e), "retry_after": e.retry_after}
            emit_a2a_task(skill=skill_id, state="failed")
            emit_a2a_error(method="create_task", error_code="unknown_skill")
            return self._task_to_dict(record)

        # Create task record in submitted state
        record = A2ATaskRecord(
            task_id=task_id,
            context_id=context_id,
            state=A2ATaskState.SUBMITTED,
            skill_id=skill_id,
            input_params=input_params,
        )
        try:
            self._store.create(record)
        except TaskStoreFullError as e:
            return {"error": str(e), "retry_after": e.retry_after}

        # Emit metric: task submitted
        emit_a2a_task(skill=skill_id, state="submitted")
        update_a2a_tasks_active(state="submitted", count=self._store.count_by_state("submitted"))

        # Emit telemetry: PRODUCER span for task submission
        model = input_params.get("model", "") if isinstance(input_params, dict) else ""
        trace_ctx = record_task_submit(task_id, skill_id, model)

        # Launch skill handler (GC-prevented background task)
        handler = self._skill_handlers[skill_id]
        self._spawn_background_task(self._run_skill_handler(handler, record, trace_ctx))

        logger.info("A2A task created: %s (skill=%s)", task_id, skill_id)
        return self._task_to_dict(record)

    async def get_task(self, task_id: str) -> dict | None:
        """Return current task state, artifacts, and status.

        Parameters
        ----------
        task_id : str
            Task ID to retrieve.

        Returns
        -------
        dict or None
            A2A Task object, or None if not found. Returns a summary
            dict for compacted (terminal) tasks.
        """
        result = self._store.get(task_id)
        if result is None:
            return None
        if isinstance(result, CompactedResult):
            return {
                "id": result.task_id,
                "status": {
                    "state": result.status,
                    "message": result.error,
                },
                "artifacts": list(result.output_artifacts),
                "result_summary": result.result_summary,
            }
        return self._task_to_dict(result)

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a task if it's still in submitted/working state.

        Parameters
        ----------
        task_id : str
            Task ID to cancel.

        Returns
        -------
        bool
            True if canceled, False if not found or already completed/failed.
        """
        record = self._store.get_active(task_id)
        if record is None:
            return False

        # Can only cancel submitted or working tasks
        if record.state not in (A2ATaskState.SUBMITTED, A2ATaskState.WORKING):
            return False

        try:
            self._store.update_state(task_id, A2ATaskState.CANCELED)
        except (KeyError, ValueError):
            return False
        await self._notify_subscribers(
            task_id,
            {"statusUpdate": self._status_update_event(record)},
        )
        logger.info("A2A task canceled: %s", task_id)
        return True

    async def subscribe_task(self, task_id: str, request: Any = None) -> AsyncGenerator[dict, None]:
        """SSE event generator for task status/artifact updates.

        Hardened implementation with:
        - Bounded queues with drop-oldest on full
        - Client disconnect detection via request.is_disconnected()
        - Proper heartbeat as SSE comment format
        - Sentinel value (None) for clean shutdown
        - CancelledError re-raise (asyncio contract)

        Parameters
        ----------
        task_id : str
            Task ID to subscribe to.
        request : Request, optional
            FastAPI Request object for disconnect detection.

        Yields
        ------
        dict or None
            SSE event dict (statusUpdate or artifactUpdate), heartbeat marker,
            or None sentinel.
        """
        if not self._store.has_task(task_id):
            raise ValueError(f"Task not found: {task_id}")

        queue = self._store.subscribe(task_id)

        try:
            # Send initial status from active store if available
            record = self._store.get_active(task_id)
            if record is not None:
                yield {"statusUpdate": self._status_update_event(record)}

            # Stream updates with heartbeat and disconnect detection
            while True:
                # Check for client disconnect
                if (
                    request is not None
                    and hasattr(request, 'is_disconnected')
                    and await request.is_disconnected()
                ):
                    logger.debug("SSE client disconnected for task %s", task_id)
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    # Send heartbeat marker (handled by _sse_wrapper as SSE comment)
                    yield {"_heartbeat": True}
                    continue

                # Sentinel value for clean shutdown
                if event is None:
                    break

                yield event

                # Stop streaming if task is terminal
                if "statusUpdate" in event:
                    state = event["statusUpdate"]["status"]["state"]
                    if state in ("completed", "failed", "canceled"):
                        break

        except asyncio.CancelledError:
            logger.debug("SSE generator cancelled for task %s", task_id)
            raise  # MUST re-raise per asyncio contract

        finally:
            self._store.unsubscribe(task_id, queue)

    # ── Skill Handlers ──────────────────────────────────────────────────

    def _safe_transition(self, task_id: str, new_state: A2ATaskState) -> bool:
        """Transition task state through the TaskStore.

        Uses ``TaskStore.update_state`` to validate the transition, set
        ``record.state`` and ``record.updated_at``, and compact terminal
        tasks into the completed store.

        Returns True on success.  Returns False if the task was already
        compacted (KeyError) or the transition is invalid (ValueError),
        logging at DEBUG level.
        """
        try:
            self._store.update_state(task_id, new_state)
            return True
        except KeyError:
            logger.debug(
                "State transition skipped for %s -> %s: task not in active store",
                task_id, new_state.value,
            )
            return False
        except ValueError as exc:
            logger.debug(
                "State transition skipped for %s -> %s: %s",
                task_id, new_state.value, exc,
            )
            return False

    async def _run_skill_handler(
        self,
        handler: Callable,
        record: A2ATaskRecord,
        trace_context: dict[str, str] | None = None,
    ) -> None:
        """Wrapper to run a skill handler and catch exceptions.

        Transitions task to working, calls handler, handles errors.

        Parameters
        ----------
        handler : Callable
            The skill handler coroutine.
        record : A2ATaskRecord
            Task record being processed.
        trace_context : dict, optional
            Serialized trace context from task submission (for span linking).
        """
        # Start a CONSUMER telemetry span linked to the producer
        model = (
            record.input_params.get("model", "")
            if isinstance(record.input_params, dict)
            else ""
        )
        process_span = record_task_process(
            record.task_id, record.skill_id, model, trace_context,
        )

        try:
            # Transition to working (validates SUBMITTED -> WORKING)
            if not self._safe_transition(record.task_id, A2ATaskState.WORKING):
                end_span(process_span, error="Task no longer active")
                return
            await self._notify_subscribers(
                record.task_id,
                {"statusUpdate": self._status_update_event(record)},
            )

            # Record queue wait time (submitted -> working)
            observe_a2a_queue_wait(
                skill=record.skill_id,
                model=model or "none",
                wait_seconds=time.time() - record.created_at,
            )

            # Run the handler
            await handler(record)

            # Record end-to-end task duration on success
            observe_a2a_task_duration(
                skill=record.skill_id,
                model=model or "none",
                state=record.state.value,
                duration=time.time() - record.created_at,
            )

            # End the telemetry span (success)
            end_span(process_span)

        except CircuitOpenError as e:
            logger.warning(
                "A2A skill handler circuit open (task=%s, skill=%s): %s",
                record.task_id, record.skill_id, e,
            )
            record.error = f"Backend unavailable (circuit breaker open): {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            observe_a2a_task_duration(
                skill=record.skill_id, model=model or "none",
                state="failed", duration=time.time() - record.created_at,
            )
            end_span(process_span, error=str(e))
        except Exception as e:
            logger.exception(
                "A2A skill handler error (task=%s, skill=%s)",
                record.task_id, record.skill_id,
            )
            record.error = str(e)
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            observe_a2a_task_duration(
                skill=record.skill_id, model=model or "none",
                state="failed", duration=time.time() - record.created_at,
            )
            end_span(process_span, error=str(e))

    async def _handle_infer(self, record: A2ATaskRecord) -> None:
        """Handle the 'infer' skill: single prompt -> result.

        Flow
        ----
        1. Extract model, prompt, system_prompt, options from input_params
        2. Build Ollama-format payload
        3. Create QueuedRequest and enqueue via _enqueue_fn
        4. Await grant event (scheduler loads model)
        5. Forward to Ollama via httpx
        6. Collect response (streaming or non-streaming)
        7. Store result as artifact on the task record
        8. Transition task to "completed"
        """
        params = record.input_params
        model = params.get("model")
        prompt = params.get("prompt", "")
        system_prompt = params.get("system_prompt")
        options = params.get("options", {})
        stream = params.get("stream", False)

        if not model:
            record.error = "Missing 'model' parameter"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            return

        # Build Ollama payload
        ollama_payload = {
            "model": model,
            "prompt": prompt,
            "stream": stream,
            "options": {**options, "use_mmap": False},  # GPU crash prevention
        }
        if system_prompt:
            ollama_payload["system"] = system_prompt

        # Enqueue request
        queued = QueuedRequest(
            model=model,
            endpoint="/api/generate",
            body=json.dumps(ollama_payload).encode(),
            priority=self._config.priorities.agent,
            base_priority=self._config.priorities.agent,
            tier=PriorityTier.AGENT,
            client_info=f"a2a:{record.task_id}",
        )

        try:
            grant_event, _done_fn, cancel_fn = await self._enqueue_fn(queued)
        except RuntimeError as e:
            # Queue full or draining
            record.error = f"Queue full: {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            return

        # Wait for grant (with timeout)
        queue_timeout = self._config.proxy.queue_timeout_seconds
        try:
            await asyncio.wait_for(grant_event.wait(), timeout=queue_timeout)
        except TimeoutError:
            cancel_fn()  # Clean up ghost request from all tracking structures
            record.error = f"Queue timeout after {queue_timeout}s"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            return

        # Forward to Ollama
        ollama_url = f"{self._config.ollama.base_url}/api/generate"

        try:
            if stream:
                # Use streaming bridge
                await self._stream_ollama_to_sse(record, ollama_url, ollama_payload)
            else:
                # Non-streaming response — use shared client if available
                if self._http_client is not None:
                    resp = await self._http_client.post(ollama_url, json=ollama_payload)
                    resp.raise_for_status()
                    result = resp.json()
                else:
                    timeout = httpx.Timeout(
                        connect=self._config.proxy.connect_timeout_seconds,
                        read=self._config.proxy.inference_timeout_seconds,
                        write=10.0,
                        pool=10.0,
                    )
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        resp = await client.post(ollama_url, json=ollama_payload)
                        resp.raise_for_status()
                        result = resp.json()

                response_text = result.get("response", "")
                record.output_artifacts = [{
                    "artifact_id": "result",
                    "parts": [{"kind": "text", "text": response_text}],
                    "metadata": {
                        "model": model,
                        "eval_count": result.get("eval_count"),
                        "total_duration": result.get("total_duration"),
                    },
                }]

                # Mark completed (compacts to completed store)
                if self._safe_transition(record.task_id, A2ATaskState.COMPLETED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )

                # Tiered audit with A2A identity
                audit.emit_tiered(
                    event_type="a2a_infer_complete",
                    data={
                        "task_id": record.task_id,
                        "model": model,
                        "eval_count": result.get("eval_count"),
                        "total_duration": result.get("total_duration"),
                        "status": "completed",
                    },
                    a2a_identity={
                        "skill_id": record.skill_id,
                        "task_id": record.task_id,
                        "context_id": record.context_id,
                    },
                    prompt=prompt,
                    response=response_text,
                )

                logger.info("A2A infer completed: %s (model=%s)", record.task_id, model)

        except CircuitOpenError as e:
            record.error = f"Backend unavailable (circuit breaker open): {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.error("A2A infer circuit open: %s (error=%s)", record.task_id, e)

        except httpx.HTTPError as e:
            record.error = f"Ollama error: {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.error("A2A infer failed: %s (error=%s)", record.task_id, e)

    async def _stream_ollama_to_sse(
        self,
        record: A2ATaskRecord,
        ollama_url: str,
        payload: dict,
    ) -> None:
        """Bridge Ollama NDJSON streaming to A2A SSE events.

        Parameters
        ----------
        record : A2ATaskRecord
            Task record to update with streaming results.
        ollama_url : str
            Ollama endpoint URL to stream from.
        payload : dict
            Request payload to send to Ollama.

        Flow
        ----
        1. Open httpx streaming connection to Ollama with the payload
        2. For each NDJSON line from Ollama:
           a. Parse JSON chunk
           b. Extract token from chunk.get("response") (generate) or
              chunk.get("message", {}).get("content") (chat)
           c. Create artifact update event with the token
           d. Push via _notify_subscribers()
        3. On stream end (chunk.get("done") == True):
           a. Create final artifact with accumulated response
           b. Push final status update (state=completed)
           c. Store artifact on task record
        """
        timeout = httpx.Timeout(
            connect=self._config.proxy.connect_timeout_seconds,
            read=self._config.proxy.inference_timeout_seconds,
            write=10.0,
            pool=10.0,
        )

        accumulated_text = ""
        token_index = 0
        model = payload.get("model", "")
        stream_start_time: float = 0.0

        try:
            # Use shared client if available; otherwise create a per-request one
            use_shared = self._http_client is not None
            client_cm = (
                _NoopAsyncCm(self._http_client)
                if use_shared
                else httpx.AsyncClient(timeout=timeout)
            )
            async with client_cm as client, client.stream("POST", ollama_url, json=payload) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        # HTTP error from Ollama
                        record.error = f"Ollama HTTP {e.response.status_code}: {e}"
                        if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                            await self._notify_subscribers(
                                record.task_id,
                                {"statusUpdate": self._status_update_event(record)},
                            )
                        logger.error("A2A stream failed (HTTP): %s (error=%s)", record.task_id, e)
                        return

                    stream_start_time = time.time()

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue

                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError as e:
                            # Malformed JSON, log and skip
                            logger.warning(
                                "A2A stream: malformed JSON chunk (task=%s): %s",
                                record.task_id, e,
                            )
                            continue

                        # Extract token text
                        token = ""
                        if "response" in chunk:
                            # /api/generate format
                            token = chunk.get("response", "")
                        elif "message" in chunk:
                            # /api/chat format
                            token = chunk.get("message", {}).get("content", "")

                        if token:
                            # Record time to first token
                            if token_index == 0 and stream_start_time > 0:
                                observe_llm_ttft(model, time.time() - stream_start_time)

                            accumulated_text += token

                            # Push artifact update with token
                            await self._notify_subscribers(
                                record.task_id,
                                {
                                    "artifactUpdate": {
                                        "taskId": record.task_id,
                                        "artifact": {
                                            "artifact_id": f"token-{token_index}",
                                            "parts": [{"kind": "text", "text": token}],
                                        },
                                        "timestamp": datetime.now(UTC).isoformat(),
                                    }
                                },
                            )
                            token_index += 1

                        # Check for stream completion
                        if chunk.get("done"):
                            # Store final artifact
                            record.output_artifacts = [
                                {
                                    "artifact_id": "result",
                                    "parts": [{"kind": "text", "text": accumulated_text}],
                                    "metadata": {
                                        "model": model,
                                        "stream": True,
                                        "eval_count": chunk.get("eval_count"),
                                        "total_duration": chunk.get("total_duration"),
                                    },
                                }
                            ]

                            # Mark completed (compacts to completed store)
                            if self._safe_transition(record.task_id, A2ATaskState.COMPLETED):
                                await self._notify_subscribers(
                                    record.task_id,
                                    {"statusUpdate": self._status_update_event(record)},
                                )
                            logger.info(
                                "A2A stream completed: %s (model=%s, tokens=%d)",
                                record.task_id,
                                model,
                                token_index,
                            )
                            break

        except httpx.HTTPError as e:
            # Connection error or other HTTP issue
            record.error = f"Ollama stream error: {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.error("A2A stream failed: %s (error=%s)", record.task_id, e)

        except asyncio.CancelledError:
            # Client disconnected or task canceled
            logger.info("A2A stream canceled: %s", record.task_id)
            raise

        except Exception as e:
            # Unexpected error
            record.error = f"Stream processing error: {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.exception("A2A stream unexpected error: %s", record.task_id)

    async def _handle_status(self, record: A2ATaskRecord) -> None:
        """Handle the 'status' skill: return broker status.

        Wraps /broker/status as an A2A task response.
        Returns queue depth, loaded models, GPU health as a DataPart.
        """
        try:
            # Query current state
            loaded_raw = await self._vram.get_loaded_models() if self._vram else []
            # State-unknown sentinel (None) coerced to [] so the status skill
            # keeps answering during Ollama outages; vram_state tells clients
            # the list is unverified rather than genuinely empty.
            loaded = loaded_raw if loaded_raw is not None else []
            vram_state = "unknown" if loaded_raw is None else "ok"
            queue_depth = (
                self._scheduler.queue.total_size
                if self._scheduler and hasattr(self._scheduler, "queue")
                else 0
            )
            queue_by_model = (
                self._scheduler.queue.queue_depth_by_model()
                if self._scheduler and hasattr(self._scheduler, "queue")
                else {}
            )

            status_data = {
                "queue_depth": queue_depth,
                "queue_by_model": queue_by_model,
                "loaded_models": [m.name for m in loaded],
                "vram_state": vram_state,
                "current_model": self._scheduler.current_model if self._scheduler else None,
            }

            record.output_artifacts = [{
                "artifact_id": "status",
                "parts": [{"kind": "data", "data": status_data}],
            }]
            if self._safe_transition(record.task_id, A2ATaskState.COMPLETED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.info("A2A status completed: %s", record.task_id)

        except Exception as e:
            record.error = str(e)
            logger.exception(
                "A2A status handler error (task=%s)", record.task_id
            )
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )

    async def _handle_batch_infer(self, record: A2ATaskRecord) -> None:
        """Handle the 'batch_infer' skill: N prompts -> N results.

        Flow
        ----
        1. Parse BatchInferRequest from input_params
        2. Validate batch size <= config.a2a.max_batch_size
        3. Enqueue FIRST prompt through scheduler to ensure model is loaded
        4. After grant, create a Reservation to prevent model eviction during batch
        5. Process remaining prompts DIRECTLY via Ollama (bypass queue since model is loaded)
        6. Collect partial results with per-prompt status (index, status, response/error)
        7. Push artifact updates via _notify_subscribers as each prompt completes
        8. When all done, create BatchInferResult artifact
        9. Clean up reservation
        10. Transition task to completed
        """
        try:
            # Parse and validate request
            params = record.input_params
            model = params.get("model")
            prompts = params.get("prompts", [])
            system_prompt = params.get("system_prompt")
            options = params.get("options", {})
            priority = params.get("priority", self._config.priorities.agent)

            if not model:
                record.error = "Missing 'model' parameter"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            if not prompts:
                record.error = "Missing or empty 'prompts' parameter"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            if len(prompts) > self._config.a2a.max_batch_size:
                record.error = (
                    f"Batch size {len(prompts)} exceeds max"
                    f" {self._config.a2a.max_batch_size}"
                )
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Initialize results tracking
            results: list[dict[str, Any]] = []
            succeeded = 0
            failed = 0

            # Step 1: Enqueue first prompt through scheduler to load model
            first_ollama_payload = {
                "model": model,
                "prompt": prompts[0],
                "stream": False,
                "options": {**options, "use_mmap": False},
            }
            if system_prompt:
                first_ollama_payload["system"] = system_prompt

            queued = QueuedRequest(
                model=model,
                endpoint="/api/generate",
                body=json.dumps(first_ollama_payload).encode(),
                priority=priority,
                base_priority=priority,
                tier=PriorityTier.AGENT,
                client_info=f"a2a:batch:{record.task_id}",
            )

            try:
                grant_event, _done_fn, cancel_fn = await self._enqueue_fn(queued)
            except RuntimeError as e:
                record.error = f"Queue full: {e}"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Wait for grant with timeout
            queue_timeout = self._config.proxy.queue_timeout_seconds
            try:
                await asyncio.wait_for(grant_event.wait(), timeout=queue_timeout)
            except TimeoutError:
                cancel_fn()  # Clean up ghost request from all tracking structures
                record.error = f"Queue timeout after {queue_timeout}s"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Step 2: Create reservation to prevent model eviction
            reservation = Reservation(
                model=model,
                remaining_requests=len(prompts),
                priority=PriorityTier.INTERACTIVE,
                expires_at=time.time() + self._config.a2a.reservation_timeout_seconds,
            )
            self._reservations[reservation.reservation_id] = reservation
            logger.info(
                "Created reservation %s for batch task %s (model=%s, prompts=%d)",
                reservation.reservation_id,
                record.task_id,
                model,
                len(prompts),
            )

            # Step 3: Process all prompts via Ollama
            ollama_url = f"{self._config.ollama.base_url}/api/generate"
            timeout = httpx.Timeout(
                connect=self._config.proxy.connect_timeout_seconds,
                read=self._config.proxy.inference_timeout_seconds,
                write=10.0,
                pool=10.0,
            )

            # Use shared client if available; otherwise create per-request
            use_shared = self._http_client is not None
            client_cm = (
                _NoopAsyncCm(self._http_client)
                if use_shared
                else httpx.AsyncClient(timeout=timeout)
            )

            try:
                async with client_cm as client:
                    # First prompt
                    try:
                        resp = await client.post(ollama_url, json=first_ollama_payload)
                        resp.raise_for_status()
                        result = resp.json()
                        response_text = result.get("response", "")
                        results.append(
                            {"index": 0, "status": "completed", "response": response_text}
                        )
                        succeeded += 1
                        reservation.remaining_requests -= 1
                    except httpx.HTTPError as e:
                        results.append({"index": 0, "status": "failed", "error": str(e)})
                        failed += 1

                    # Push partial result
                    await self._notify_subscribers(
                        record.task_id,
                        {
                            "artifactUpdate": {
                                "taskId": record.task_id,
                                "artifact": {
                                    "artifact_id": "batch-progress",
                                    "parts": [
                                        {
                                            "kind": "data",
                                            "data": {
                                                "results": results.copy(),
                                                "total": len(prompts),
                                                "succeeded": succeeded,
                                                "failed": failed,
                                            },
                                        }
                                    ],
                                },
                                "timestamp": time.time(),
                            }
                        },
                    )

                    # Step 4: Process remaining prompts directly via Ollama
                    for idx, prompt in enumerate(prompts[1:], start=1):
                        ollama_payload = {
                            "model": model,
                            "prompt": prompt,
                            "stream": False,
                            "options": {**options, "use_mmap": False},
                        }
                        if system_prompt:
                            ollama_payload["system"] = system_prompt

                        try:
                            resp = await client.post(ollama_url, json=ollama_payload)
                            resp.raise_for_status()
                            result = resp.json()
                            response_text = result.get("response", "")
                            results.append(
                                {
                                    "index": idx,
                                    "status": "completed",
                                    "response": response_text,
                                }
                            )
                            succeeded += 1
                            reservation.remaining_requests -= 1
                        except httpx.HTTPError as e:
                            results.append({"index": idx, "status": "failed", "error": str(e)})
                            failed += 1

                        # Push partial result after each prompt
                        await self._notify_subscribers(
                            record.task_id,
                            {
                                "artifactUpdate": {
                                    "taskId": record.task_id,
                                    "artifact": {
                                        "artifact_id": "batch-progress",
                                        "parts": [
                                            {
                                                "kind": "data",
                                                "data": {
                                                    "results": results.copy(),
                                                    "total": len(prompts),
                                                    "succeeded": succeeded,
                                                    "failed": failed,
                                                },
                                            }
                                        ],
                                    },
                                    "timestamp": time.time(),
                                }
                            },
                        )

            finally:
                # Step 5: Clean up reservation
                if reservation.reservation_id in self._reservations:
                    del self._reservations[reservation.reservation_id]
                    logger.info(
                        "Cleaned up reservation %s for batch task %s",
                        reservation.reservation_id,
                        record.task_id,
                    )

            # Step 6: Create final artifact
            record.output_artifacts = [
                {
                    "artifact_id": "batch-result",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "results": results,
                                "total": len(prompts),
                                "succeeded": succeeded,
                                "failed": failed,
                            },
                        }
                    ],
                    "metadata": {"model": model, "batch_size": len(prompts)},
                }
            ]

            # Mark completed (compacts to completed store)
            if self._safe_transition(record.task_id, A2ATaskState.COMPLETED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            # Tiered audit with A2A identity
            audit.emit_tiered(
                event_type="a2a_batch_infer_complete",
                data={
                    "task_id": record.task_id,
                    "model": model,
                    "batch_size": len(prompts),
                    "succeeded": succeeded,
                    "failed": failed,
                    "status": "completed",
                },
                a2a_identity={
                    "skill_id": record.skill_id,
                    "task_id": record.task_id,
                    "context_id": record.context_id,
                },
            )

            logger.info(
                "A2A batch_infer completed: %s (model=%s, prompts=%d, succeeded=%d, failed=%d)",
                record.task_id,
                model,
                len(prompts),
                succeeded,
                failed,
            )

        except CircuitOpenError as e:
            record.error = f"Backend unavailable (circuit breaker open): {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.error("A2A batch_infer circuit open: %s (error=%s)", record.task_id, e)

        except httpx.HTTPError as e:
            record.error = f"Ollama error: {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.error("A2A batch_infer failed: %s (error=%s)", record.task_id, e)

    async def _handle_preload(self, record: A2ATaskRecord) -> None:
        """Handle the 'preload' skill: reserve model for N requests.

        Flow
        ----
        1. Parse ReservationRequest from input_params
        2. Validate model exists in config.models
        3. Check VRAM availability via VRAMTracker.can_load_model()
        4. Trigger model load by creating a minimal QueuedRequest (like /broker/preload does)
        5. Create Reservation:
           - remaining_requests = req.num_requests
           - expires_at = now + (req.timeout_seconds or config.a2a.reservation_timeout_seconds)
           - priority = req.priority
        6. Store in self._reservations[reservation_id]
        7. Return artifact with reservation_id, model, num_requests, expires_at
        8. Transition task to completed
        """
        try:
            # Parse and validate request
            params = record.input_params
            model = params.get("model")
            num_requests = params.get("num_requests", 10)
            timeout_seconds = params.get(
                "timeout_seconds", self._config.a2a.reservation_timeout_seconds
            )
            priority = params.get("priority", PriorityTier.INTERACTIVE)

            if not model:
                record.error = "Missing 'model' parameter"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Validate model exists in config
            if model not in self._config.models:
                record.error = f"Unknown model: {model}"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Validate num_requests
            if num_requests <= 0 or num_requests > self._config.a2a.reservation_max_requests:
                record.error = (
                    f"num_requests must be between 1 and"
                    f" {self._config.a2a.reservation_max_requests}"
                )
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Check VRAM availability
            can_load, reason = await self._vram.can_load_model(model)
            if not can_load:
                record.error = f"Cannot load model {model}: {reason}"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Trigger model load via scheduler (minimal generate request)
            load_payload = {
                "model": model,
                "prompt": "",
                "stream": False,
                "options": {"use_mmap": False},
            }

            queued = QueuedRequest(
                model=model,
                endpoint="/api/generate",
                body=json.dumps(load_payload).encode(),
                priority=(
                    priority if isinstance(priority, float)
                    else self._config.priorities.interactive
                ),
                base_priority=(
                    priority if isinstance(priority, float)
                    else self._config.priorities.interactive
                ),
                tier=PriorityTier.INTERACTIVE,
                client_info=f"a2a:preload:{record.task_id}",
            )

            try:
                grant_event, _done_fn, cancel_fn = await self._enqueue_fn(queued)
            except RuntimeError as e:
                record.error = f"Queue full: {e}"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Wait for grant (confirms model is loaded)
            queue_timeout = self._config.proxy.queue_timeout_seconds
            try:
                await asyncio.wait_for(grant_event.wait(), timeout=queue_timeout)
            except TimeoutError:
                cancel_fn()  # Clean up ghost request from all tracking structures
                record.error = f"Queue timeout after {queue_timeout}s"
                if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                    await self._notify_subscribers(
                        record.task_id,
                        {"statusUpdate": self._status_update_event(record)},
                    )
                return

            # Create reservation (backward compat)
            reservation = Reservation(
                model=model,
                remaining_requests=num_requests,
                priority=PriorityTier.INTERACTIVE,
                expires_at=time.time() + timeout_seconds,
            )
            self._reservations[reservation.reservation_id] = reservation

            logger.info(
                "Created reservation %s for model %s (requests=%d, timeout=%ds)",
                reservation.reservation_id,
                model,
                num_requests,
                timeout_seconds,
            )

            # Create hybrid lease (upgrade from simple reservation)
            lease = self.create_lease(
                model=model,
                max_requests=num_requests,
                ttl_seconds=timeout_seconds,
                idle_timeout=60.0,
            )

            # Return artifact with reservation and lease details
            record.output_artifacts = [
                {
                    "artifact_id": "reservation",
                    "parts": [
                        {
                            "kind": "data",
                            "data": {
                                "reservation_id": reservation.reservation_id,
                                "lease_id": lease.lease_id,
                                "fencing_token": lease.fencing_token,
                                "model": model,
                                "num_requests": num_requests,
                                "remaining_requests": num_requests,
                                "expires_at": reservation.expires_at,
                                "idle_timeout": 60.0,
                            },
                        }
                    ],
                    "metadata": {"model": model, "priority": str(priority)},
                }
            ]

            # Mark completed (compacts to completed store)
            if self._safe_transition(record.task_id, A2ATaskState.COMPLETED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            # Tiered audit with A2A identity
            audit.emit_tiered(
                event_type="a2a_preload_complete",
                data={
                    "task_id": record.task_id,
                    "model": model,
                    "reservation_id": reservation.reservation_id,
                    "lease_id": lease.lease_id,
                    "num_requests": num_requests,
                    "status": "completed",
                },
                a2a_identity={
                    "skill_id": record.skill_id,
                    "task_id": record.task_id,
                    "context_id": record.context_id,
                },
            )

            logger.info(
                "A2A preload completed: %s (model=%s, reservation=%s)",
                record.task_id,
                model,
                reservation.reservation_id,
            )

        except Exception as e:
            record.error = f"Preload error: {e}"
            if self._safe_transition(record.task_id, A2ATaskState.FAILED):
                await self._notify_subscribers(
                    record.task_id,
                    {"statusUpdate": self._status_update_event(record)},
                )
            logger.error("A2A preload failed: %s (error=%s)", record.task_id, e)

    # ── Reservation Management ──────────────────────────────────────────

    def has_active_reservation(self, model: str) -> bool:
        """Check if any active reservation exists for a model.

        Called by the scheduler before evicting a model.
        Also checks hybrid leases for active reservations.

        Parameters
        ----------
        model : str
            Model name to check.

        Returns
        -------
        bool
            True if an active reservation or lease exists, False otherwise.
        """
        now = time.time()
        for r in self._reservations.values():
            if r.model == model and r.remaining_requests > 0 and now < r.expires_at:
                return True
        # Also check hybrid leases
        return bool(self.has_active_lease(model))

    # ── Lease Management ───────────────────────────────────────────────

    def _next_fencing_token(self) -> int:
        """Generate next monotonically increasing fencing token."""
        self._fencing_counter += 1
        return self._fencing_counter

    def create_lease(
        self,
        model: str,
        max_requests: int = 100,
        ttl_seconds: float = 600.0,
        idle_timeout: float = 60.0,
    ) -> ModelLease:
        """Create a new model lease with fencing token.

        Parameters
        ----------
        model : str
            Model name to lease.
        max_requests : int
            Maximum number of requests allowed on this lease.
        ttl_seconds : float
            Absolute TTL in seconds from now.
        idle_timeout : float
            Seconds of inactivity before lease expires.

        Returns
        -------
        ModelLease
            The newly created lease with a unique fencing token.
        """
        lease = ModelLease(
            model=model,
            max_requests=max_requests,
            remaining_requests=max_requests,
            expiry=time.monotonic() + ttl_seconds,
            idle_timeout=idle_timeout,
            fencing_token=self._next_fencing_token(),
        )
        self._leases[lease.lease_id] = lease
        logger.info(
            "Lease created: %s model=%s requests=%d ttl=%.0fs idle=%.0fs token=%d",
            lease.lease_id, model, max_requests, ttl_seconds, idle_timeout, lease.fencing_token,
        )
        return lease

    def validate_lease(self, lease_id: str, fencing_token: int) -> tuple[bool, str]:
        """Validate a lease is active and fencing token matches.

        Parameters
        ----------
        lease_id : str
            Lease ID to validate.
        fencing_token : int
            Expected fencing token (must match current lease token).

        Returns
        -------
        tuple[bool, str]
            (valid, reason) — True if lease is active and token matches.
        """
        if lease_id not in self._leases:
            return False, "Lease not found"

        lease = self._leases[lease_id]

        if lease.fencing_token != fencing_token:
            return (
                False,
                f"Stale fencing token: got {fencing_token},"
                f" expected {lease.fencing_token}",
            )

        should_release, reason = lease.should_release()
        if should_release:
            return False, f"Lease expired: {reason}"

        return True, "OK"

    def release_lease(self, lease_id: str) -> bool:
        """Explicitly release a lease.

        Parameters
        ----------
        lease_id : str
            Lease ID to release.

        Returns
        -------
        bool
            True if lease was found and released, False otherwise.
        """
        if lease_id not in self._leases:
            return False

        lease = self._leases[lease_id]
        lease.state = LeaseState.RELEASED
        del self._leases[lease_id]
        logger.info("Lease released: %s model=%s", lease_id, lease.model)
        return True

    def has_active_lease(self, model: str) -> bool:
        """Check if any active lease exists for a model.

        Parameters
        ----------
        model : str
            Model name to check.

        Returns
        -------
        bool
            True if an active lease exists, False otherwise.
        """
        for lease in self._leases.values():
            should_release, _ = lease.should_release()
            if lease.model == model and not should_release:
                return True
        return False

    def get_snapshot(self, max_tasks: int = 5, max_leases: int = 5) -> dict:
        """Return a compact snapshot of current A2A state for dashboards.

        Returns
        -------
        dict
            {
              "summary": {"total": int, "submitted": int, "working": int,
                          "completed": int, "failed": int, "canceled": int},
              "tasks": [{"task_id": str, "state": str, "skill_id": str,
                         "model": str}, ...],  # most recent first, up to max_tasks
              "leases": [{"lease_id": str, "model": str, "state": str,
                          "remaining_requests": int,
                          "ttl_remaining": float}, ...]  # up to max_leases
            }
        """
        states = ["submitted", "working", "completed", "failed", "canceled"]
        summary: dict[str, int] = {s: self._store.count_by_state(s) for s in states}
        summary["total"] = sum(summary.values())

        # Most-recent active tasks
        active = self._store.list_active()[:max_tasks]
        tasks = [
            {
                "task_id": r.task_id,
                "state": r.state.value,
                "skill_id": r.skill_id,
                "model": (r.input_params or {}).get("model", ""),
            }
            for r in active
        ]

        leases_snapshot: list[dict] = []
        now = time.monotonic()
        for lease in list(self._leases.values())[:max_leases]:
            state_val = lease.state.value if hasattr(lease.state, "value") else str(lease.state)
            leases_snapshot.append(
                {
                    "lease_id": lease.lease_id,
                    "model": lease.model,
                    "state": state_val,
                    "remaining_requests": lease.remaining_requests,
                    "ttl_remaining": max(0.0, lease.expiry - now),
                }
            )

        return {"summary": summary, "tasks": tasks, "leases": leases_snapshot}

    async def _cleanup_expired_reservations(self) -> None:
        """Periodically clean up expired reservations and leases.

        Runs every 30 seconds to remove reservations that have expired
        or have no remaining requests, and leases that should be released.
        """
        while True:
            await asyncio.sleep(30)
            now = time.time()
            # Clean up reservations
            expired_res = [
                rid
                for rid, r in self._reservations.items()
                if now >= r.expires_at or r.remaining_requests <= 0
            ]
            for rid in expired_res:
                del self._reservations[rid]
                logger.debug("Cleaned up expired reservation: %s", rid)

            # Clean up leases
            expired_leases = [
                lid for lid, lease in self._leases.items()
                if lease.should_release()[0]
            ]
            for lid in expired_leases:
                lease = self._leases[lid]
                lease.state = LeaseState.EXPIRED
                del self._leases[lid]
                logger.debug(
                    "Cleaned up expired lease: %s (reason=%s)",
                    lid, lease.should_release()[1],
                )

    # ── Agent Card ──────────────────────────────────────────────────────

    def build_public_card(self) -> dict:
        """Build the Tier 1 public agent card (no auth required).

        Returns a stripped-down card with NO infrastructure details:
        no model names, no VRAM data, no queue depth, no GPU info.
        Only generic identity, protocol capabilities, broad skill
        categories, and authentication requirements.

        Returns
        -------
        dict
            A2A AgentCard with generic info and securitySchemes only.
        """
        card: dict[str, Any] = {
            "name": "BASTION GPU Inference Broker",
            "description": "GPU inference broker with scheduling, batching, and model management",
            "url": self._config.server.external_url,
            "version": __import__("bastion").__version__,
            "serviceEndpoint": f"{self._config.server.external_url}/a2a",
            "protocolVersion": "0.1",
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
            },
            "skills": [
                {"id": "infer", "name": "Single Prompt Inference"},
                {"id": "batch_infer", "name": "Batch Inference"},
                {"id": "preload", "name": "Preload Model"},
                {"id": "status", "name": "Broker Status"},
            ],
        }

        # Always advertise security schemes so unauthenticated callers
        # know how to authenticate for the extended card / task endpoints.
        card["securitySchemes"] = {
            "BearerToken": {
                "type": "http",
                "scheme": "bearer",
                "description": "A2A bearer token for task and extended card endpoints",
            }
        }
        card["security"] = [{"BearerToken": []}]

        return card

    async def build_extended_card(self) -> dict:
        """Build the Tier 2 extended agent card (A2A auth required).

        Returns detailed capability info for authenticated A2A agents:
        specific model families, capability parameters, availability
        status, and supported model list. Still does NOT expose raw
        VRAM numbers, queue depth, or GPU hardware details (those
        remain in Tier 3: /broker/status).

        Returns
        -------
        dict
            A2A AgentCard with model details and availability status.
        """
        # Determine availability based on circuit breaker state
        if self._circuit_breaker and self._circuit_breaker.state == "open":
            availability = "unavailable"
        elif self._circuit_breaker and self._circuit_breaker.state == "half_open":
            availability = "degraded"
        else:
            availability = "available"

        # Build supported models list from config
        supported_models: list[dict[str, Any]] = []
        for model_name, model_info in self._config.models.items():
            entry: dict[str, Any] = {
                "name": model_name,
                "vram_gb": model_info.vram_gb,
                "default_num_ctx": model_info.default_num_ctx,
                "tags": model_info.tags,
            }
            supported_models.append(entry)

        card: dict[str, Any] = {
            "name": "BASTION GPU Inference Broker",
            "description": "GPU inference broker with scheduling, batching, and model management",
            "url": self._config.server.external_url,
            "version": __import__("bastion").__version__,
            "serviceEndpoint": f"{self._config.server.external_url}/a2a",
            "protocolVersion": "0.1",
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
                "batch": True,
                "reservations": True,
            },
            "availability": availability,
            "supported_models": supported_models,
            "skills": [
                {
                    "id": "infer",
                    "name": "Single Prompt Inference",
                    "description": "Execute a single prompt through the scheduler",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": "Model name to use for inference",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Prompt text to process",
                            },
                            "system_prompt": {
                                "type": "string",
                                "description": "Optional system prompt",
                            },
                            "options": {
                                "type": "object",
                                "description": "Ollama options (temperature, etc.)",
                            },
                            "stream": {
                                "type": "boolean",
                                "description": "Enable SSE streaming",
                                "default": False,
                            },
                        },
                        "required": ["model", "prompt"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "response": {"type": "string"},
                            "model": {"type": "string"},
                            "eval_count": {"type": "integer"},
                            "total_duration": {"type": "integer"},
                        },
                    },
                },
                {
                    "id": "status",
                    "name": "Broker Status",
                    "description": "Get current queue depth, loaded models, and GPU health",
                    "inputSchema": {"type": "object", "properties": {}},
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "queue_depth": {"type": "integer"},
                            "queue_by_model": {"type": "object"},
                            "loaded_models": {"type": "array", "items": {"type": "string"}},
                            "current_model": {"type": "string"},
                        },
                    },
                },
                {
                    "id": "batch_infer",
                    "name": "Batch Inference",
                    "description": "Submit N prompts for same model with single model load",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string"},
                            "prompts": {"type": "array", "items": {"type": "string"}},
                            "system_prompt": {"type": "string"},
                            "options": {"type": "object"},
                        },
                        "required": ["model", "prompts"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "results": {"type": "array"},
                            "total": {"type": "integer"},
                            "succeeded": {"type": "integer"},
                            "failed": {"type": "integer"},
                        },
                    },
                },
                {
                    "id": "preload",
                    "name": "Preload Model",
                    "description": "Reserve model loading for upcoming workload",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string"},
                            "num_requests": {"type": "integer"},
                            "timeout_seconds": {"type": "number"},
                        },
                        "required": ["model"],
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "reservation_id": {"type": "string"},
                            "model": {"type": "string"},
                            "expires_at": {"type": "number"},
                        },
                    },
                },
            ],
        }

        # Add security scheme if tokens are configured
        if self._config.a2a.tokens:
            card["securitySchemes"] = {
                "BearerToken": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "A2A bearer token for task and extended card endpoints",
                }
            }
            card["security"] = [{"BearerToken": []}]

        return card

    # ── Helpers ─────────────────────────────────────────────────────────

    def _task_to_dict(self, record: A2ATaskRecord) -> dict:
        """Convert A2ATaskRecord to A2A Task dict format.

        Parameters
        ----------
        record : A2ATaskRecord
            Internal task record.

        Returns
        -------
        dict
            A2A Task object.
        """
        return {
            "id": record.task_id,
            "contextId": record.context_id,
            "status": {
                "state": record.state.value,
                "message": record.error if record.error else None,
            },
            "artifacts": record.output_artifacts,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def _status_update_event(self, record: A2ATaskRecord) -> dict:
        """Build a TaskStatusUpdateEvent for SSE.

        Parameters
        ----------
        record : A2ATaskRecord
            Task record.

        Returns
        -------
        dict
            SSE event dict.
        """
        event = {
            "taskId": record.task_id,
            "status": {
                "state": record.state.value,
                "message": record.error if record.error else None,
            },
            "timestamp": record.updated_at,
        }
        # Mark terminal states so generators can detect end-of-stream
        if record.state in (A2ATaskState.COMPLETED, A2ATaskState.FAILED, A2ATaskState.CANCELED):
            event["final"] = True
        return event

    async def _notify_subscribers(self, task_id: str, event: dict) -> None:
        """Push event to all SSE subscribers via the TaskStore.

        Delegates to TaskStore.notify_subscribers which handles bounded
        queues with drop-oldest strategy.
        """
        await self._store.notify_subscribers(task_id, event)
