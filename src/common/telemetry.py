"""OpenTelemetry metrics and tracing bootstrap shared by every GrooveMap service.

One call configures the OpenTelemetry SDK from the standard environment variables and one
call flushes it, so no service has to reimplement provider, reader, exporter, sampler, and
propagator wiring.

Transport is OTLP over HTTP/protobuf; there is no gRPC dependency and no Prometheus scrape
endpoint. Configuration is read from the standard OpenTelemetry environment variables only:

- ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the metrics-specific
  ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT``) selects the collector. Unset means telemetry off.
- ``OTEL_METRICS_EXPORTER`` accepts ``otlp`` (default) or ``none`` to force telemetry off.
- ``OTEL_METRIC_EXPORT_INTERVAL`` sets the push interval in milliseconds (SDK default 60000).
- ``OTEL_SERVICE_NAME`` and ``OTEL_RESOURCE_ATTRIBUTES`` override the resource attributes the
  bootstrap derives from its arguments.
- ``OTEL_TRACES_EXPORTER`` accepts ``otlp`` (default) or ``none`` to force tracing off.
- ``OTEL_TRACES_SAMPLER`` and ``OTEL_TRACES_SAMPLER_ARG`` select the sampler, defaulting to
  ``parentbased_traceidratio`` at ratio ``1.0``.

The two signals are configured from one resource but stay independent: either can be off while
the other is on. Telemetry never fails startup: a missing ``otel`` extra, a missing endpoint, or
a broken SDK configuration all fall back to no-op providers and log instead of raising. The
whole module imports and works without any ``opentelemetry`` package installed, because a
consumer pinned to an older lockfile resolves this library without the extra.

The bootstrap also turns on the process view — CPU, memory, threads, file descriptors, and
garbage collection — and offers an asyncio event-loop lag histogram, because a saturated
consumer and an idle one look identical on every dashboard without them.

Span helpers — ``get_tracer``, header injection and extraction, and the wrapper span shapes —
live next door in :mod:`common.tracing`; this module owns provider lifecycle only.
"""

import asyncio
import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from os import environ, getenv
from threading import RLock
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakKeyDictionary


try:
    from opentelemetry import metrics as _metrics_api
    from opentelemetry import trace as _trace_api
except ImportError:  # pragma: no cover - covered by the no-op shim tests
    # The OpenTelemetry API ships with the `otel` extra, not with the base package. A consumer
    # pinned to an older lockfile resolves this library without it, and `common` must keep
    # importing and working there: every instrument simply becomes a local no-op.
    _metrics_api = None  # type: ignore[assignment]
    _trace_api = None  # type: ignore[assignment]

# Deliberately untyped: mypy runs with the extra installed and would otherwise prove every
# `metrics is None` / `trace is None` guard unreachable, which is exactly the case this module
# has to handle.
metrics: Any = _metrics_api
trace: Any = _trace_api


if TYPE_CHECKING:  # pragma: no cover
    from asyncio import AbstractEventLoop, Task

    from opentelemetry.metrics import Meter, MeterProvider
    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
    from opentelemetry.trace import TracerProvider


logger = logging.getLogger(__name__)

# The single value of OTEL_METRICS_EXPORTER / OTEL_TRACES_EXPORTER that means "collect nothing".
_EXPORTER_DISABLED = "none"

# GrooveMap samples every span by default and turns the ratio down per deployment, so the
# sampler defaults to the ratio form rather than the SDK's parentbased_always_on. An operator
# value always wins; see _default_the_sampler.
_DEFAULT_TRACES_SAMPLER = "parentbased_traceidratio"
_DEFAULT_TRACES_SAMPLER_ARG = "1.0"

# The scope this library's own instruments and spans are reported under.
INSTRUMENTATION_SCOPE = "groovemap.runtime"

# The process-scoped subset of the system-metrics instrumentor. No `system.` key appears here:
# host metrics belong to node-exporter, and a service reporting them would multiply one host's
# numbers by however many containers happen to run on it. The instrumentor decides the emitted
# instrument names; docs/runtime.md records the ones the pinned version produces.
RUNTIME_METRICS_CONFIG: dict[str, list[str] | None] = {
    "cpython.gc.collections": None,
    "process.context_switches": ["involuntary", "voluntary"],
    "process.cpu.time": ["user", "system"],
    "process.cpu.utilization": None,
    "process.memory.usage": None,
    "process.memory.virtual": None,
    "process.open_file_descriptor.count": None,
    "process.thread.count": None,
}

