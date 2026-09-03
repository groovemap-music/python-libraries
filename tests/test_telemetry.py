"""Behavioral tests for the shared OpenTelemetry metrics bootstrap."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import Meter as SdkMeter
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricExporter, MetricExportResult

from common import telemetry


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.metrics.export import MetricsData


OTEL_ENVIRONMENT = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_METRICS_EXPORTER",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
)
EXPORTER_IMPORT_PATH = "opentelemetry.exporter.otlp.proto.http.metric_exporter"


class CapturingExporter(MetricExporter):
    """In-memory stand-in for the OTLP/HTTP exporter that records every export call."""

    def __init__(self, **_kwargs: Any) -> None:
        super().__init__(preferred_temporality={}, preferred_aggregation={})
        self.exported: list[MetricsData] = []
        self.flush_calls = 0
        self.shutdown_calls = 0

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **_kwargs: Any) -> MetricExportResult:  # noqa: ARG002
        self.exported.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:  # noqa: ARG002
        self.flush_calls += 1
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_kwargs: Any) -> None:  # noqa: ARG002
        self.shutdown_calls += 1


@pytest.fixture(autouse=True)
def isolated_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a pristine, environment-free telemetry module."""
    for name in OTEL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_sdk_provider", None)
    yield
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_sdk_provider", None)


@pytest.fixture
def capturing_exporter(monkeypatch: pytest.MonkeyPatch) -> list[CapturingExporter]:
    """Replace the OTLP exporter the bootstrap constructs with a capturing one."""
    built: list[CapturingExporter] = []

    def factory(**kwargs: Any) -> CapturingExporter:
        exporter = CapturingExporter(**kwargs)
        built.append(exporter)
        return exporter

    monkeypatch.setattr(f"{EXPORTER_IMPORT_PATH}.OTLPMetricExporter", factory)
    return built


