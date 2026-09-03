"""OpenTelemetry metrics bootstrap shared by every GrooveMap service.

One call configures the OpenTelemetry SDK from the standard environment variables and one
call flushes it, so no service has to reimplement provider, reader, and exporter wiring.

Transport is OTLP over HTTP/protobuf; there is no gRPC dependency and no Prometheus scrape
endpoint. Configuration is read from the standard OpenTelemetry environment variables only:

- ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the metrics-specific
  ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT``) selects the collector. Unset means telemetry off.
- ``OTEL_METRICS_EXPORTER`` accepts ``otlp`` (default) or ``none`` to force telemetry off.
- ``OTEL_METRIC_EXPORT_INTERVAL`` sets the push interval in milliseconds (SDK default 60000).
- ``OTEL_SERVICE_NAME`` and ``OTEL_RESOURCE_ATTRIBUTES`` override the resource attributes the
  bootstrap derives from its arguments.

Telemetry never fails startup: a missing ``otel`` extra, a missing endpoint, or a broken SDK
configuration all fall back to the API no-op ``MeterProvider`` and log instead of raising.

Only metrics are configured here. Tracing would be added as a sibling ``TracerProvider`` built
from the same resource in :func:`_build_resource`; nothing in this module assumes metrics are
the only signal.
"""

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from os import environ, getenv
from threading import RLock
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics


if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.metrics import Meter, MeterProvider
    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
    from opentelemetry.sdk.resources import Resource


logger = logging.getLogger(__name__)

# The single value of OTEL_METRICS_EXPORTER that means "collect nothing".
_EXPORTER_DISABLED = "none"

# Probe endpoints are excluded by default: they are polled constantly and would otherwise
# dominate the request histogram without telling an operator anything.
DEFAULT_EXCLUDED_URLS = "health,ready,metrics"

# The contrib HTTP instrumentations still default to the pre-stable metric names
# (http.server.duration in milliseconds). GrooveMap dashboards are written against the stable
# semantic conventions, so opt in unless an operator has already chosen a value. This is a
# standard OpenTelemetry environment variable, not a GrooveMap-specific one.
_SEMCONV_STABILITY_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
_STABLE_HTTP_SEMCONV = "http"

# Guards the module-level provider handles so concurrent service startup paths cannot install
# two providers, and so shutdown cannot observe a half-built one.
_lock = RLock()

# The provider handed back to callers: the SDK provider when telemetry is live, otherwise the
# API no-op provider. `_sdk_provider` is the subset that owns exporter resources to flush.
_provider: MeterProvider | None = None
_sdk_provider: SdkMeterProvider | None = None

# Bumped whenever the installed provider changes. Callers that cache instruments compare it
# so instruments built against an earlier (usually no-op) provider are rebuilt rather than
# silently dropping every measurement after a late setup_telemetry.
_generation = 0


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


def setup_telemetry(service_name: str, *, service_version: str | None = None) -> MeterProvider:
    """Install the process-wide MeterProvider and return it.

    Args:
        service_name: Default ``service.name``; ``OTEL_SERVICE_NAME`` overrides it.
        service_version: Default ``service.version``. When None the installed distribution
            named ``service_name`` is consulted, and the attribute is omitted if that fails.

    Returns:
        The installed provider — the SDK provider when metrics export is configured, otherwise
        the API no-op provider.

    Calling this twice is idempotent: the second call returns the provider the first installed
    without rebuilding an exporter. It never raises; any failure degrades to the no-op provider.
    """
    global _generation, _provider, _sdk_provider

    with _lock:
        if _provider is not None:
            return _provider

        reason = _disabled_reason()
        if reason is not None:
            logger.info("📊 OpenTelemetry metrics disabled (%s) — keeping the no-op MeterProvider", reason)
            _provider = metrics.NoOpMeterProvider()
            _generation += 1
            return _provider

        try:
            _sdk_provider = _build_sdk_provider(service_name, service_version)
        except Exception:
            # A missing `otel` extra, an unreachable collector, or a malformed env var must
            # degrade the service to no telemetry, never take its startup down with it.
            logger.warning("⚠️ OpenTelemetry metrics bootstrap failed — falling back to the no-op MeterProvider", exc_info=True)
            _provider = metrics.NoOpMeterProvider()
            _generation += 1
            return _provider

        metrics.set_meter_provider(_sdk_provider)
        _provider = _sdk_provider
        _generation += 1
        logger.info("📊 OpenTelemetry metrics configured for %r exporting to %s", service_name, _configured_endpoint())
        return _provider


