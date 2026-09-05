"""Span helpers shared by GrooveMap services and the resilience wrappers.

:mod:`common.telemetry` owns the provider lifecycle; this module is the small surface everything
else uses to open a span and to carry a trace across a process boundary. It follows the same
rules as the metrics side: importable with no ``opentelemetry`` package installed, a measured
no-op before ``setup_telemetry`` or without the ``otel`` extra, and never raising into
application code — a broken trace context must not fail the message it rode in on.

Span names are low-cardinality by construction. The helpers here take a closed-set operation and
a destination or system, never an id, a query, a file name, or a routing key that carries one.
"""

import logging
from collections.abc import Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from common import telemetry


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator, MutableMapping, Sequence

    from opentelemetry.context import Context
    from opentelemetry.trace import Tracer


logger = logging.getLogger(__name__)

# The scope every span this library opens is reported under; the same one its metrics use.
INSTRUMENTATION_SCOPE = telemetry.INSTRUMENTATION_SCOPE

# W3C TraceContext carries exactly these two headers; both are printable ASCII by spec.
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"

# The one broker GrooveMap runs, matching the messaging metric attributes.
MESSAGING_SYSTEM = "rabbitmq"

# A batch flush links the message spans it covers. Beyond this the links stop telling an
# operator anything and start costing the collector real money.
MAX_FLUSH_LINKS = 64

# How many attempts a retried operation took. An integer, never a description of what failed.
RETRY_COUNT_ATTRIBUTE = "groovemap.pipeline.retry.count"


def get_tracer(name: str, version: str | None = None) -> Tracer:
    """Return a tracer from the installed provider, or a no-op tracer before setup."""
    return telemetry.tracer_provider().get_tracer(name, version)


def _as_text(value: object) -> str | None:
    """Return a header value as text, or None when it cannot be one.

    AMQP header values arrive as ``bytes`` from some brokers and clients and as ``str`` from
    others. The W3C propagator only understands text, and its default carrier getter treats
    ``bytes`` as an iterable of integers rather than decoding it, so every carrier is normalized
    here before it reaches the propagator.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        try:
            return bytes(value).decode("ascii")
        except UnicodeDecodeError:
            return None
    return None


def inject_headers(headers: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Write the current trace context into ``headers`` and return it.

    The values written are always ``str``; existing entries of any type are left alone, so an
    AMQP header dict that already carries ``bytes`` values survives unchanged. A no-op without
    the ``otel`` extra, before ``setup_telemetry``, and outside any span.
    """
    if telemetry.trace is None:
        return headers
    try:
        from opentelemetry.propagate import inject  # noqa: PLC0415

        carrier: dict[str, str] = {}
        inject(carrier)
        headers.update(carrier)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not inject the trace context into outbound headers", exc_info=True)
    return headers


def extract_context(headers: Mapping[str, Any]) -> Context | None:
    """Return the trace context carried by ``headers``, or None when there is none.

    Accepts ``str`` and ``bytes`` header values, so a pika or aio-pika message dict can be
    passed straight through. Returns None without the ``otel`` extra, when the headers carry no
    context, and when the context they carry is malformed: an unreadable ``traceparent`` starts
    a new trace rather than failing the message that delivered it.
    """
    if telemetry.trace is None or not isinstance(headers, Mapping):
        # A broker client hands back whatever it was given; anything that is not a mapping
        # carries no context by definition and must not be iterated.
        return None
    try:
        from opentelemetry.propagate import extract  # noqa: PLC0415

        carrier = {key: text for key, value in headers.items() if (text := _as_text(value)) is not None}
        if not carrier:
            return None
        # A Context is a mapping, so an empty one is exactly "no propagator recognized
        # anything here" — which keeps this generic over whichever propagator is installed.
        return extract(carrier) or None
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not extract a trace context from inbound headers", exc_info=True)
        return None


def _mark_error(span: Any, exc: BaseException) -> None:
    """Fail a span with `error.type` only — never a message, never an event with a payload."""
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

        span.set_attribute("error.type", type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR))
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not mark a span as failed", exc_info=True)