def _metric_names(exported: list[MetricsData]) -> set[str]:
    return {
        metric.name
        for metrics_data in exported
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_setup_without_an_endpoint_keeps_the_noop_provider(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        provider = telemetry.setup_telemetry("extractor-discogs")

    assert isinstance(provider, NoOpMeterProvider)
    disabled_lines = [record for record in caplog.records if "disabled" in record.getMessage()]
    assert len(disabled_lines) == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT is unset" in disabled_lines[0].getMessage()
    assert disabled_lines[0].levelno == logging.INFO


def test_setup_honors_the_metrics_exporter_none_switch(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "None")

    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        provider = telemetry.setup_telemetry("graphinator")

    assert isinstance(provider, NoOpMeterProvider)
    assert any("OTEL_METRICS_EXPORTER=none" in record.getMessage() for record in caplog.records)


def test_setup_without_the_otel_extra_degrades_to_the_noop_provider(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A configured endpoint with no SDK installed must log and continue, never raise."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    # A None entry in sys.modules makes the lazy import inside the bootstrap raise ImportError,
    # which is exactly what an install without the `otel` extra produces.
    monkeypatch.setitem(sys.modules, EXPORTER_IMPORT_PATH, None)

    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        provider = telemetry.setup_telemetry("tableinator")

    assert isinstance(provider, NoOpMeterProvider)
    assert any("bootstrap failed" in record.getMessage() for record in caplog.records)
    assert telemetry.get_meter("tableinator").create_counter("groovemap.pipeline.messages") is not None


def test_setup_installs_a_periodic_otlp_reader_over_the_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")

    provider = telemetry.setup_telemetry("brainzgraphinator", service_version="1.2.3")

    assert isinstance(provider, SdkMeterProvider)
    assert len(capturing_exporter) == 1
    # _all_metric_readers is a class-level registry shared by every provider in the process;
    # only _metric_readers is this provider's own.
    (reader,) = provider._metric_readers
    assert reader._exporter is capturing_exporter[0]
    assert reader._export_interval_millis == 600000


def test_resource_carries_service_identity_merged_with_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.namespace=groovemap,deployment.environment.name=dev")

    provider = telemetry.setup_telemetry("schema-init", service_version="0.1.0")
    assert isinstance(provider, SdkMeterProvider)

    attributes = dict(provider._sdk_config.resource.attributes)
    assert attributes["service.name"] == "schema-init"
    assert attributes["service.version"] == "0.1.0"
    assert attributes["service.namespace"] == "groovemap"
    assert attributes["deployment.environment.name"] == "dev"
    assert attributes["telemetry.sdk.language"] == "python"
    assert capturing_exporter  # the configured path really built an exporter


def test_environment_service_name_outranks_the_code_default(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "extractor-musicbrainz")

    provider = telemetry.setup_telemetry("code-default-name")
    assert isinstance(provider, SdkMeterProvider)

    assert provider._sdk_config.resource.attributes["service.name"] == "extractor-musicbrainz"


def test_service_version_falls_back_to_the_installed_distribution(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")

    provider = telemetry.setup_telemetry("groovemap-runtime")
    assert isinstance(provider, SdkMeterProvider)

    assert provider._sdk_config.resource.attributes["service.version"] == "0.1.0"


def test_service_version_is_omitted_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")

    provider = telemetry.setup_telemetry("no-such-distribution-anywhere")
    assert isinstance(provider, SdkMeterProvider)

    assert "service.version" not in provider._sdk_config.resource.attributes


def test_setup_is_idempotent(monkeypatch: pytest.MonkeyPatch, capturing_exporter: list[CapturingExporter]) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")

    first = telemetry.setup_telemetry("insights")
    second = telemetry.setup_telemetry("insights")

    assert first is second
    assert len(capturing_exporter) == 1


def test_disabled_setup_is_idempotent() -> None:
    first = telemetry.setup_telemetry("dashboard")
    second = telemetry.setup_telemetry("dashboard")

    assert first is second


def test_get_meter_records_through_the_configured_provider_and_shutdown_flushes(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    # Keep the periodic push far away so the only export is the one shutdown forces.
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")
    provider = telemetry.setup_telemetry("mcp-server", service_version="0.1.0")
    assert isinstance(provider, SdkMeterProvider), "the configured path must install the SDK provider"

    meter = telemetry.get_meter("common.telemetry", "0.1.0")
    assert isinstance(meter, SdkMeter), "get_meter must hand back a recording meter, not a no-op one"
    meter.create_counter("groovemap.pipeline.messages").add(1, {"source": "discogs", "outcome": "processed"})

    assert len(capturing_exporter) == 1
    exporter = capturing_exporter[0]
    assert exporter.exported == []

    telemetry.shutdown_telemetry()

    assert exporter.flush_calls == 1
    assert exporter.shutdown_calls == 1
    assert "groovemap.pipeline.messages" in _metric_names(exporter.exported)
    counter = exporter.exported[0].resource_metrics[0].scope_metrics[0].metrics[0]
    assert counter.data.aggregation_temporality == AggregationTemporality.CUMULATIVE


def test_get_meter_before_setup_returns_a_usable_meter() -> None:
    meter = telemetry.get_meter("common.telemetry")

    assert meter.create_histogram("groovemap.pipeline.message.duration", unit="s") is not None


def test_shutdown_without_setup_is_a_noop() -> None:
    telemetry.shutdown_telemetry()

    assert telemetry._provider is None


def test_shutdown_never_raises_and_resets_state(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingProvider:
        def force_flush(self, timeout_millis: float = 10_000) -> bool:  # noqa: ARG002
            raise RuntimeError("collector unreachable")

        def shutdown(self, timeout_millis: float = 30_000) -> None:  # noqa: ARG002
            raise RuntimeError("already shut down")

    monkeypatch.setattr(telemetry, "_sdk_provider", ExplodingProvider())
    monkeypatch.setattr(telemetry, "_provider", ExplodingProvider())

    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        telemetry.shutdown_telemetry()

    messages = [record.getMessage() for record in caplog.records]
    assert any("force-flush failed" in message for message in messages)
    assert any("shutdown failed" in message for message in messages)
    assert telemetry._provider is None
    assert telemetry._sdk_provider is None


def test_shutdown_allows_a_later_setup_to_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")

    first = telemetry.setup_telemetry("api")
    telemetry.shutdown_telemetry()
    second = telemetry.setup_telemetry("api")

    assert first is not second
    assert len(capturing_exporter) == 2


def test_public_names_are_exported_lazily_from_common() -> None:
    import common

    assert {"get_meter", "setup_telemetry", "shutdown_telemetry"} <= set(common.__all__)
    assert common.setup_telemetry is telemetry.setup_telemetry
    assert common.shutdown_telemetry is telemetry.shutdown_telemetry
    assert common.get_meter is telemetry.get_meter
