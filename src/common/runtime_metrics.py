"""OpenTelemetry instruments shared by the runtime resilience wrappers.

These are implementation details of `common`, not part of the stable public surface. Services
get the metrics by using the wrappers they already use; they never import this module.

Instruments are built lazily from ``get_meter("groovemap.runtime")`` on first use and cached
until the installed provider changes, so a process that never calls ``setup_telemetry`` pays
only for one no-op instrument per metric. Every recording helper swallows its own errors:
telemetry must never turn a working database call into a failure.

Metric names, units, and attribute keys follow the GrooveMap OpenTelemetry conventions. All
attribute values are closed, low-cardinality sets — never ids, queries, hosts, or free text.
"""

import logging
from threading import RLock
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

from common.telemetry import get_meter, provider_generation


if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from opentelemetry.metrics import CallbackOptions, Observation

    from common.db_resilience import CircuitBreaker


logger = logging.getLogger(__name__)

INSTRUMENTATION_SCOPE = "groovemap.runtime"

DB_OPERATION_DURATION = "db.client.operation.duration"
MESSAGING_CONSUMED_MESSAGES = "messaging.client.consumed.messages"
MESSAGING_OPERATION_DURATION = "messaging.client.operation.duration"
MESSAGING_SENT_MESSAGES = "messaging.client.sent.messages"
PIPELINE_CIRCUIT_BREAKER_STATE = "groovemap.pipeline.circuit_breaker.state"
PIPELINE_RECONNECTS = "groovemap.pipeline.reconnects"

MESSAGING_SYSTEM = "rabbitmq"

# The circuit-breaker gauge reports one value per live breaker. A weak set keeps the callback
# from pinning a breaker (and the pool behind it) alive after its owner is gone.
_breakers: WeakSet[CircuitBreaker] = WeakSet()

_lock = RLock()
_instruments: dict[str, Any] = {}
_instrument_generation = -1

_CIRCUIT_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def _observe_circuit_breakers(_options: CallbackOptions) -> Iterator[Observation]:
    """Yield the current state of every live circuit breaker."""
    from opentelemetry.metrics import Observation  # noqa: PLC0415

    for breaker in list(_breakers):
        try:
            value = _CIRCUIT_STATE_VALUES[breaker.state.value]
        except AttributeError, KeyError:  # pragma: no cover - defensive
            continue
        yield Observation(value, {"system": breaker.system})


def _build_instruments() -> dict[str, Any]:
    """Create one instrument per runtime metric from the current provider."""
    meter = get_meter(INSTRUMENTATION_SCOPE)
    instruments: dict[str, Any] = {
        DB_OPERATION_DURATION: meter.create_histogram(
            DB_OPERATION_DURATION,
            unit="s",
            description="Duration of a database client operation.",
        ),
        MESSAGING_CONSUMED_MESSAGES: meter.create_counter(
            MESSAGING_CONSUMED_MESSAGES,
            description="Messages consumed from the broker.",
        ),
        MESSAGING_OPERATION_DURATION: meter.create_histogram(
            MESSAGING_OPERATION_DURATION,
            unit="s",
            description="Duration of a messaging client operation.",
        ),
        MESSAGING_SENT_MESSAGES: meter.create_counter(
            MESSAGING_SENT_MESSAGES,
            description="Messages published to the broker.",
        ),
        PIPELINE_RECONNECTS: meter.create_counter(
            PIPELINE_RECONNECTS,
            description="Reconnections performed by a resilient connection wrapper.",
        ),
    }
    instruments[PIPELINE_CIRCUIT_BREAKER_STATE] = meter.create_observable_gauge(
        PIPELINE_CIRCUIT_BREAKER_STATE,
        callbacks=[_observe_circuit_breakers],
        description="Circuit breaker state: 0 closed, 1 half-open, 2 open.",
    )
    return instruments


def _instrument(name: str) -> Any:
    """Return one cached instrument, rebuilding the cache when the provider changed."""
    global _instrument_generation

    generation = provider_generation()
    with _lock:
        if _instrument_generation != generation or not _instruments:
            _instruments.clear()
            _instruments.update(_build_instruments())
            _instrument_generation = generation
        return _instruments[name]


def reset_instruments() -> None:
    """Drop the instrument cache. Test seam; production relies on the generation check."""
    global _instrument_generation

    with _lock:
        _instruments.clear()
        _instrument_generation = -1


def register_circuit_breaker(breaker: CircuitBreaker) -> None:
    """Include a breaker in the circuit-breaker state gauge for as long as it lives."""
    try:
        _breakers.add(breaker)
        _instrument(PIPELINE_CIRCUIT_BREAKER_STATE)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not register a circuit breaker for telemetry", exc_info=True)


def record_db_operation(system: str, operation: str, duration_s: float, error_type: str | None = None) -> None:
    """Record one database client operation's duration."""
    attributes: dict[str, str] = {"db.system.name": system, "db.operation.name": operation}
    if error_type is not None:
        attributes["error.type"] = error_type
    try:
        _instrument(DB_OPERATION_DURATION).record(duration_s, attributes)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record %s", DB_OPERATION_DURATION, exc_info=True)


def record_reconnect(system: str) -> None:
    """Count one reconnection of a resilient connection wrapper."""
    try:
        _instrument(PIPELINE_RECONNECTS).add(1, {"system": system})
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record %s", PIPELINE_RECONNECTS, exc_info=True)


def record_consumed_message(destination: str, duration_s: float, error_type: str | None = None) -> None:
    """Count one consumed message and record how long handling it took."""
    attributes: dict[str, str] = {
        "messaging.system": MESSAGING_SYSTEM,
        "messaging.destination.name": destination,
        "messaging.operation.name": "process",
    }
    if error_type is not None:
        attributes["error.type"] = error_type
    try:
        _instrument(MESSAGING_CONSUMED_MESSAGES).add(1, attributes)
        _instrument(MESSAGING_OPERATION_DURATION).record(duration_s, attributes)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record consumed-message metrics", exc_info=True)


def record_sent_message(destination: str, duration_s: float, error_type: str | None = None) -> None:
    """Count one published message and record how long publishing took."""
    attributes: dict[str, str] = {
        "messaging.system": MESSAGING_SYSTEM,
        "messaging.destination.name": destination,
        "messaging.operation.name": "send",
    }
    if error_type is not None:
        attributes["error.type"] = error_type
    try:
        _instrument(MESSAGING_SENT_MESSAGES).add(1, attributes)
        _instrument(MESSAGING_OPERATION_DURATION).record(duration_s, attributes)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Could not record sent-message metrics", exc_info=True)


def error_type_of(exc: BaseException) -> str:
    """Return the closed-set error.type value for an exception: its class name."""
    return type(exc).__name__
