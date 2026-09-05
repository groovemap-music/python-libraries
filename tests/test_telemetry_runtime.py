"""Contracts for the process view and the event-loop lag histogram.

The instrument names here are copied into the deployment metric catalog and referenced by
dashboard panels, so this suite asserts the exact names the pinned instrumentor emits rather
than that "some" runtime metric exists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from common import telemetry
from tests.test_telemetry import CapturingExporter, DiscardingSpanExporter


if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.metrics.export import Metric, MetricsData


ENDPOINT = "http://otel-collector:4318"
METRIC_EXPORTER_IMPORT_PATH = "opentelemetry.exporter.otlp.proto.http.metric_exporter"
SPAN_EXPORTER_IMPORT_PATH = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
SYSTEM_METRICS_MODULE = "opentelemetry.instrumentation.system_metrics"

# Every instrument the pinned instrumentor emits for RUNTIME_METRICS_CONFIG, as recorded in
# docs/runtime.md. Two are platform-conditional, so they are asserted separately.
PROCESS_INSTRUMENTS = {
    "cpython.gc.collections",
    "process.cpu.time",
    "process.cpu.utilization",
    "process.memory.usage",
    "process.memory.virtual",
    "process.thread.count",
}


@pytest.fixture(autouse=True)
def isolated_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the provider handles and leave the instrumentor uninstrumented for the next test."""
    for name in ("_provider", "_sdk_provider", "_tracer_provider", "_sdk_tracer_provider"):
        monkeypatch.setattr(telemetry, name, None)
    monkeypatch.setattr(f"{SPAN_EXPORTER_IMPORT_PATH}.OTLPSpanExporter", DiscardingSpanExporter)
    telemetry._event_loop_monitors.clear()
    yield
    telemetry._event_loop_monitors.clear()
    # The instrumentor is a process-wide singleton: without this, the second test in this file
    # would be told it is already instrumented and register nothing.
    if SystemMetricsInstrumentor().is_instrumented_by_opentelemetry:
        SystemMetricsInstrumentor().uninstrument()
    for name in ("_provider", "_sdk_provider", "_tracer_provider", "_sdk_tracer_provider"):
        monkeypatch.setattr(telemetry, name, None)


@pytest.fixture
def capturing_exporter(monkeypatch: pytest.MonkeyPatch) -> list[CapturingExporter]:
    """Replace the OTLP metric exporter the bootstrap constructs with a capturing one."""
    built: list[CapturingExporter] = []

    def factory(**kwargs: Any) -> CapturingExporter:
        exporter = CapturingExporter(**kwargs)
        built.append(exporter)
        return exporter

    monkeypatch.setattr(f"{METRIC_EXPORTER_IMPORT_PATH}.OTLPMetricExporter", factory)
    return built


