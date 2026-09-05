"""Behavioral tests for the shared OpenTelemetry tracing bootstrap and span helpers."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import NoOpTracerProvider, SpanKind

from common import telemetry, tracing


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from opentelemetry.sdk.trace import ReadableSpan


SPAN_EXPORTER_IMPORT_PATH = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
METRIC_EXPORTER_IMPORT_PATH = "opentelemetry.exporter.otlp.proto.http.metric_exporter"
ENDPOINT = "http://otel-collector:4318"

# A well-formed W3C parent whose sampled flag is set, so a ratio-0 sampler still records its
# children. Built by hand rather than by a live span: it is the wire format a broker delivers.
SAMPLED_PARENT = {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}


class CapturingSpanExporter(SpanExporter):
    """In-memory stand-in for the OTLP/HTTP span exporter that records every call."""

    def __init__(self, events: list[str] | None = None, **_kwargs: Any) -> None:
        self.spans: list[ReadableSpan] = []
        self.flush_calls = 0
        self.shutdown_calls = 0
        self.events = events if events is not None else []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        self.events.append("traces")
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
        self.flush_calls += 1
        self.events.append("traces")
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def names(self) -> list[str]:
        return [span.name for span in self.spans]


class RecordingMetricExporter(MetricExporter):
    """A metric exporter that notes when it was flushed, to prove the shutdown ordering."""

    def __init__(self, events: list[str]) -> None:
        super().__init__(preferred_temporality={}, preferred_aggregation={})
        self.events = events

    def export(self, metrics_data: Any, timeout_millis: float = 10_000, **_kwargs: Any) -> MetricExportResult:  # noqa: ARG002
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:  # noqa: ARG002
        self.events.append("metrics")
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_kwargs: Any) -> None:
        """Discard the shutdown."""


class DiscardingMetricExporter(MetricExporter):
    """Stands in for the OTLP metric exporter so this suite never opens a socket."""

    def __init__(self, **_kwargs: Any) -> None:
        super().__init__(preferred_temporality={}, preferred_aggregation={})

    def export(self, metrics_data: Any, timeout_millis: float = 10_000, **_kwargs: Any) -> MetricExportResult:  # noqa: ARG002
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:  # noqa: ARG002
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_kwargs: Any) -> None:
        """Discard the shutdown."""


@pytest.fixture(autouse=True)
def discarded_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tracing suite off the network: setup_telemetry configures both signals."""
    monkeypatch.setattr(f"{METRIC_EXPORTER_IMPORT_PATH}.OTLPMetricExporter", DiscardingMetricExporter)