# Scheduling delay of an asyncio loop, the one runtime signal no library provides.
EVENT_LOOP_LAG = "groovemap.runtime.event_loop.lag"

# A healthy loop schedules within a millisecond; a saturated one takes seconds. The boundaries
# span that range so both ends stay legible on a heatmap instead of piling into one bucket.
EVENT_LOOP_LAG_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

# Probe endpoints are excluded by default: they are polled constantly and would otherwise
# dominate the request histogram without telling an operator anything.
DEFAULT_EXCLUDED_URLS = "health,ready,metrics"

# The contrib HTTP instrumentations still default to the pre-stable metric names
# (http.server.duration in milliseconds). GrooveMap dashboards are written against the stable
# semantic conventions, so opt in unless an operator has already chosen a value. This is a
# standard OpenTelemetry environment variable, not a GrooveMap-specific one.
_SEMCONV_STABILITY_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
_STABLE_HTTP_SEMCONV = "http"


class _NoOpInstrument:
    """Accepts and discards every measurement, for installs without the `otel` extra."""

    def add(self, amount: float, attributes: Any = None, context: Any = None) -> None:
        """Discard a counter measurement."""

    def record(self, amount: float, attributes: Any = None, context: Any = None) -> None:
        """Discard a histogram or gauge measurement."""


class _NoOpMeter:
    """Hands out no-op instruments so callers need no availability checks of their own."""

    def create_counter(self, name: str, unit: str = "", description: str = "") -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding counter."""
        return _NoOpInstrument()

    def create_up_down_counter(self, name: str, unit: str = "", description: str = "") -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding up-down counter."""
        return _NoOpInstrument()

    def create_histogram(self, name: str, unit: str = "", description: str = "", **_kwargs: Any) -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding histogram."""
        return _NoOpInstrument()

    def create_gauge(self, name: str, unit: str = "", description: str = "") -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding gauge."""
        return _NoOpInstrument()

    def create_observable_gauge(self, name: str, callbacks: Any = None, unit: str = "", description: str = "") -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding observable gauge; its callbacks are never invoked."""
        return _NoOpInstrument()

    def create_observable_counter(self, name: str, callbacks: Any = None, unit: str = "", description: str = "") -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding observable counter."""
        return _NoOpInstrument()

    def create_observable_up_down_counter(self, name: str, callbacks: Any = None, unit: str = "", description: str = "") -> _NoOpInstrument:  # noqa: ARG002
        """Return a discarding observable up-down counter."""
        return _NoOpInstrument()


class _NoOpMeterProvider:
    """Stand-in for the API's NoOpMeterProvider when the API itself is not installed."""

    def get_meter(self, name: str, version: str | None = None, schema_url: str | None = None) -> _NoOpMeter:  # noqa: ARG002
        """Return the no-op meter."""
        return _NoOpMeter()


class _NoOpSpan:
    """Accepts and discards every span operation, for installs without the `otel` extra.

    Doubles as its own context manager so ``with tracer.start_as_current_span(...) as span``
    reads identically with and without the extra.
    """

    def __enter__(self) -> _NoOpSpan:
        """Enter the span scope."""
        return self

    def __exit__(self, *_exc: Any) -> None:
        """Leave the span scope without suppressing anything."""

    def set_attribute(self, key: str, value: Any) -> None:
        """Discard an attribute."""

    def set_attributes(self, attributes: Any) -> None:
        """Discard a batch of attributes."""

    def set_status(self, status: Any, description: str | None = None) -> None:
        """Discard a status."""

    def record_exception(self, exception: BaseException, **_kwargs: Any) -> None:
        """Discard a recorded exception."""

    def add_event(self, name: str, attributes: Any = None, timestamp: int | None = None) -> None:
        """Discard an event."""

    def is_recording(self) -> bool:
        """Report that nothing is being recorded."""
        return False

    def end(self, end_time: int | None = None) -> None:
        """Discard the end of the span."""


