"""Span contracts for the instrumented resilience wrappers.

Ten services get these spans by using the wrappers they already use, so the assertions here are
about the shape a trace viewer and the collector's span-metrics connector depend on: span name,
kind, the closed attribute set, and — for the broker — that a producer and a consumer end up in
one trace.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracerProvider, SpanKind, StatusCode
from pika import BasicProperties

from common import telemetry, tracing
from common.db_resilience import ExponentialBackoff
from common.query_debug import execute_sql
from common.rabbitmq_resilient import AsyncResilientRabbitMQ, ResilientRabbitMQConnection, process_message_with_retry


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace import ReadableSpan


SOURCE = Path(__file__).resolve().parents[1] / "src/common"
AMQP_URL = "amqp://guest:guest@rabbitmq:5672/"
NO_BACKOFF = ExponentialBackoff(initial_delay=0.0, max_delay=0.0)


class SpanCollector:
    """An in-memory tracer provider whose finished spans can be read back by name."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = SdkTracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def spans(self) -> tuple[ReadableSpan, ...]:
        return self.exporter.get_finished_spans()

    def named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.spans() if span.name == name]

    def only(self, name: str) -> ReadableSpan:
        matching = self.named(name)
        assert len(matching) == 1, f"expected exactly one {name!r} span, saw {[span.name for span in self.spans()]}"
        return matching[0]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[SpanCollector]:
    """Make the wrappers record into an in-memory provider instead of the global one."""
    collector = SpanCollector()
    monkeypatch.setattr(telemetry, "_tracer_provider", collector.provider)
    assert telemetry.tracer_provider() is collector.provider
    yield collector
    monkeypatch.setattr(telemetry, "_tracer_provider", None)


@pytest.fixture
def tracing_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install the no-op provider, the state a service without an endpoint runs in."""
    monkeypatch.setattr(telemetry, "_tracer_provider", NoOpTracerProvider())
    yield
    monkeypatch.setattr(telemetry, "_tracer_provider", None)


class FakeMessage:
    """Minimal stand-in for an aio-pika incoming message."""

    def __init__(self, headers: dict[str, Any] | None = None, consumer_tag: str = "discogs-releases") -> None:
        self.consumer_tag = consumer_tag
        self.routing_key = "release.12345"
        self.headers = headers if headers is not None else {}
        self.acked = False
        self.nacked = False

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = True) -> None:  # noqa: ARG002
        self.nacked = True


class FakeChannel:
    """Records what a pika publish would have put on the wire."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def basic_publish(self, exchange: str, routing_key: str, body: bytes, properties: Any, mandatory: bool) -> None:
        self.published.append({"exchange": exchange, "routing_key": routing_key, "body": body, "properties": properties, "mandatory": mandatory})