@pytest.fixture(autouse=True)
def isolated_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test pristine provider handles; conftest clears the environment."""
    for name in ("_provider", "_sdk_provider", "_tracer_provider", "_sdk_tracer_provider"):
        monkeypatch.setattr(telemetry, name, None)
    yield
    for name in ("_provider", "_sdk_provider", "_tracer_provider", "_sdk_tracer_provider"):
        monkeypatch.setattr(telemetry, name, None)


@pytest.fixture
def capturing_spans(monkeypatch: pytest.MonkeyPatch) -> list[CapturingSpanExporter]:
    """Replace the OTLP span exporter the bootstrap constructs with a capturing one."""
    built: list[CapturingSpanExporter] = []

    def factory(**kwargs: Any) -> CapturingSpanExporter:
        exporter = CapturingSpanExporter(**kwargs)
        built.append(exporter)
        return exporter

    monkeypatch.setattr(f"{SPAN_EXPORTER_IMPORT_PATH}.OTLPSpanExporter", factory)
    return built


def test_setup_installs_a_batching_otlp_span_processor(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],
) -> None:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)

    telemetry.setup_telemetry("graphinator")
    provider = telemetry.tracer_provider()

    assert isinstance(provider, SdkTracerProvider)
    assert len(capturing_spans) == 1
    (processor,) = provider._active_span_processor._span_processors
    assert isinstance(processor, BatchSpanProcessor)
    assert processor.span_exporter is capturing_spans[0]


def test_spans_reach_the_exporter_and_carry_the_service_resource(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    telemetry.setup_telemetry("tableinator", service_version="1.2.3")

    with tracing.get_tracer(tracing.INSTRUMENTATION_SCOPE).start_as_current_span("session postgresql", kind=SpanKind.CLIENT):
        pass
    telemetry.shutdown_telemetry()

    exporter = capturing_spans[0]
    assert exporter.names() == ["session postgresql"]
    exported = exporter.spans[0]
    assert exported.kind is SpanKind.CLIENT
    assert exported.resource.attributes["service.name"] == "tableinator"
    assert exported.resource.attributes["service.version"] == "1.2.3"


def test_traces_exporter_none_keeps_the_noop_tracer_provider(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "None")

    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        telemetry.setup_telemetry("dashboard")

    assert isinstance(telemetry.tracer_provider(), NoOpTracerProvider)
    assert any("OTEL_TRACES_EXPORTER=none" in record.getMessage() for record in caplog.records)


def test_setup_without_an_endpoint_keeps_the_noop_tracer_provider(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        telemetry.setup_telemetry("extractor-discogs")

    assert isinstance(telemetry.tracer_provider(), NoOpTracerProvider)
    disabled = [record.getMessage() for record in caplog.records if "tracing disabled" in record.getMessage()]
    assert len(disabled) == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT is unset" in disabled[0]


def test_either_signal_can_be_off_while_the_other_is_on(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],
) -> None:
    """Metrics off, tracing on: the two halves are configured independently."""
    from opentelemetry.metrics import NoOpMeterProvider

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")

    provider = telemetry.setup_telemetry("insights")

    assert isinstance(provider, NoOpMeterProvider)
    assert isinstance(telemetry.tracer_provider(), SdkTracerProvider)
    assert len(capturing_spans) == 1


def test_tracing_off_leaves_metrics_exporting(monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")

    provider = telemetry.setup_telemetry("mcp-server")

    assert isinstance(provider, SdkMeterProvider)
    assert isinstance(telemetry.tracer_provider(), NoOpTracerProvider)


def test_the_sampler_defaults_to_parentbased_traceidratio_at_one(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)

    telemetry.setup_telemetry("api")
    provider = telemetry.tracer_provider()

    assert isinstance(provider, SdkTracerProvider)
    assert provider.sampler.get_description().startswith("ParentBased{root:TraceIdRatioBased{1.0}")
    with tracing.get_tracer("test").start_as_current_span("api.sync") as span:
        assert span.is_recording()


def test_a_zero_ratio_drops_root_spans_but_keeps_a_sampled_parents_children(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0")

    telemetry.setup_telemetry("extractor-musicbrainz")
    tracer = tracing.get_tracer("test")

    with tracer.start_as_current_span("extract discogs release") as root:
        assert not root.is_recording(), "a ratio of 0 must drop root spans"

    parent = tracing.extract_context(SAMPLED_PARENT)
    with tracer.start_as_current_span("process discogs-releases", context=parent) as child:
        assert child.is_recording(), "a sampled parent must keep its children"

    telemetry.shutdown_telemetry()
    assert capturing_spans[0].names() == ["process discogs-releases"]


def test_a_malformed_sampler_argument_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "not-a-ratio")

    telemetry.setup_telemetry("console")
    provider = telemetry.tracer_provider()

    assert isinstance(provider, SdkTracerProvider)
    with tracing.get_tracer("test").start_as_current_span("console.poll rabbitmq") as span:
        assert span.is_recording(), "an unreadable ratio must fall back to sampling everything"


def test_an_operator_sampler_choice_is_not_overridden(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")

    telemetry.setup_telemetry("schema-init")
    provider = telemetry.tracer_provider()

    assert isinstance(provider, SdkTracerProvider)
    assert provider.sampler.get_description() == "AlwaysOffSampler"


def test_tracing_bootstrap_failure_degrades_to_the_noop_provider(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A configured endpoint with no SDK installed must log and continue, never raise."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setitem(sys.modules, SPAN_EXPORTER_IMPORT_PATH, None)
    monkeypatch.setitem(sys.modules, METRIC_EXPORTER_IMPORT_PATH, None)

    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        telemetry.setup_telemetry("discogs-graph-enricher")

    assert isinstance(telemetry.tracer_provider(), NoOpTracerProvider)
    assert any("tracing bootstrap failed" in record.getMessage() for record in caplog.records)
    with tracing.get_tracer("test").start_as_current_span("insights collaborations"):
        pass


def test_setup_is_idempotent_for_both_signals(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)

    telemetry.setup_telemetry("catalog-api")
    first = telemetry.tracer_provider()
    telemetry.setup_telemetry("catalog-api")

    assert telemetry.tracer_provider() is first
    assert len(capturing_spans) == 1


def test_shutdown_flushes_traces_before_metrics_and_allows_a_later_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    spans: list[CapturingSpanExporter] = []

    def span_factory(**kwargs: Any) -> CapturingSpanExporter:
        exporter = CapturingSpanExporter(events=events, **kwargs)
        spans.append(exporter)
        return exporter

    def metric_factory(**kwargs: Any) -> RecordingMetricExporter:  # noqa: ARG001
        return RecordingMetricExporter(events)

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")
    monkeypatch.setattr(f"{SPAN_EXPORTER_IMPORT_PATH}.OTLPSpanExporter", span_factory)
    monkeypatch.setattr(f"{METRIC_EXPORTER_IMPORT_PATH}.OTLPMetricExporter", metric_factory)

    telemetry.setup_telemetry("api")
    with tracing.get_tracer("test").start_as_current_span("api.sync"):
        pass
    telemetry.shutdown_telemetry()

    assert events, "neither provider was flushed"
    assert events.index("traces") < events.index("metrics")
    assert spans[0].shutdown_calls == 1
    assert spans[0].names() == ["api.sync"]

    telemetry.setup_telemetry("api")
    assert len(spans) == 2, "shutdown must let a later setup reconfigure tracing"


def test_shutdown_without_setup_is_a_noop() -> None:
    telemetry.shutdown_telemetry()

    assert telemetry._tracer_provider is None
    assert telemetry._sdk_tracer_provider is None


def test_tracer_shutdown_never_raises(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingTracerProvider:
        def force_flush(self, timeout_millis: int = 30_000) -> bool:  # noqa: ARG002
            raise RuntimeError("collector unreachable")

        def shutdown(self) -> None:
            raise RuntimeError("already shut down")

    monkeypatch.setattr(telemetry, "_sdk_tracer_provider", ExplodingTracerProvider())
    monkeypatch.setattr(telemetry, "_tracer_provider", ExplodingTracerProvider())

    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        telemetry.shutdown_telemetry()

    messages = [record.getMessage() for record in caplog.records]
    assert any("tracing force-flush failed" in message for message in messages)
    assert any("tracer provider shutdown failed" in message for message in messages)
    assert telemetry._tracer_provider is None
    assert telemetry._sdk_tracer_provider is None


def test_get_tracer_before_setup_returns_a_usable_tracer() -> None:
    """Before setup the helper falls back to the API global, exactly as get_meter does."""
    from opentelemetry import trace as trace_api

    assert telemetry.tracer_provider() is trace_api.get_tracer_provider()
    with tracing.get_tracer("common.tracing").start_as_current_span("schema_init neo4j") as span:
        span.set_attribute("db.system.name", "neo4j")


def test_the_propagator_round_trips_traceparent_through_a_plain_dict(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    telemetry.setup_telemetry("extractor-discogs")
    tracer = tracing.get_tracer("test")

    headers: dict[str, Any] = {}
    with tracer.start_as_current_span("publish discogs-releases", kind=SpanKind.PRODUCER) as producer:
        tracing.inject_headers(headers)
        expected = producer.get_span_context()

    assert tracing.TRACEPARENT_HEADER in headers
    assert isinstance(headers[tracing.TRACEPARENT_HEADER], str)

    with tracer.start_as_current_span("process discogs-releases", context=tracing.extract_context(headers)) as consumer:
        assert consumer.get_span_context().trace_id == expected.trace_id
        assert consumer.parent is not None
        assert consumer.parent.span_id == expected.span_id


def test_the_propagator_round_trips_through_amqp_style_bytes_headers(
    monkeypatch: pytest.MonkeyPatch,
    capturing_spans: list[CapturingSpanExporter],  # noqa: ARG001
) -> None:
    """Some brokers and clients hand back header values as bytes; the default getter cannot."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    telemetry.setup_telemetry("extractor-musicbrainz")
    tracer = tracing.get_tracer("test")

    headers: dict[str, Any] = {"x-groovemap-entity": b"release"}
    with tracer.start_as_current_span("publish musicbrainz-releases", kind=SpanKind.PRODUCER) as producer:
        tracing.inject_headers(headers)
        expected = producer.get_span_context()

    delivered = {key: value.encode() if isinstance(value, str) else value for key, value in headers.items()}
    assert delivered["x-groovemap-entity"] == b"release"

    with tracer.start_as_current_span("process musicbrainz-releases", context=tracing.extract_context(delivered)) as consumer:
        assert consumer.get_span_context().trace_id == expected.trace_id
        assert consumer.parent is not None
        assert consumer.parent.span_id == expected.span_id