class _NoOpTracer:
    """Hands out no-op spans so callers need no availability checks of their own."""

    def start_as_current_span(self, name: str, *_args: Any, **_kwargs: Any) -> _NoOpSpan:  # noqa: ARG002
        """Return a discarding span usable as a context manager."""
        return _NoOpSpan()

    def start_span(self, name: str, *_args: Any, **_kwargs: Any) -> _NoOpSpan:  # noqa: ARG002
        """Return a discarding span."""
        return _NoOpSpan()


class _NoOpTracerProvider:
    """Stand-in for the API's NoOpTracerProvider when the API itself is not installed."""

    def get_tracer(self, name: str, version: str | None = None, schema_url: str | None = None, attributes: Any = None) -> _NoOpTracer:  # noqa: ARG002
        """Return the no-op tracer."""
        return _NoOpTracer()


# Guards the module-level provider handles so concurrent service startup paths cannot install
# two providers, and so shutdown cannot observe a half-built one.
_lock = RLock()

# The provider handed back to callers: the SDK provider when telemetry is live, otherwise the
# API no-op provider. `_sdk_provider` is the subset that owns exporter resources to flush.
_provider: MeterProvider | None = None
_sdk_provider: SdkMeterProvider | None = None

# The tracing halves of the same pair. Both signals are installed by one `setup_telemetry`
# call, so `_provider` alone is the "already configured" flag for the whole bootstrap.
_tracer_provider: TracerProvider | None = None
_sdk_tracer_provider: SdkTracerProvider | None = None

# One event-loop sampler per loop. Keyed weakly so a finished loop's monitor is collectable
# and a service that runs several loops in sequence gets a fresh one for each.
_event_loop_monitors: WeakKeyDictionary[AbstractEventLoop, Task[None]] = WeakKeyDictionary()

# Bumped whenever the installed provider changes. Callers that cache instruments compare it
# so instruments built against an earlier (usually no-op) provider are rebuilt rather than
# silently dropping every measurement after a late setup_telemetry.
_generation = 0


def _noop_provider() -> MeterProvider:
    """Return the no-op provider: the API's when installed, otherwise the local stand-in."""
    if metrics is None:
        return cast("MeterProvider", _NoOpMeterProvider())
    return cast("MeterProvider", metrics.NoOpMeterProvider())


def _configured_endpoint() -> str | None:
    """Return the configured OTLP endpoint, preferring the metrics-specific override."""
    endpoint = getenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT") or getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    return endpoint.strip() or None


def _disabled_reason() -> str | None:
    """Return why metrics export is off, or None when it should be configured."""
    if (getenv("OTEL_METRICS_EXPORTER") or "").strip().lower() == _EXPORTER_DISABLED:
        return "OTEL_METRICS_EXPORTER=none"
    if _configured_endpoint() is None:
        return "OTEL_EXPORTER_OTLP_ENDPOINT is unset"
    return None


def _noop_tracer_provider() -> TracerProvider:
    """Return the no-op provider: the API's when installed, otherwise the local stand-in."""
    if trace is None:
        return cast("TracerProvider", _NoOpTracerProvider())
    return cast("TracerProvider", trace.NoOpTracerProvider())


def _configured_traces_endpoint() -> str | None:
    """Return the configured OTLP endpoint, preferring the traces-specific override."""
    endpoint = getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    return endpoint.strip() or None


def _traces_disabled_reason() -> str | None:
    """Return why span export is off, or None when it should be configured."""
    if (getenv("OTEL_TRACES_EXPORTER") or "").strip().lower() == _EXPORTER_DISABLED:
        return "OTEL_TRACES_EXPORTER=none"
    if _configured_traces_endpoint() is None:
        return "OTEL_EXPORTER_OTLP_ENDPOINT is unset"
    return None