def _exported_names(exported: list[MetricsData]) -> set[str]:
    return {
        metric.name
        for metrics_data in exported
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def _collected(reader: InMemoryMetricReader) -> dict[str, Metric]:
    data = reader.get_metrics_data()
    if data is None:
        return {}
    return {
        metric.name: metric
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def test_the_configured_subset_is_process_scoped_only() -> None:
    assert not any(key.startswith("system.") for key in telemetry.RUNTIME_METRICS_CONFIG)
    assert "process.cpu.time" in telemetry.RUNTIME_METRICS_CONFIG
    assert "cpython.gc.collections" in telemetry.RUNTIME_METRICS_CONFIG


def test_the_instrumentor_registers_the_documented_process_instruments() -> None:
    reader = InMemoryMetricReader()
    provider = SdkMeterProvider(metric_readers=[reader])

    assert telemetry._install_runtime_metrics(provider) is True

    registered = _collected(reader)
    assert set(registered) >= PROCESS_INSTRUMENTS
    assert not [name for name in registered if name.startswith("system.")], "host metrics belong to node-exporter"
    assert registered["process.cpu.time"].unit == "s"
    assert registered["process.memory.usage"].unit == "By"
    assert {"type"} == {key for point in registered["process.cpu.time"].data.data_points for key in point.attributes}


@pytest.mark.skipif(sys.platform == "win32", reason="the instrumentor omits file descriptors on Windows")
def test_the_open_file_descriptor_instrument_is_registered_off_windows() -> None:
    reader = InMemoryMetricReader()
    telemetry._install_runtime_metrics(SdkMeterProvider(metric_readers=[reader]))

    assert "process.open_file_descriptor.count" in _collected(reader)


def test_setup_installs_the_process_view(monkeypatch: pytest.MonkeyPatch, capturing_exporter: list[CapturingExporter]) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")

    telemetry.setup_telemetry("graphinator")
    telemetry.shutdown_telemetry()

    exported = _exported_names(capturing_exporter[0].exported)
    assert exported >= PROCESS_INSTRUMENTS, f"missing runtime instruments, saw {sorted(exported)}"
    assert not [name for name in exported if name.startswith("system.")]


def test_setup_opts_into_the_stable_http_semconv_before_instrumenting(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],  # noqa: ARG001
) -> None:
    """The contrib semconv cache is filled by whichever instrumentation initializes first."""
    recorded: list[str | None] = []
    install = telemetry._install_runtime_metrics

    def spy(provider: Any) -> bool:
        recorded.append(os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN"))
        return install(provider)

    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(telemetry, "_install_runtime_metrics", spy)

    telemetry.setup_telemetry("api")

    assert recorded == ["http"], "the process view must not initialize the cache before the opt-in"


def test_a_disabled_bootstrap_does_not_install_the_process_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing to export to, the process view is not worth a psutil handle."""
    monkeypatch.setattr(f"{SYSTEM_METRICS_MODULE}.SystemMetricsInstrumentor", _exploding_instrumentor)

    telemetry.setup_telemetry("dashboard")


def _exploding_instrumentor(**_kwargs: Any) -> Any:
    raise RuntimeError("psutil is unavailable")


def test_a_failing_instrumentor_degrades_to_a_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    reader = InMemoryMetricReader()
    monkeypatch.setattr(f"{SYSTEM_METRICS_MODULE}.SystemMetricsInstrumentor", _exploding_instrumentor)

    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        assert telemetry._install_runtime_metrics(SdkMeterProvider(metric_readers=[reader])) is False

    assert any("runtime metrics instrumentation" in record.getMessage() for record in caplog.records)


def test_a_missing_instrumentor_package_logs_once(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setitem(sys.modules, SYSTEM_METRICS_MODULE, None)

    with caplog.at_level(logging.INFO, logger=telemetry.__name__):
        assert telemetry._install_runtime_metrics(SdkMeterProvider()) is False

    assert any("Runtime metrics unavailable" in record.getMessage() for record in caplog.records)


def test_the_lag_histogram_name_and_buckets_are_the_documented_ones() -> None:
    assert telemetry.EVENT_LOOP_LAG == "groovemap.runtime.event_loop.lag"
    assert telemetry.EVENT_LOOP_LAG_BUCKETS[0] == 0.001
    assert telemetry.EVENT_LOOP_LAG_BUCKETS[-1] == 5.0


def test_the_monitor_is_a_noop_outside_a_running_loop(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
        assert telemetry.start_event_loop_monitor() is None


@pytest.mark.asyncio
async def test_the_monitor_is_a_noop_before_setup() -> None:
    assert telemetry.start_event_loop_monitor() is None
    assert not telemetry._event_loop_monitors


@pytest.mark.asyncio
async def test_the_monitor_is_idempotent_per_loop(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],  # noqa: ARG001
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    telemetry.setup_telemetry("tableinator")

    first = telemetry.start_event_loop_monitor(interval_s=0.05)
    second = telemetry.start_event_loop_monitor(interval_s=0.05)

    assert first is not None
    assert second is first
    assert len(telemetry._event_loop_monitors) == 1

    telemetry.shutdown_telemetry()
    await asyncio.gather(first, return_exceptions=True)
    assert first.done()


@pytest.mark.asyncio
async def test_the_monitor_records_a_deliberate_block_and_stops_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")
    telemetry.setup_telemetry("extractor-discogs")

    monitor = telemetry.start_event_loop_monitor(interval_s=0.05)
    assert monitor is not None
    await asyncio.sleep(0)  # let the sampler take its first timestamp

    time.sleep(0.4)
    await asyncio.sleep(0.15)

    telemetry.shutdown_telemetry()
    await asyncio.gather(monitor, return_exceptions=True)

    assert monitor.cancelled(), "shutdown must cancel the sampler"
    exported = capturing_exporter[0].exported
    points = [
        point
        for metrics_data in exported
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == telemetry.EVENT_LOOP_LAG
        for point in metric.data.data_points
    ]
    assert points, f"no {telemetry.EVENT_LOOP_LAG} was recorded"
    assert max(point.max for point in points) >= 0.2, "the recorded lag must reflect the block"
    assert tuple(points[0].explicit_bounds) == telemetry.EVENT_LOOP_LAG_BUCKETS


@pytest.mark.asyncio
async def test_the_lag_histogram_is_reported_in_seconds(
    monkeypatch: pytest.MonkeyPatch,
    capturing_exporter: list[CapturingExporter],
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_METRIC_EXPORT_INTERVAL", "600000")
    telemetry.setup_telemetry("mcp-server")

    telemetry.start_event_loop_monitor(interval_s=0.01)
    await asyncio.sleep(0.05)
    telemetry.shutdown_telemetry()
    await asyncio.sleep(0.01)

    units = {
        metric.unit
        for metrics_data in capturing_exporter[0].exported
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == telemetry.EVENT_LOOP_LAG
    }
    assert units == {"s"}


def test_the_monitor_is_exported_from_common() -> None:
    import common

    assert "start_event_loop_monitor" in common.__all__
    assert common.start_event_loop_monitor is telemetry.start_event_loop_monitor
