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
from typing import TYPE_CHECKING, Any

from common import telemetry


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, MutableMapping

    from opentelemetry.context import Context
    from opentelemetry.trace import Tracer


logger = logging.getLogger(__name__)

# The scope every span this library opens is reported under; the same one its metrics use.
INSTRUMENTATION_SCOPE = telemetry.INSTRUMENTATION_SCOPE

# W3C TraceContext carries exactly these two headers; both are printable ASCII by spec.
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"


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
    if telemetry.trace is None:
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