def _default_the_sampler() -> None:
    """Default the sampler to ``parentbased_traceidratio`` at ratio 1.0.

    The SDK reads both variables itself; it just defaults to ``parentbased_always_on``, which
    a deployment cannot turn down without also changing the sampler name. Defaulting the ratio
    form here means a deployment only ever sets ``OTEL_TRACES_SAMPLER_ARG``. An explicit
    operator value wins, and a blank value counts as unset — a compose file that declares the
    variable without a value must not pin an empty sampler name.
    """
    if not (environ.get("OTEL_TRACES_SAMPLER") or "").strip():
        environ["OTEL_TRACES_SAMPLER"] = _DEFAULT_TRACES_SAMPLER
    if not (environ.get("OTEL_TRACES_SAMPLER_ARG") or "").strip():
        environ["OTEL_TRACES_SAMPLER_ARG"] = _DEFAULT_TRACES_SAMPLER_ARG


def _install_propagators() -> None:
    """Make W3C TraceContext plus baggage the global propagator.

    That is already the SDK default, but a GrooveMap consumer span joining an extractor's
    trace depends on it, so it is installed explicitly rather than inherited. ``OTEL_PROPAGATORS``
    still wins: the API loaded the operator's choice at import time and it is left alone.
    """
    if (environ.get("OTEL_PROPAGATORS") or "").strip():
        return
    from opentelemetry.baggage.propagation import W3CBaggagePropagator  # noqa: PLC0415
    from opentelemetry.propagate import set_global_textmap  # noqa: PLC0415
    from opentelemetry.propagators.composite import CompositePropagator  # noqa: PLC0415
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator  # noqa: PLC0415

    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))


def _resolve_service_version(service_name: str, service_version: str | None) -> str | None:
    """Resolve ``service.version``, falling back to the installed distribution's version.

    The fallback looks up ``service_name`` as a distribution name. A service whose deployment
    name differs from its distribution name should pass ``service_version`` explicitly; when
    neither resolves, ``service.version`` is simply omitted from the resource.
    """
    if service_version:
        return service_version
    try:
        return distribution_version(service_name)
    except PackageNotFoundError:
        logger.debug("No installed distribution named %r; omitting service.version", service_name)
        return None


def _build_resource(service_name: str, service_version: str | None) -> Resource:
    """Build the OTEL resource from the caller's defaults and the standard env vars."""
    from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, OTELResourceDetector, Resource  # noqa: PLC0415

    attributes: dict[str, str] = {SERVICE_NAME: service_name}
    resolved_version = _resolve_service_version(service_name, service_version)
    if resolved_version:
        attributes[SERVICE_VERSION] = resolved_version

    # Resource.create() runs the environment detector first and then lets the passed attributes
    # win. Deployments set service.name and the shared service.namespace /
    # deployment.environment.name through OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES, so the
    # environment must outrank the code defaults: merge the detector back in last.
    return Resource.create(attributes).merge(OTELResourceDetector().detect())


def _build_sdk_provider(service_name: str, service_version: str | None) -> SdkMeterProvider:
    """Build a MeterProvider that pushes OTLP/HTTP metrics on the configured interval."""
    # Imported lazily so `import common` — and any consumer without the `otel` extra — never
    # pulls in the SDK, the exporter, or their protobuf dependency.
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter  # noqa: PLC0415
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # noqa: PLC0415

    # Both the exporter (endpoint, headers, timeout, cumulative temporality) and the reader
    # (OTEL_METRIC_EXPORT_INTERVAL) read their own configuration from the standard env vars.
    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    return MeterProvider(resource=_build_resource(service_name, service_version), metric_readers=[reader])


def _build_sdk_tracer_provider(service_name: str, service_version: str | None) -> SdkTracerProvider:
    """Build a TracerProvider that batches OTLP/HTTP spans to the configured endpoint."""
    # Imported lazily for the same reason as the metrics builder: `import common` must not pull
    # in the SDK, the exporter, or their protobuf dependency.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider as _SdkTracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    _default_the_sampler()
    # The exporter reads endpoint, headers, and timeout, the batch processor its queue and
    # schedule, and the provider its sampler, all from the standard environment variables.
    provider = _SdkTracerProvider(resource=_build_resource(service_name, service_version))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def _install_meter_provider(service_name: str, service_version: str | None) -> MeterProvider:
    """Build and install the MeterProvider, degrading to the no-op provider on any failure."""
    global _sdk_provider

    reason = _disabled_reason()
    if reason is not None:
        logger.info("📊 OpenTelemetry metrics disabled (%s) — keeping the no-op MeterProvider", reason)
        return _noop_provider()

    try:
        _sdk_provider = _build_sdk_provider(service_name, service_version)
    except Exception:
        # A missing `otel` extra, an unreachable collector, or a malformed env var must
        # degrade the service to no telemetry, never take its startup down with it.
        logger.warning("⚠️ OpenTelemetry metrics bootstrap failed — falling back to the no-op MeterProvider", exc_info=True)
        return _noop_provider()

    if metrics is not None:
        metrics.set_meter_provider(_sdk_provider)
    _install_runtime_metrics(_sdk_provider)
    logger.info("📊 OpenTelemetry metrics configured for %r exporting to %s", service_name, _configured_endpoint())
    return _sdk_provider


