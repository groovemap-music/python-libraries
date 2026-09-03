"""Metric contracts for the instrumented resilience wrappers.

Every assertion here is about the shape the collector and the dashboards depend on: instrument
name, unit, and the closed attribute set. Values are checked only where the value carries
meaning (the circuit-breaker state encoding).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from common import runtime_metrics, telemetry
from common.db_resilience import CircuitBreaker, CircuitBreakerConfig, CircuitState


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.metrics.export import Metric


class Collector:
    """An in-memory provider plus helpers for reading what the wrappers recorded."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = SdkMeterProvider(metric_readers=[self.reader])

    def metrics(self) -> dict[str, Metric]:
        """Collect once and return every recorded metric by name."""
        data = self.reader.get_metrics_data()
        if data is None:
            return {}
        return {
            metric.name: metric
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        }

    def points(self, name: str) -> list[Any]:
        """Return the data points recorded for one metric name."""
        metric = self.metrics().get(name)
        return [] if metric is None else list(metric.data.data_points)

    def attributes(self, name: str) -> list[dict[str, Any]]:
        """Return the attribute dicts recorded for one metric name."""
        return [dict(point.attributes) for point in self.points(name)]

    def value_for_system(self, name: str, system: str) -> Any:
        """Return the single value recorded for one `system` attribute value.

        The circuit-breaker gauge observes every live breaker in the process, including ones
        other test modules created, so a point has to be selected by attribute, never by index.
        """
        matching = [point.value for point in self.points(name) if dict(point.attributes).get("system") == system]
        assert len(matching) == 1, f"expected exactly one {name} point for system={system!r}, got {matching}"
        return matching[0]


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    """Install an in-memory provider and make the wrappers build instruments against it."""
    active = Collector()
    monkeypatch.setattr(telemetry, "_provider", active.provider)
    monkeypatch.setattr(telemetry, "_generation", telemetry.provider_generation() + 1)
    runtime_metrics.reset_instruments()
    yield active
    monkeypatch.setattr(telemetry, "_provider", None)
    runtime_metrics.reset_instruments()


class FakeMessage:
    """Minimal stand-in for an aio-pika incoming message."""

    def __init__(self, consumer_tag: str = "discogs-releases") -> None:
        self.consumer_tag = consumer_tag
        self.routing_key = "release.12345"
        self.acked = False
        self.nacked = False

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = True) -> None:  # noqa: ARG002
        self.nacked = True


def test_db_operation_duration_is_a_seconds_histogram_with_the_conventional_attributes(collector: Collector) -> None:
    runtime_metrics.record_db_operation("postgresql", "session", 0.25)

    metric = collector.metrics()[runtime_metrics.DB_OPERATION_DURATION]
    assert metric.unit == "s"
    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [{"db.system.name": "postgresql", "db.operation.name": "session"}]


def test_db_operation_duration_carries_error_type_only_on_failure(collector: Collector) -> None:
    runtime_metrics.record_db_operation("neo4j", "session", 0.1, "ServiceUnavailable")

    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [
        {"db.system.name": "neo4j", "db.operation.name": "session", "error.type": "ServiceUnavailable"}
    ]


def test_reconnects_counter_records_the_system(collector: Collector) -> None:
    runtime_metrics.record_reconnect("rabbitmq")
    runtime_metrics.record_reconnect("rabbitmq")

    points = collector.points(runtime_metrics.PIPELINE_RECONNECTS)
    assert [dict(point.attributes) for point in points] == [{"system": "rabbitmq"}]
    assert points[0].value == 2


def test_circuit_breaker_gauge_reports_the_documented_state_encoding(collector: Collector) -> None:
    gauge = runtime_metrics.PIPELINE_CIRCUIT_BREAKER_STATE
    breaker = CircuitBreaker(CircuitBreakerConfig(name="StateProbe", system="state-probe"))

    assert collector.value_for_system(gauge, "state-probe") == 0

    breaker.state = CircuitState.HALF_OPEN
    assert collector.value_for_system(gauge, "state-probe") == 1

    breaker.state = CircuitState.OPEN
    assert collector.value_for_system(gauge, "state-probe") == 2