def get_meter(name: str, version: str | None = None) -> Meter:
    """Return a meter from the installed provider, or a no-op meter before setup."""
    with _lock:
        provider = _provider
    if provider is None:
        provider = metrics.get_meter_provider()
    return provider.get_meter(name, version)


def _opt_in_to_stable_http_semconv() -> None:
    """Default the HTTP instrumentations to the stable metric names.

    The contrib packages read this once, the first time an instrumentation initializes, so it
    has to be set before the first ``instrument_*`` call. An explicit operator value wins; a
    blank value counts as unset, because a compose file that declares the variable without a
    value would otherwise silently pin the pre-stable metric names.
    """
    if not (environ.get(_SEMCONV_STABILITY_OPT_IN) or "").strip():
        environ[_SEMCONV_STABILITY_OPT_IN] = _STABLE_HTTP_SEMCONV


def _active_provider() -> MeterProvider:
    """Return the provider third-party instrumentation should report through."""
    with _lock:
        provider = _provider
    return provider if provider is not None else metrics.get_meter_provider()


def provider_generation() -> int:
    """Return a counter that changes whenever the installed provider is replaced.

    Instrument caches compare this so a cache built before ``setup_telemetry`` — against the
    no-op provider — is rebuilt instead of silently discarding every later measurement.
    """
    with _lock:
        return _generation


def shutdown_telemetry(timeout_s: float = 5.0) -> None:
    """Force-flush and shut down the installed provider so the last export lands.

    One-shot processes must call this before exiting; the periodic reader would otherwise drop
    everything recorded since its last push. Safe to call without a prior
    :func:`setup_telemetry`, safe to call twice, and never raises.
    """
    global _generation, _provider, _sdk_provider

    with _lock:
        provider = _sdk_provider
        _sdk_provider = None
        _provider = None
        _generation += 1

    if provider is None:
        return

    timeout_millis = max(float(timeout_s), 0.0) * 1000.0
    try:
        provider.force_flush(timeout_millis=timeout_millis)
    except Exception:
        logger.warning("⚠️ OpenTelemetry metrics force-flush failed during shutdown", exc_info=True)
    try:
        provider.shutdown(timeout_millis=timeout_millis)
    except Exception:
        logger.warning("⚠️ OpenTelemetry metrics provider shutdown failed", exc_info=True)


def instrument_fastapi_app(app: Any, *, excluded_urls: str = DEFAULT_EXCLUDED_URLS) -> bool:
    """Emit `http.server.*` metrics for a FastAPI app. Returns whether it was instrumented.

    Routes are reported by their templated path (``/artists/{artist_id}``), never the raw one,
    so `http.route` stays low-cardinality. ``excluded_urls`` is the contrib comma-separated
    pattern list and defaults to the probe endpoints, which would otherwise dominate the
    request histogram.

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
        FastAPIInstrumentor.instrument_app(app, meter_provider=_active_provider(), excluded_urls=excluded_urls)
    except Exception:
        logger.warning("⚠️ Could not instrument the FastAPI app — serving without HTTP metrics", exc_info=True)
        return False
    return True


def instrument_httpx(client: Any = None) -> bool:
    """Emit `http.client.*` metrics for httpx. Returns whether instrumentation was applied.

    With a client, only that client is instrumented; with None, every httpx client created in
    this process is. Metrics carry `server.address` and the response status code, never the
    full URL.

    Safe to call without the ``otel-http`` extra: it logs once and returns False.
    """
    _opt_in_to_stable_http_semconv()
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor  # noqa: PLC0415
    except ImportError:
        logger.info("📊 httpx instrumentation unavailable (install the 'otel-http' extra) — calling peers without HTTP metrics")
        return False

    provider = _active_provider()
    try:
        if client is None:
            HTTPXClientInstrumentor().instrument(meter_provider=provider)
        else:
            HTTPXClientInstrumentor.instrument_client(client, meter_provider=provider)
    except Exception:
        logger.warning("⚠️ Could not instrument httpx — calling peers without HTTP metrics", exc_info=True)
        return False
    return True