def _install_runtime_metrics(provider: MeterProvider) -> bool:
    """Emit the process view through the system-metrics instrumentor. Never raises.

    Returns whether the instrumentation was applied. A failure here costs a service its CPU and
    memory series, which is never a reason to fail its startup.
    """
    try:
        from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor  # noqa: PLC0415
    except ImportError:
        logger.info("📊 Runtime metrics unavailable (install the 'otel' extra) — running without the process view")
        return False

    try:
        SystemMetricsInstrumentor(config=RUNTIME_METRICS_CONFIG).instrument(meter_provider=provider)
    except Exception:
        logger.warning("⚠️ Could not install the runtime metrics instrumentation — running without the process view", exc_info=True)
        return False
    return True


def _install_tracer_provider(service_name: str, service_version: str | None) -> TracerProvider:
    """Build and install the TracerProvider, degrading to the no-op provider on any failure."""
    global _sdk_tracer_provider

    reason = _traces_disabled_reason()
    if reason is not None:
        logger.info("🧭 OpenTelemetry tracing disabled (%s) — keeping the no-op TracerProvider", reason)
        return _noop_tracer_provider()

    try:
        _sdk_tracer_provider = _build_sdk_tracer_provider(service_name, service_version)
        _install_propagators()
    except Exception:
        logger.warning("⚠️ OpenTelemetry tracing bootstrap failed — falling back to the no-op TracerProvider", exc_info=True)
        _sdk_tracer_provider = None
        return _noop_tracer_provider()

    if trace is not None:
        trace.set_tracer_provider(_sdk_tracer_provider)
    logger.info("🧭 OpenTelemetry tracing configured for %r exporting to %s", service_name, _configured_traces_endpoint())
    return _sdk_tracer_provider


def setup_telemetry(service_name: str, *, service_version: str | None = None) -> MeterProvider:
    """Install the process-wide MeterProvider and TracerProvider, and return the meter one.

    Args:
        service_name: Default ``service.name``; ``OTEL_SERVICE_NAME`` overrides it.
        service_version: Default ``service.version``. When None the installed distribution
            named ``service_name`` is consulted, and the attribute is omitted if that fails.

    Returns:
        The installed MeterProvider — the SDK provider when metrics export is configured,
        otherwise the API no-op provider. :func:`tracer_provider` returns the tracing half.

    Both signals are built from one resource but configured independently, so either can be a
    no-op while the other exports. Calling this twice is idempotent: the second call returns the
    providers the first installed without rebuilding an exporter. It never raises; any failure
    degrades that signal to its no-op provider.
    """
    global _generation, _provider, _tracer_provider

    with _lock:
        if _provider is not None:
            return _provider

        # Before anything imports a contrib instrumentation: the contrib packages read this
        # once, the first time any of them initializes, and the process view is an
        # instrumentation too. Setting it only inside the HTTP helpers left them emitting the
        # pre-stable names whenever the process view had already initialized the cache.
        _opt_in_to_stable_http_semconv()
        _provider = _install_meter_provider(service_name, service_version)
        _tracer_provider = _install_tracer_provider(service_name, service_version)
        _generation += 1
        return _provider


async def _sample_event_loop_lag(interval_s: float, histogram: Any) -> None:
    """Record how much longer than ``interval_s`` each sleep actually took.

    The difference is the time the loop spent unable to run a ready callback, which is the
    definition of event-loop lag: a coroutine blocking the loop shows up here and nowhere else.
    """
    while True:
        started = perf_counter()
        await asyncio.sleep(interval_s)
        lag = perf_counter() - started - interval_s
        try:
            histogram.record(max(lag, 0.0))
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not record %s", EVENT_LOOP_LAG, exc_info=True)