class FakeExchange:
    """Records what an aio-pika publish would have put on the wire."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.published: list[Any] = []

    async def publish(self, message: Any, routing_key: str) -> str:
        self.published.append((message, routing_key))
        return "ok"


class FakeAmqpMessage:
    """Minimal stand-in for an outbound aio-pika message."""

    def __init__(self, headers: dict[str, Any] | None = None) -> None:
        self.headers = headers if headers is not None else {}
        self.body = b"{}"


# --- database spans ---------------------------------------------------------


def test_every_database_metric_site_also_opens_a_span() -> None:
    """The two must stay paired: a span-less metric site is invisible in a trace viewer."""
    for module in ("neo4j_resilient.py", "postgres_resilient.py", "query_debug.py"):
        source = (SOURCE / module).read_text()
        assert source.count("record_db_operation(") == source.count("tracing.db_span("), module


def test_execute_sql_opens_a_client_span_without_the_statement(spans: SpanCollector) -> None:
    cursor = AsyncMock()

    asyncio.run(execute_sql(cursor, "SELECT * FROM releases WHERE id = %s", ("12345",)))

    span = spans.only("execute postgresql")
    assert span.kind is SpanKind.CLIENT
    assert dict(span.attributes or {}) == {"db.system.name": "postgresql", "db.operation.name": "execute"}
    assert span.status.status_code is not StatusCode.ERROR


def test_a_failing_statement_fails_the_span_with_error_type_only(spans: SpanCollector) -> None:
    cursor = AsyncMock()
    cursor.execute.side_effect = RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        asyncio.run(execute_sql(cursor, "SELECT 1"))

    span = spans.only("execute postgresql")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "RuntimeError"
    assert not span.events, "a stack trace is a payload; the conventions allow error.type only"
    assert "SELECT" not in str(dict(span.attributes or {}))


def test_the_neo4j_session_opens_a_client_span(monkeypatch: pytest.MonkeyPatch, spans: SpanCollector) -> None:
    from common.neo4j_resilient import ResilientNeo4jDriver

    with patch("common.neo4j_resilient.GraphDatabase"):
        driver = ResilientNeo4jDriver("neo4j://neo4j:7687", ("neo4j", "password"))
        # The span covers acquisition, exactly the region the duration metric times.
        monkeypatch.setattr(driver, "get_connection", Mock(return_value=Mock()))
        driver.session()

    span = spans.only("session neo4j")
    assert span.kind is SpanKind.CLIENT
    assert dict(span.attributes or {}) == {"db.system.name": "neo4j", "db.operation.name": "session"}


# --- broker spans -----------------------------------------------------------


def test_publishing_through_the_pika_wrapper_propagates_the_context(monkeypatch: pytest.MonkeyPatch, spans: SpanCollector) -> None:
    channel = FakeChannel()
    connection = ResilientRabbitMQConnection(AMQP_URL)
    monkeypatch.setattr(connection, "channel", lambda: channel)
    reused = BasicProperties(headers={"x-groovemap-entity": "release"})

    connection.publish("discogs-releases", b"{}", properties=reused)

    published = channel.published[0]
    headers = published["properties"].headers
    assert headers["x-groovemap-entity"] == "release"
    assert tracing.TRACEPARENT_HEADER in headers
    assert reused.headers == {"x-groovemap-entity": "release"}, "the caller's properties must not be mutated"

    span = spans.only("publish discogs-releases")
    assert span.kind is SpanKind.PRODUCER
    assert dict(span.attributes or {}) == {
        "messaging.system": "rabbitmq",
        "messaging.destination.name": "discogs-releases",
        "messaging.operation.name": "send",
    }
    assert span.context is not None
    assert headers[tracing.TRACEPARENT_HEADER].split("-")[1] == format(span.context.trace_id, "032x")


def test_a_named_exchange_is_the_destination(monkeypatch: pytest.MonkeyPatch, spans: SpanCollector) -> None:
    connection = ResilientRabbitMQConnection(AMQP_URL)
    monkeypatch.setattr(connection, "channel", lambda: FakeChannel())

    connection.publish("release.12345", b"{}", exchange="groovemap")

    assert spans.only("publish groovemap").kind is SpanKind.PRODUCER


def test_a_failing_publish_fails_the_span(monkeypatch: pytest.MonkeyPatch, spans: SpanCollector) -> None:
    channel = FakeChannel()
    channel.basic_publish = Mock(side_effect=RuntimeError("channel closed"))  # type: ignore[method-assign]
    connection = ResilientRabbitMQConnection(AMQP_URL)
    monkeypatch.setattr(connection, "channel", lambda: channel)

    with pytest.raises(RuntimeError):
        connection.publish("discogs-releases", b"{}")

    span = spans.only("publish discogs-releases")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "RuntimeError"


def test_publishing_through_the_aio_pika_wrapper_propagates_the_context(spans: SpanCollector) -> None:
    exchange = FakeExchange(name="groovemap")
    rabbit = AsyncResilientRabbitMQ(AMQP_URL)
    message = FakeAmqpMessage()

    result = asyncio.run(rabbit.publish(message, "release.12345", exchange=exchange))

    assert result == "ok"
    assert tracing.TRACEPARENT_HEADER in message.headers
    span = spans.only("publish groovemap")
    assert span.kind is SpanKind.PRODUCER
    assert span.context is not None
    assert message.headers[tracing.TRACEPARENT_HEADER].split("-")[1] == format(span.context.trace_id, "032x")


def test_a_publish_and_its_consumer_land_in_one_trace(monkeypatch: pytest.MonkeyPatch, spans: SpanCollector) -> None:
    """The whole point of carrying W3C context over AMQP."""
    channel = FakeChannel()
    connection = ResilientRabbitMQConnection(AMQP_URL)
    monkeypatch.setattr(connection, "channel", lambda: channel)

    connection.publish("discogs-releases", b"{}")
    delivered = FakeMessage(headers=dict(channel.published[0]["properties"].headers))

    async def handler(_received: Any) -> None:
        return None

    asyncio.run(process_message_with_retry(delivered, handler))

    producer = spans.only("publish discogs-releases")
    consumer = spans.only("process discogs-releases")
    assert producer.context is not None
    assert consumer.context is not None
    assert consumer.context.trace_id == producer.context.trace_id
    assert consumer.parent is not None
    assert consumer.parent.span_id == producer.context.span_id
    assert consumer.kind is SpanKind.CONSUMER
    assert dict(consumer.attributes or {}) == {
        "messaging.system": "rabbitmq",
        "messaging.destination.name": "discogs-releases",
        "messaging.operation.name": "process",
    }
    assert delivered.acked


def test_a_message_without_context_starts_its_own_trace(spans: SpanCollector) -> None:
    async def handler(_received: Any) -> None:
        return None

    asyncio.run(process_message_with_retry(FakeMessage(), handler))

    consumer = spans.only("process discogs-releases")
    assert consumer.parent is None


def test_retries_produce_one_span_carrying_an_integer_attempt_count(spans: SpanCollector) -> None:
    attempts: list[int] = []

    async def handler(_received: Any) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("bad payload")

    asyncio.run(process_message_with_retry(FakeMessage(), handler, backoff=NO_BACKOFF))

    assert len(attempts) == 3
    span = spans.only("process discogs-releases")
    assert len(spans.spans()) == 1, "a retried message is still one delivery"
    retries = (span.attributes or {})[tracing.RETRY_COUNT_ATTRIBUTE]
    assert retries == 2
    assert isinstance(retries, int) and not isinstance(retries, bool)


def test_an_exhausted_message_fails_its_span(spans: SpanCollector) -> None:
    async def handler(_received: Any) -> None:
        raise ValueError("bad payload")

    message = FakeMessage()
    with pytest.raises(ValueError, match="bad payload"):
        asyncio.run(process_message_with_retry(message, handler, max_retries=2, backoff=NO_BACKOFF))

    span = spans.only("process discogs-releases")
    assert span.status.status_code is StatusCode.ERROR
    assert (span.attributes or {})["error.type"] == "ValueError"
    assert (span.attributes or {})[tracing.RETRY_COUNT_ATTRIBUTE] == 2
    assert message.nacked


def test_a_first_time_success_carries_no_retry_attribute(spans: SpanCollector) -> None:
    async def handler(_received: Any) -> None:
        return None

    asyncio.run(process_message_with_retry(FakeMessage(), handler))

    assert tracing.RETRY_COUNT_ATTRIBUTE not in (spans.only("process discogs-releases").attributes or {})


# --- batch flush ------------------------------------------------------------


def test_flush_span_names_the_store_and_entity(spans: SpanCollector) -> None:
    with tracing.flush_span("neo4j", "release"):
        pass

    span = spans.only("flush neo4j release")
    assert span.kind is SpanKind.INTERNAL
    assert dict(span.attributes or {}) == {"db.system.name": "neo4j", "groovemap.entity": "release"}
    assert not span.links


def test_flush_span_links_at_most_sixty_four_member_spans(spans: SpanCollector) -> None:
    tracer = tracing.get_tracer("test")
    contexts = []
    for index in range(100):
        with tracer.start_as_current_span(f"member-{index}") as member:
            contexts.append(member.get_span_context())

    with tracing.flush_span("postgresql", "release", links=contexts):
        pass

    assert len(spans.only("flush postgresql release").links) == tracing.MAX_FLUSH_LINKS


# --- disabled tracing -------------------------------------------------------


def test_the_wrappers_are_a_no_op_with_tracing_disabled(monkeypatch: pytest.MonkeyPatch, tracing_off: None) -> None:  # noqa: ARG001
    channel = FakeChannel()
    connection = ResilientRabbitMQConnection(AMQP_URL)
    monkeypatch.setattr(connection, "channel", lambda: channel)

    connection.publish("discogs-releases", b"{}")
    headers = channel.published[0]["properties"].headers
    assert tracing.TRACEPARENT_HEADER not in headers, "no live span means nothing to propagate"

    message = FakeMessage()

    async def handler(_received: Any) -> None:
        return None

    asyncio.run(process_message_with_retry(message, handler))
    assert message.acked

    asyncio.run(execute_sql(AsyncMock(), "SELECT 1"))
    with tracing.flush_span("neo4j", "release"), tracing.db_span("postgresql", "session"):
        pass