def test_circuit_breaker_system_falls_back_to_the_lowercased_name(collector: Collector) -> None:
    # Bound to a local: the gauge holds breakers weakly, so an unreferenced one is not observed.
    breaker = CircuitBreaker(CircuitBreakerConfig(name="FallbackProbe"))

    assert breaker.system == "fallbackprobe"
    assert collector.value_for_system(runtime_metrics.PIPELINE_CIRCUIT_BREAKER_STATE, "fallbackprobe") == 0


def test_the_gauge_stops_observing_a_breaker_once_its_owner_is_gone(collector: Collector) -> None:
    import gc

    gauge = runtime_metrics.PIPELINE_CIRCUIT_BREAKER_STATE
    breaker = CircuitBreaker(CircuitBreakerConfig(name="LifetimeProbe"))
    assert collector.value_for_system(gauge, "lifetimeprobe") == 0

    del breaker
    gc.collect()

    assert {"system": "lifetimeprobe"} not in collector.attributes(gauge)


def test_messaging_metrics_use_the_conventional_attribute_set(collector: Collector) -> None:
    runtime_metrics.record_consumed_message("discogs-releases", 0.5)

    expected = {
        "messaging.system": "rabbitmq",
        "messaging.destination.name": "discogs-releases",
        "messaging.operation.name": "process",
    }
    assert collector.attributes(runtime_metrics.MESSAGING_CONSUMED_MESSAGES) == [expected]
    assert collector.attributes(runtime_metrics.MESSAGING_OPERATION_DURATION) == [expected]
    assert collector.metrics()[runtime_metrics.MESSAGING_OPERATION_DURATION].unit == "s"


def test_sent_messages_use_the_send_operation_name(collector: Collector) -> None:
    runtime_metrics.record_sent_message("graphinator", 0.01)

    assert collector.attributes(runtime_metrics.MESSAGING_SENT_MESSAGES) == [
        {"messaging.system": "rabbitmq", "messaging.destination.name": "graphinator", "messaging.operation.name": "send"}
    ]


def test_process_message_with_retry_counts_a_handled_message(collector: Collector) -> None:
    from common.rabbitmq_resilient import process_message_with_retry

    message = FakeMessage()
    handled: list[Any] = []

    async def handler(received: Any) -> None:
        handled.append(received)

    asyncio.run(process_message_with_retry(message, handler))

    assert handled == [message]
    assert message.acked
    assert collector.attributes(runtime_metrics.MESSAGING_CONSUMED_MESSAGES) == [
        {"messaging.system": "rabbitmq", "messaging.destination.name": "discogs-releases", "messaging.operation.name": "process"}
    ]


def test_process_message_with_retry_records_error_type_when_the_handler_never_succeeds(collector: Collector) -> None:
    from common.db_resilience import ExponentialBackoff
    from common.rabbitmq_resilient import process_message_with_retry

    message = FakeMessage()

    async def handler(_received: Any) -> None:
        raise ValueError("bad payload")

    with pytest.raises(ValueError, match="bad payload"):
        asyncio.run(
            process_message_with_retry(
                message,
                handler,
                max_retries=1,
                backoff=ExponentialBackoff(initial_delay=0.0, max_delay=0.0, jitter=False),
            )
        )

    assert message.nacked
    assert collector.attributes(runtime_metrics.MESSAGING_CONSUMED_MESSAGES) == [
        {
            "messaging.system": "rabbitmq",
            "messaging.destination.name": "discogs-releases",
            "messaging.operation.name": "process",
            "error.type": "ValueError",
        }
    ]


def test_destination_name_never_falls_back_to_a_high_cardinality_routing_key() -> None:
    from common.rabbitmq_resilient import _destination_name

    class Untagged:
        routing_key = ""
        exchange = ""

    assert _destination_name(Untagged()) == "unknown"


def test_sync_postgres_pool_times_the_whole_connection_checkout(collector: Collector) -> None:
    from common.postgres_resilient import ResilientPostgreSQLPool

    pool = object.__new__(ResilientPostgreSQLPool)

    @contextmanager_for(pool)
    def _pooled_connection() -> Iterator[str]:
        yield "connection"

    with ResilientPostgreSQLPool.connection(pool) as conn:
        assert conn == "connection"

    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [{"db.system.name": "postgresql", "db.operation.name": "session"}]