def start_event_loop_monitor(interval_s: float = 1.0) -> Task[None] | None:
    """Sample this loop's scheduling delay into the event-loop lag histogram.

    Call it from the service's running loop once telemetry is configured. Returns the sampling
    task, or None when there is nothing to sample into: before :func:`setup_telemetry`, with
    metrics export off, without the ``otel`` extra, or outside a running loop. Idempotent per
    loop — a second call returns the task the first started — and never raises. The task is
    cancelled by :func:`shutdown_telemetry`.
    """
    with _lock:
        recording = _sdk_provider is not None

    if not recording:
        logger.debug("Event-loop monitoring skipped: metrics are not being exported")
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("⚠️ start_event_loop_monitor was called outside a running event loop — not sampling")
        return None

    with _lock:
        existing = _event_loop_monitors.get(loop)
        if existing is not None and not existing.done():
            return existing

        try:
            histogram = get_meter(INSTRUMENTATION_SCOPE).create_histogram(
                EVENT_LOOP_LAG,
                unit="s",
                description="Delay between when the event loop should have run a callback and when it did.",
                explicit_bucket_boundaries_advisory=list(EVENT_LOOP_LAG_BUCKETS),
            )
            monitor = loop.create_task(_sample_event_loop_lag(max(interval_s, 0.0), histogram), name="groovemap-event-loop-monitor")
        except Exception:
            logger.warning("⚠️ Could not start the event-loop monitor — running without the lag histogram", exc_info=True)
            return None

        _event_loop_monitors[loop] = monitor
        return monitor


def _stop_event_loop_monitors() -> None:
    """Cancel every running event-loop sampler. Never raises."""
    with _lock:
        monitors = list(_event_loop_monitors.items())
        _event_loop_monitors.clear()

    for loop, monitor in monitors:
        if monitor.done():
            continue
        try:
            if loop.is_closed():
                continue
            # The caller may be shutting down from another thread, and cancelling a task is
            # only safe from the loop that owns it.
            loop.call_soon_threadsafe(monitor.cancel)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not cancel the event-loop monitor", exc_info=True)


def tracer_provider() -> TracerProvider:
    """Return the installed TracerProvider, or the no-op provider before setup."""
    with _lock:
        provider = _tracer_provider
    if provider is not None:
        return provider
    return _noop_tracer_provider() if trace is None else cast("TracerProvider", trace.get_tracer_provider())


def get_meter(name: str, version: str | None = None) -> Meter:
    """Return a meter from the installed provider, or a no-op meter before setup."""
    with _lock:
        provider = _provider
    if provider is None:
        provider = _noop_provider() if metrics is None else cast("MeterProvider", metrics.get_meter_provider())
    return provider.get_meter(name, version)


def _opt_in_to_stable_http_semconv() -> None:
    """Default the HTTP instrumentations to the stable metric names.

    The contrib packages read this once, the first time ANY instrumentation initializes — the
    system-metrics one included — so ``setup_telemetry`` sets it before installing anything and
    each ``instrument_*`` helper sets it again for services that call them on their own. An
    explicit operator value wins; a blank value counts as unset, because a compose file that
    declares the variable without a value would otherwise silently pin the pre-stable names.
    """
    if not (environ.get(_SEMCONV_STABILITY_OPT_IN) or "").strip():
        environ[_SEMCONV_STABILITY_OPT_IN] = _STABLE_HTTP_SEMCONV


def _active_provider() -> MeterProvider:
    """Return the provider third-party instrumentation should report through."""
    with _lock:
        provider = _provider
    if provider is not None:
        return provider
    return _noop_provider() if metrics is None else cast("MeterProvider", metrics.get_meter_provider())


def provider_generation() -> int:
    """Return a counter that changes whenever the installed provider is replaced.

    Instrument caches compare this so a cache built before ``setup_telemetry`` — against the
    no-op provider — is rebuilt instead of silently discarding every later measurement.
    """
    with _lock:
        return _generation