@contextmanager
def _span(name: str, kind: Any, attributes: dict[str, Any], context: Any = None, links: Any = None) -> Iterator[Any]:
    """Open one span, failing it with `error.type` on the way out. Yields None when tracing is off.

    Exception recording and automatic status are both switched off: the conventions allow a
    status and an `error.type`, not a stack trace attached as a span event.
    """
    try:
        manager = get_tracer(INSTRUMENTATION_SCOPE).start_as_current_span(
            name,
            context=context,
            kind=kind,
            attributes=attributes,
            links=links,
            record_exception=False,
            set_status_on_exception=False,
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not start the %r span", name, exc_info=True)
        yield None
        return

    with manager as span:
        try:
            yield span
        except BaseException as exc:
            _mark_error(span, exc)
            raise


def _span_kind(name: str) -> Any:
    """Return a SpanKind member, or None when the API is not installed."""
    if telemetry.trace is None:
        return None
    return getattr(telemetry.trace.SpanKind, name)


@contextmanager
def db_span(system: str, operation: str) -> Iterator[Any]:
    """Open the CLIENT span for one database operation: `{operation} {system}`.

    Both arguments are closed-set values — `postgresql`, `neo4j`, `session`, `execute` — so the
    span name stays low-cardinality. No statement, parameter, or identifier is ever attached.
    """
    with _span(
        f"{operation} {system}",
        _span_kind("CLIENT"),
        {"db.system.name": system, "db.operation.name": operation},
    ) as span:
        yield span


@contextmanager
def publish_span(destination: str) -> Iterator[Any]:
    """Open the PRODUCER span for one publish: `publish {destination}`.

    Inject the current context into the outbound headers from inside this block, so the
    consumer's span becomes a child of this one rather than of whatever ran before it.
    """
    with _span(
        f"publish {destination}",
        _span_kind("PRODUCER"),
        {"messaging.system": MESSAGING_SYSTEM, "messaging.destination.name": destination, "messaging.operation.name": "send"},
    ) as span:
        yield span


@contextmanager
def consume_span(destination: str, headers: Mapping[str, Any] | None = None) -> Iterator[Any]:
    """Open the CONSUMER span for one message: `process {destination}`.

    The span is a child of the context carried in ``headers``, which is what puts an
    extractor's publish and a consumer's processing in one trace. Headers without a readable
    context simply start a new trace.
    """
    with _span(
        f"process {destination}",
        _span_kind("CONSUMER"),
        {"messaging.system": MESSAGING_SYSTEM, "messaging.destination.name": destination, "messaging.operation.name": "process"},
        context=extract_context(headers) if headers else None,
    ) as span:
        yield span


def set_retry_count(span: Any, retries: int) -> None:
    """Record how many attempts a retried operation took, as an integer.

    One logical operation is one span: retrying inside a wrapper must not produce a span per
    attempt, because a dashboard counting spans would then report a failing consumer as a busy
    one. Nothing is recorded for an operation that succeeded first time.
    """
    if span is None or retries <= 0:
        return
    try:
        span.set_attribute(RETRY_COUNT_ATTRIBUTE, int(retries))
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record the retry count on a span", exc_info=True)


def _links_for(contexts: Sequence[Any]) -> list[Any] | None:
    """Turn span contexts (or ready-made links) into at most MAX_FLUSH_LINKS links."""
    if telemetry.trace is None or not contexts:
        return None
    try:
        from opentelemetry.trace import Link  # noqa: PLC0415

        return [item if isinstance(item, Link) else Link(item) for item in list(contexts)[:MAX_FLUSH_LINKS]]
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not build span links for a flush span", exc_info=True)
        return None


@contextmanager
def flush_span(store: str, entity: str, links: Sequence[Any] | None = None) -> Iterator[Any]:
    """Open the INTERNAL span for a batch flush: `flush {store} {entity}`.

    ``links`` are the span contexts of the messages in the batch — `span.get_span_context()`
    from each `consume_span`, or ready-made `Link` objects. At most 64 are attached, because a
    batch of ten thousand rows would otherwise carry ten thousand links into the collector.
    """
    with _span(
        f"flush {store} {entity}",
        _span_kind("INTERNAL"),
        {"db.system.name": store, "groovemap.entity": entity},
        links=_links_for(links or ()),
    ) as span:
        yield span
