"""OpenTelemetry instrumentation for BASTION.

Provides trace spans for the request pipeline:
  - a2a.task.submit (PRODUCER) -- at task creation
  - a2a.task.process (CONSUMER, LINKED to producer) -- at processing
  - bastion.scheduler.queue_wait -- time in queue
  - bastion.scheduler.model_swap -- model loading time
  - bastion.ollama.inference (CLIENT) -- actual Ollama call

GenAI semantic attributes follow OTel conventions:
  gen_ai.request.model, gen_ai.operation.name, gen_ai.provider.name,
  gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
  server.address, server.port

All functions are no-ops when:
  - opentelemetry packages are not installed (OTEL_AVAILABLE is False)
  - telemetry is disabled in configuration (TelemetryConfig.enabled is False)
  - init_telemetry() has not been called
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conditional OpenTelemetry import
# ---------------------------------------------------------------------------

try:
    from opentelemetry import trace
    from opentelemetry.propagate import extract, inject
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.trace import Link, SpanKind, StatusCode

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False

# Optional OTLP exporter (separate package)
_OTLP_AVAILABLE = False
if OTEL_AVAILABLE:
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        _OTLP_AVAILABLE = True
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_tracer: Any = None  # opentelemetry.trace.Tracer or None
_enabled: bool = False

# GenAI semantic convention attribute keys
_GENAI_REQUEST_MODEL = "gen_ai.request.model"
_GENAI_OPERATION_NAME = "gen_ai.operation.name"
_GENAI_PROVIDER_NAME = "gen_ai.provider.name"
_GENAI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_GENAI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_SERVER_ADDRESS = "server.address"
_SERVER_PORT = "server.port"


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def init_telemetry(config: Any) -> None:
    """Initialize the OpenTelemetry TracerProvider and exporter.

    Parameters
    ----------
    config : TelemetryConfig
        Telemetry configuration from BrokerConfig.telemetry.
        Must have: enabled (bool), exporter (str), endpoint (str),
        service_name (str).

    When OTEL_AVAILABLE is False or config.enabled is False, this
    function is a no-op and all subsequent instrumentation calls
    remain no-ops.
    """
    global _tracer, _enabled

    if not config.enabled:
        logger.debug("Telemetry disabled by configuration")
        _enabled = False
        _tracer = None
        return

    if not OTEL_AVAILABLE:
        logger.warning(
            "Telemetry enabled in config but opentelemetry packages not installed. "
            "Install with: pip install opentelemetry-api opentelemetry-sdk"
        )
        _enabled = False
        _tracer = None
        return

    # Build resource
    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.version": __import__("bastion").__version__,
        }
    )

    # Build provider
    provider = TracerProvider(resource=resource)

    # Configure exporter
    exporter_name = config.exporter.lower().strip()

    if exporter_name == "console":
        processor = SimpleSpanProcessor(ConsoleSpanExporter())
        provider.add_span_processor(processor)
        logger.info("Telemetry exporter: console")

    elif exporter_name == "otlp":
        if not _OTLP_AVAILABLE:
            logger.warning(
                "OTLP exporter requested but opentelemetry-exporter-otlp not installed. "
                "Install with: pip install opentelemetry-exporter-otlp"
            )
            _enabled = False
            _tracer = None
            return

        endpoint = config.endpoint or "http://localhost:4317"
        otlp_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        processor = BatchSpanProcessor(otlp_exporter)
        provider.add_span_processor(processor)
        logger.info("Telemetry exporter: OTLP -> %s", endpoint)

    elif exporter_name == "none":
        logger.info("Telemetry enabled with no exporter (spans recorded but not exported)")

    else:
        logger.warning("Unknown telemetry exporter %r, defaulting to none", exporter_name)

    # Set the global provider and get our tracer
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("bastion", __import__("bastion").__version__)
    _enabled = True
    logger.info("OpenTelemetry instrumentation initialized (exporter=%s)", exporter_name)


def get_tracer() -> Any:
    """Return the active tracer, or None if telemetry is not enabled.

    Returns
    -------
    opentelemetry.trace.Tracer or None
        The tracer instance, or None when telemetry is disabled / unavailable.
    """
    return _tracer


def is_enabled() -> bool:
    """Check if telemetry is active.

    Returns
    -------
    bool
        True if init_telemetry() was called successfully and telemetry is on.
    """
    return _enabled and _tracer is not None


# ---------------------------------------------------------------------------
# Context propagation helpers
# ---------------------------------------------------------------------------


def inject_trace_context() -> dict[str, str]:
    """Serialize current trace context into a carrier dict.

    Used at task submission time to capture traceparent/tracestate
    for later extraction at processing time.

    Returns
    -------
    dict
        Carrier with traceparent/tracestate headers, or empty dict
        if telemetry is not enabled.
    """
    if not is_enabled():
        return {}
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def extract_trace_context(carrier: dict[str, str]) -> Any:
    """Extract trace context from a carrier dict.

    Used at task processing time to recover the context from submission.

    Parameters
    ----------
    carrier : dict
        Dict containing traceparent/tracestate (from inject_trace_context).

    Returns
    -------
    opentelemetry.context.Context or None
        Extracted context, or None if telemetry is not enabled.
    """
    if not is_enabled():
        return None
    return extract(carrier)


# ---------------------------------------------------------------------------
# Span recording functions
# ---------------------------------------------------------------------------


def record_task_submit(
    task_id: str,
    skill_id: str,
    model: str = "",
) -> dict[str, str]:
    """Create a PRODUCER span for A2A task submission.

    Parameters
    ----------
    task_id : str
        Unique task identifier.
    skill_id : str
        Skill being invoked (e.g. "infer", "batch_infer").
    model : str
        Model name (if known at submission time).

    Returns
    -------
    dict
        Serialized trace context (traceparent/tracestate) for linking
        at processing time. Empty dict if telemetry is disabled.
    """
    if not is_enabled():
        return {}

    with _tracer.start_as_current_span(
        "a2a.task.submit",
        kind=SpanKind.PRODUCER,
        attributes={
            "a2a.task.id": task_id,
            "a2a.skill.id": skill_id,
            _GENAI_PROVIDER_NAME: "ollama",
        },
    ) as span:
        if model:
            span.set_attribute(_GENAI_REQUEST_MODEL, model)

        # Capture trace context for the consumer to link to
        return inject_trace_context()


def record_task_process(
    task_id: str,
    skill_id: str,
    model: str = "",
    trace_context: dict[str, str] | None = None,
) -> Any:
    """Create a CONSUMER span for A2A task processing, linked to the producer.

    Parameters
    ----------
    task_id : str
        Unique task identifier.
    skill_id : str
        Skill being processed.
    model : str
        Model name.
    trace_context : dict, optional
        Serialized trace context from record_task_submit() for linking.

    Returns
    -------
    opentelemetry.trace.Span or None
        The started span (caller should call span.end() when processing
        completes), or None if telemetry is disabled.
    """
    if not is_enabled():
        return None

    # Build link to the producer span
    links: list[Link] = []
    if trace_context:
        extracted_ctx = extract_trace_context(trace_context)
        if extracted_ctx is not None:
            linked_span_ctx = trace.get_current_span(extracted_ctx).get_span_context()
            if linked_span_ctx.is_valid:
                links.append(Link(linked_span_ctx))

    span = _tracer.start_span(
        "a2a.task.process",
        kind=SpanKind.CONSUMER,
        links=links,
        attributes={
            "a2a.task.id": task_id,
            "a2a.skill.id": skill_id,
            _GENAI_PROVIDER_NAME: "ollama",
        },
    )
    if model:
        span.set_attribute(_GENAI_REQUEST_MODEL, model)

    return span


@contextmanager
def record_queue_wait(
    request_id: str,
    model: str,
) -> Generator[None, None, None]:
    """Context manager span for time spent in the scheduler queue.

    Parameters
    ----------
    request_id : str
        Queued request ID.
    model : str
        Requested model name.

    Yields
    ------
    None
        The span is active during the context block.
    """
    if not is_enabled():
        yield
        return

    with _tracer.start_as_current_span(
        "bastion.scheduler.queue_wait",
        kind=SpanKind.INTERNAL,
        attributes={
            "bastion.request.id": request_id,
            _GENAI_REQUEST_MODEL: model,
        },
    ):
        yield


@contextmanager
def record_model_swap(
    from_model: str | None,
    to_model: str,
) -> Generator[None, None, None]:
    """Context manager span for model swap (load/unload) time.

    Parameters
    ----------
    from_model : str or None
        Model being unloaded (None if no previous model).
    to_model : str
        Model being loaded.

    Yields
    ------
    None
        The span is active during the context block.
    """
    if not is_enabled():
        yield
        return

    attrs: dict[str, Any] = {
        "bastion.swap.to_model": to_model,
    }
    if from_model:
        attrs["bastion.swap.from_model"] = from_model

    with _tracer.start_as_current_span(
        "bastion.scheduler.model_swap",
        kind=SpanKind.INTERNAL,
        attributes=attrs,
    ):
        yield


@contextmanager
def record_inference(
    model: str,
    operation: str = "generate",
    endpoint: str = "/api/generate",
) -> Generator[Any | None, None, None]:
    """Context manager span for an Ollama inference call (CLIENT span).

    Parameters
    ----------
    model : str
        Model name.
    operation : str
        GenAI operation: "chat", "generate", or "embed".
    endpoint : str
        Ollama endpoint path.

    Yields
    ------
    span or None
        The active span (so caller can set token counts after inference),
        or None if telemetry is disabled.
    """
    if not is_enabled():
        yield None
        return

    with _tracer.start_as_current_span(
        "bastion.ollama.inference",
        kind=SpanKind.CLIENT,
        attributes={
            _GENAI_REQUEST_MODEL: model,
            _GENAI_OPERATION_NAME: operation,
            _GENAI_PROVIDER_NAME: "ollama",
            _SERVER_ADDRESS: "localhost",
            _SERVER_PORT: 11435,
            "http.url": f"http://localhost:11435{endpoint}",
        },
    ) as span:
        yield span


def set_inference_tokens(
    span: Any,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Set token usage attributes on an inference span.

    Parameters
    ----------
    span : opentelemetry.trace.Span or None
        The span from record_inference(). No-op if None.
    input_tokens : int, optional
        Number of input (prompt) tokens.
    output_tokens : int, optional
        Number of output (eval) tokens.
    """
    if span is None or not is_enabled():
        return

    if input_tokens is not None:
        span.set_attribute(_GENAI_USAGE_INPUT_TOKENS, input_tokens)
    if output_tokens is not None:
        span.set_attribute(_GENAI_USAGE_OUTPUT_TOKENS, output_tokens)


def end_span(span: Any, error: str | None = None) -> None:
    """End a span, optionally recording an error.

    Parameters
    ----------
    span : opentelemetry.trace.Span or None
        Span to end. No-op if None.
    error : str, optional
        Error message to record on the span before ending.
    """
    if span is None or not is_enabled():
        return

    if error:
        span.set_status(StatusCode.ERROR, error)
        span.set_attribute("error.message", error)

    span.end()


def shutdown_telemetry() -> None:
    """Flush and shut down the tracer provider.

    Call this during application shutdown to ensure all spans are exported.
    """
    global _tracer, _enabled

    if not OTEL_AVAILABLE or not _enabled:
        return

    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        try:
            provider.shutdown()
        except Exception as exc:
            logger.warning("Error shutting down telemetry: %s", exc)

    _tracer = None
    _enabled = False
    logger.info("OpenTelemetry instrumentation shut down")