def test_extract_returns_none_when_no_readable_context_survives() -> None:
    assert tracing.extract_context({}) is None
    assert tracing.extract_context({"x-groovemap-entity": b"release"}) is None
    assert tracing.extract_context({"x-groovemap-entity": b"\xff\xfe"}) is None, "undecodable bytes must not raise"


def test_a_malformed_traceparent_starts_a_new_trace_instead_of_failing() -> None:
    assert tracing.extract_context({"traceparent": "nonsense"}) is None
    assert tracing.extract_context({"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"}) is None


def test_inject_leaves_existing_header_values_alone() -> None:
    headers: dict[str, Any] = {"x-groovemap-entity": b"release"}

    assert tracing.inject_headers(headers) is headers
    assert headers["x-groovemap-entity"] == b"release"


def test_the_helpers_are_noops_without_the_otel_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """A consumer on an older lockfile resolves this library without the `otel` extra."""
    monkeypatch.setattr(telemetry, "metrics", None)
    monkeypatch.setattr(telemetry, "trace", None)
    monkeypatch.setitem(sys.modules, SPAN_EXPORTER_IMPORT_PATH, None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)

    telemetry.setup_telemetry("dashboard")

    assert isinstance(telemetry.tracer_provider(), telemetry._NoOpTracerProvider)
    tracer = tracing.get_tracer("groovemap.runtime")
    with tracer.start_as_current_span("publish graphinator", kind=None) as span:
        span.set_attribute("messaging.destination.name", "graphinator")
        span.set_status(None)
        span.record_exception(RuntimeError("ignored"))
        assert span.is_recording() is False

    headers: dict[str, Any] = {}
    assert tracing.inject_headers(headers) is headers
    assert headers == {}
    assert tracing.extract_context(SAMPLED_PARENT) is None
    telemetry.shutdown_telemetry()


