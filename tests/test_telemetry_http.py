"""Contracts for the shared FastAPI and httpx instrumentation helpers."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from common import telemetry, tracing


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.metrics.export import Metric
    from opentelemetry.sdk.trace import ReadableSpan


FASTAPI_INSTRUMENTOR_MODULE = "opentelemetry.instrumentation.fastapi"
HTTPX_INSTRUMENTOR_MODULE = "opentelemetry.instrumentation.httpx"
SERVER_DURATION = "http.server.request.duration"


class Collector:
    """In-memory providers whose recorded metrics and spans can be read back."""

    def __init__(self) -> None:
        self.reader = InMemoryMetricReader()
        self.provider = SdkMeterProvider(metric_readers=[self.reader])
        self.span_exporter = InMemorySpanExporter()
        self.tracer_provider = SdkTracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.span_exporter))

    def spans(self) -> tuple[ReadableSpan, ...]:
        return self.span_exporter.get_finished_spans()

    def metrics(self) -> dict[str, Metric]:
        data = self.reader.get_metrics_data()
        if data is None:
            return {}
        return {
            metric.name: metric
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        }

    def attributes(self, name: str) -> list[dict[str, Any]]:
        metric = self.metrics().get(name)
        return [] if metric is None else [dict(point.attributes) for point in metric.data.data_points]


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[Collector]:
    """Make the helpers bind to an in-memory provider instead of the global one."""
    active = Collector()
    monkeypatch.setattr(telemetry, "_provider", active.provider)
    monkeypatch.setattr(telemetry, "_tracer_provider", active.tracer_provider)
    assert telemetry._active_provider() is active.provider
    assert telemetry.tracer_provider() is active.tracer_provider
    yield active
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_tracer_provider", None)


def build_app() -> FastAPI:
    """A small app with a templated route and a probe endpoint."""
    app = FastAPI()

    @app.get("/artists/{artist_id}")
    def read_artist(artist_id: str) -> dict[str, str]:
        return {"artist_id": artist_id}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    return app


def test_fastapi_helper_reports_the_templated_route_and_status(collector: Collector) -> None:
    app = build_app()
    assert telemetry.instrument_fastapi_app(app) is True

    with TestClient(app) as client:
        assert client.get("/artists/12345").status_code == 200

    recorded = collector.attributes(SERVER_DURATION)
    assert recorded, f"expected {SERVER_DURATION}, saw {sorted(collector.metrics())}"
    routes = {attributes.get("http.route") for attributes in recorded}
    assert routes == {"/artists/{artist_id}"}
    assert "/artists/12345" not in routes
    assert {attributes.get("http.response.status_code") for attributes in recorded} == {200}


def test_fastapi_helper_reports_durations_in_seconds(collector: Collector) -> None:
    app = build_app()
    telemetry.instrument_fastapi_app(app)

    with TestClient(app) as client:
        client.get("/artists/1")

    assert collector.metrics()[SERVER_DURATION].unit == "s"


def test_probe_endpoints_are_excluded_by_default(collector: Collector) -> None:
    app = build_app()
    telemetry.instrument_fastapi_app(app)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    routes = {attributes.get("http.route") for attributes in collector.attributes(SERVER_DURATION)}
    assert "/health" not in routes


def test_the_excluded_url_default_matches_the_documented_probes() -> None:
    assert telemetry.DEFAULT_EXCLUDED_URLS == "health,ready,metrics"


def test_helpers_opt_in_to_the_stable_http_semantic_conventions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the contrib packages emit http.server.duration in milliseconds instead."""
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)

    telemetry.instrument_httpx()

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"


def test_an_explicit_operator_semconv_choice_is_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")

    telemetry.instrument_httpx()

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"


def test_a_blank_semconv_value_still_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compose file declaring the variable with no value must not pin the legacy names."""
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "   ")

    telemetry.instrument_httpx()

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"


def test_fastapi_helper_is_a_safe_noop_without_the_otel_http_extra(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A None entry makes the lazy import raise ImportError, as an install without the extra does.
    monkeypatch.setitem(sys.modules, FASTAPI_INSTRUMENTOR_MODULE, None)
    app = build_app()

    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        assert telemetry.instrument_fastapi_app(app) is False

    assert any("FastAPI instrumentation unavailable" in record.getMessage() for record in caplog.records)
    with TestClient(app) as client:
        assert client.get("/artists/7").status_code == 200


def test_fastapi_helper_returns_false_instead_of_raising_on_a_bad_app(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        assert telemetry.instrument_fastapi_app(object()) is False

    assert any("Could not instrument the FastAPI app" in record.getMessage() for record in caplog.records)


def test_httpx_helper_instruments_a_single_client(collector: Collector) -> None:
    import httpx

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"artist_id": "9"}))
    with httpx.Client(transport=transport, base_url="http://catalog-api") as client:
        assert telemetry.instrument_httpx(client) is True
        assert client.get("/artists/9").status_code == 200

    recorded = collector.attributes("http.client.request.duration")
    assert recorded, f"expected a client duration metric, saw {sorted(collector.metrics())}"
    assert {attributes.get("server.address") for attributes in recorded} == {"catalog-api"}
    assert {attributes.get("http.response.status_code") for attributes in recorded} == {200}


def test_fastapi_helper_records_a_route_templated_server_span(collector: Collector) -> None:
    app = build_app()
    telemetry.instrument_fastapi_app(app)

    with TestClient(app) as client:
        assert client.get("/artists/12345").status_code == 200

    server_spans = [span for span in collector.spans() if span.kind is SpanKind.SERVER]
    assert server_spans, f"expected a server span, saw {[span.name for span in collector.spans()]}"
    assert {span.name for span in server_spans} == {"GET /artists/{artist_id}"}
    assert "12345" not in " ".join(span.name for span in server_spans)
    assert server_spans[0].attributes is not None
    assert server_spans[0].attributes["http.route"] == "/artists/{artist_id}"


def test_httpx_helper_records_a_client_span_that_propagates_the_trace(collector: Collector) -> None:
    """An outbound call inside a server span must carry that trace to the peer."""
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"artist_id": "9"})

    transport = httpx.MockTransport(handler)
    tracer = tracing.get_tracer("test")
    with (
        tracer.start_as_current_span("GET /artists/{artist_id}", kind=SpanKind.SERVER) as server,
        httpx.Client(transport=transport, base_url="http://catalog-api") as client,
    ):
        assert telemetry.instrument_httpx(client) is True
        assert client.get("/artists/9").status_code == 200
        expected = server.get_span_context()

    (request,) = seen
    traceparent = request.headers.get("traceparent")
    assert traceparent is not None, f"outbound headers carried no context: {dict(request.headers)}"
    assert traceparent.split("-")[1] == format(expected.trace_id, "032x")

    client_spans = [span for span in collector.spans() if span.kind is SpanKind.CLIENT]
    assert client_spans, f"expected a client span, saw {[span.name for span in collector.spans()]}"
    assert client_spans[0].context is not None
    assert client_spans[0].context.trace_id == expected.trace_id


def test_httpx_helper_is_a_safe_noop_without_the_otel_http_extra(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, HTTPX_INSTRUMENTOR_MODULE, None)

    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        assert telemetry.instrument_httpx() is False

    assert any("httpx instrumentation unavailable" in record.getMessage() for record in caplog.records)


def test_helpers_are_exported_from_common() -> None:
    import common

    assert {"instrument_fastapi_app", "instrument_httpx"} <= set(common.__all__)
    assert common.instrument_fastapi_app is telemetry.instrument_fastapi_app
    assert common.instrument_httpx is telemetry.instrument_httpx