def shutdown_telemetry(timeout_s: float = 5.0) -> None:
    """Force-flush and shut down both installed providers so the last export lands.

    Spans are flushed before metrics: the batch span processor holds finished spans that
    describe the very work whose metrics the reader is about to push, and a one-shot process
    that exits between the two flushes should lose the cheaper signal, not the trace. Any
    event-loop monitor is cancelled first, so nothing is still recording while providers close.

    One-shot processes must call this before exiting; the periodic reader and the batch span
    processor would otherwise drop everything recorded since their last push. Safe to call
    without a prior :func:`setup_telemetry`, safe to call twice, and never raises.
    """
    global _generation, _provider, _sdk_provider, _sdk_tracer_provider, _tracer_provider

    _stop_event_loop_monitors()

    with _lock:
        provider = _sdk_provider
        tracing = _sdk_tracer_provider
        _sdk_provider = None
        _sdk_tracer_provider = None
        _provider = None
        _tracer_provider = None
        _generation += 1

    timeout_millis = max(float(timeout_s), 0.0) * 1000.0

    if tracing is not None:
        try:
            tracing.force_flush(timeout_millis=int(timeout_millis))
        except Exception:
            logger.warning("⚠️ OpenTelemetry tracing force-flush failed during shutdown", exc_info=True)
        try:
            # The SDK TracerProvider takes no timeout: it delegates to its span processors,
            # which carry their own.
            tracing.shutdown()
        except Exception:
            logger.warning("⚠️ OpenTelemetry tracer provider shutdown failed", exc_info=True)

    if provider is None:
        return

    try:
        provider.force_flush(timeout_millis=timeout_millis)
    except Exception:
        logger.warning("⚠️ OpenTelemetry metrics force-flush failed during shutdown", exc_info=True)
    try:
        provider.shutdown(timeout_millis=timeout_millis)
    except Exception:
        logger.warning("⚠️ OpenTelemetry metrics provider shutdown failed", exc_info=True)


def instrument_fastapi_app(app: Any, *, excluded_urls: str = DEFAULT_EXCLUDED_URLS) -> bool:
    """Emit `http.server.*` metrics and spans for a FastAPI app. Returns whether it applied.

    Routes are reported by their templated path (``/artists/{artist_id}``), never the raw one,
    so `http.route` and the span name stay low-cardinality. ``excluded_urls`` is the contrib
    comma-separated pattern list and defaults to the probe endpoints, which would otherwise
    dominate the request histogram. Spans are recorded only once a TracerProvider is live.

    Safe to call without the ``otel-http`` extra: it logs once and returns False. Safe to call
    before ``setup_telemetry``, in which case the app reports through the no-op provider.
    """
    _opt_in_to_stable_http_semconv()
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    except ImportError:
        logger.info("📊 FastAPI instrumentation unavailable (install the 'otel-http' extra) — serving without HTTP metrics")
        return False

    try:
        FastAPIInstrumentor.instrument_app(
            app,
            meter_provider=_active_provider(),
            tracer_provider=tracer_provider(),
            excluded_urls=excluded_urls,
        )
    except Exception:
        logger.warning("⚠️ Could not instrument the FastAPI app — serving without HTTP metrics", exc_info=True)
        return False
    return True


def instrument_httpx(client: Any = None) -> bool:
    """Emit `http.client.*` metrics and spans for httpx. Returns whether it was applied.

    With a client, only that client is instrumented; with None, every httpx client created in
    this process is. Metrics carry `server.address` and the response status code, never the
    full URL. Once a TracerProvider is live the instrumentation also records client spans and
    writes `traceparent` onto every outbound request.

    Safe to call without the ``otel-http`` extra: it logs once and returns False.
    """
    _opt_in_to_stable_http_semconv()
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415
    except ImportError:
        logger.info("📊 httpx instrumentation unavailable (install the 'otel-http' extra) — calling peers without HTTP metrics")
        return False

    provider = _active_provider()
    tracing = tracer_provider()
    try:
        if client is None:
            HTTPXClientInstrumentor().instrument(meter_provider=provider, tracer_provider=tracing)
        else:
            HTTPXClientInstrumentor.instrument_client(client, meter_provider=provider, tracer_provider=tracing)
    except Exception:
        logger.warning("⚠️ Could not instrument httpx — calling peers without HTTP metrics", exc_info=True)
        return False
    return True