def test_sync_postgres_pool_records_the_error_type_when_the_body_raises(collector: Collector) -> None:
    from common.postgres_resilient import ResilientPostgreSQLPool

    pool = object.__new__(ResilientPostgreSQLPool)

    @contextmanager_for(pool)
    def _pooled_connection() -> Iterator[str]:
        yield "connection"

    with pytest.raises(RuntimeError, match="query blew up"), ResilientPostgreSQLPool.connection(pool):
        raise RuntimeError("query blew up")

    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [
        {"db.system.name": "postgresql", "db.operation.name": "session", "error.type": "RuntimeError"}
    ]


def test_async_postgres_pool_times_the_whole_connection_checkout(collector: Collector) -> None:
    from contextlib import asynccontextmanager

    from common.postgres_resilient import AsyncPostgreSQLPool

    pool = object.__new__(AsyncPostgreSQLPool)

    @asynccontextmanager
    async def _pooled_connection() -> Any:
        yield "connection"

    pool._pooled_connection = _pooled_connection  # type: ignore[method-assign]

    async def exercise() -> None:
        async with AsyncPostgreSQLPool.connection(pool) as conn:
            assert conn == "connection"

    asyncio.run(exercise())

    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [{"db.system.name": "postgresql", "db.operation.name": "session"}]


def test_sync_neo4j_session_is_timed_and_labelled(collector: Collector) -> None:
    from common.neo4j_resilient import ResilientNeo4jDriver

    driver = object.__new__(ResilientNeo4jDriver)
    driver.get_connection = lambda: _StubDriver()  # type: ignore[method-assign]

    assert ResilientNeo4jDriver.session(driver) == "session"
    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [{"db.system.name": "neo4j", "db.operation.name": "session"}]


def test_async_neo4j_session_is_timed_and_labelled(collector: Collector) -> None:
    from common.neo4j_resilient import AsyncResilientNeo4jDriver

    driver = object.__new__(AsyncResilientNeo4jDriver)

    async def get_connection() -> Any:
        return _StubAsyncDriver()

    driver.get_connection = get_connection  # type: ignore[method-assign]

    async def exercise() -> None:
        async with AsyncResilientNeo4jDriver.session(driver) as session:
            assert session == "session"

    asyncio.run(exercise())

    assert collector.attributes(runtime_metrics.DB_OPERATION_DURATION) == [{"db.system.name": "neo4j", "db.operation.name": "session"}]


def test_execute_sql_records_a_postgres_execute_without_leaking_the_query(collector: Collector) -> None:
    from common.query_debug import execute_sql

    class Cursor:
        async def execute(self, query: Any, params: Any = None) -> None:
            self.seen = (query, params)

    asyncio.run(execute_sql(Cursor(), "SELECT secret FROM artists WHERE id = %s", (42,)))

    attributes = collector.attributes(runtime_metrics.DB_OPERATION_DURATION)
    assert attributes == [{"db.system.name": "postgresql", "db.operation.name": "execute"}]
    assert all("SELECT" not in str(value) for attribute in attributes for value in attribute.values())


def test_instruments_are_rebuilt_when_a_provider_is_installed_after_first_use() -> None:
    """A wrapper touched before setup_telemetry must not stay bound to the no-op provider."""
    runtime_metrics.reset_instruments()
    runtime_metrics.record_reconnect("postgresql")

    later = Collector()
    original_provider = telemetry._provider
    original_generation = telemetry._generation
    try:
        telemetry._provider = later.provider
        telemetry._generation = original_generation + 1
        runtime_metrics.record_reconnect("postgresql")
        assert later.attributes(runtime_metrics.PIPELINE_RECONNECTS) == [{"system": "postgresql"}]
    finally:
        telemetry._provider = original_provider
        telemetry._generation = original_generation
        runtime_metrics.reset_instruments()


def contextmanager_for(target: Any) -> Any:
    """Bind a zero-argument context-manager factory onto an instance as _pooled_connection."""
    from contextlib import contextmanager

    def decorate(func: Any) -> Any:
        wrapped = contextmanager(func)
        target._pooled_connection = wrapped
        return wrapped

    return decorate


class _StubDriver:
    def session(self, **_kwargs: Any) -> str:
        return "session"


class _StubAsyncSession:
    async def __aenter__(self) -> str:
        return "session"

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _StubAsyncDriver:
    def session(self, **_kwargs: Any) -> _StubAsyncSession:
        return _StubAsyncSession()