def test_the_wrapper_span_helpers_are_noops_without_the_otel_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resilience wrappers import common.tracing at module scope, so these run everywhere."""
    monkeypatch.setattr(telemetry, "metrics", None)
    monkeypatch.setattr(telemetry, "trace", None)

    with tracing.db_span("postgresql", "session") as span:
        assert isinstance(span, telemetry._NoOpSpan)
        span.set_attribute("db.system.name", "postgresql")
    with tracing.publish_span("graphinator") as producer:
        headers: dict[str, Any] = {}
        tracing.inject_headers(headers)
        assert headers == {}, "there is no context to propagate without the API"
        assert isinstance(producer, telemetry._NoOpSpan)
    with tracing.consume_span("discogs-releases", SAMPLED_PARENT) as consumer:
        tracing.set_retry_count(consumer, 2)
        assert isinstance(consumer, telemetry._NoOpSpan)
    with tracing.flush_span("neo4j", "release", links=[object(), object()]) as flush:
        assert isinstance(flush, telemetry._NoOpSpan)


def test_a_failing_body_still_propagates_without_the_otel_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "trace", None)

    with pytest.raises(ValueError, match="bad payload"), tracing.db_span("neo4j", "session"):
        raise ValueError("bad payload")


def test_public_names_are_exported_lazily_from_common() -> None:
    import common

    assert {"extract_context", "get_tracer", "inject_headers"} <= set(common.__all__)
    assert common.get_tracer is tracing.get_tracer
    assert common.inject_headers is tracing.inject_headers
    assert common.extract_context is tracing.extract_context
