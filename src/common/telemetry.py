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
from os import getenv
from threading import RLock
from typing import TYPE_CHECKING

from opentelemetry import metrics


if TYPE_CHECKING:  # pragma: no cover
    from opentelemetry.metrics import Meter, MeterProvider
    from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
    from opentelemetry.sdk.resources import Resource


logger = logging.getLogger(__name__)

# The single value of OTEL_METRICS_EXPORTER that means "collect nothing".
_EXPORTER_DISABLED = "none"

# Guards the module-level provider handles so concurrent service startup paths cannot install
# two providers, and so shutdown cannot observe a half-built one.
_lock = RLock()

# The provider handed back to callers: the SDK provider when telemetry is live, otherwise the
# API no-op provider. `_sdk_provider` is the subset that owns exporter resources to flush.
_provider: MeterProvider | None = None
_sdk_provider: SdkMeterProvider | None = None


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
    global _provider, _sdk_provider

    with _lock:
        if _provider is not None:
            return _provider

        reason = _disabled_reason()
        if reason is not None:
            logger.info("📊 OpenTelemetry metrics disabled (%s) — keeping the no-op MeterProvider", reason)
            _provider = metrics.NoOpMeterProvider()
            return _provider

        try:
            _sdk_provider = _build_sdk_provider(service_name, service_version)
        except Exception:
            # A missing `otel` extra, an unreachable collector, or a malformed env var must
            # degrade the service to no telemetry, never take its startup down with it.
            logger.warning("⚠️ OpenTelemetry metrics bootstrap failed — falling back to the no-op MeterProvider", exc_info=True)
            _provider = metrics.NoOpMeterProvider()
            return _provider

        metrics.set_meter_provider(_sdk_provider)
        _provider = _sdk_provider
        logger.info("📊 OpenTelemetry metrics configured for %r exporting to %s", service_name, _configured_endpoint())
        return _provider


def get_meter(name: str, version: str | None = None) -> Meter:
    """Return a meter from the installed provider, or a no-op meter before setup."""
    with _lock:
        provider = _provider
    if provider is None:
        provider = metrics.get_meter_provider()
    return provider.get_meter(name, version)


def shutdown_telemetry(timeout_s: float = 5.0) -> None:
    """Force-flush and shut down the installed provider so the last export lands.

    One-shot processes must call this before exiting; the periodic reader would otherwise drop
    everything recorded since its last push. Safe to call without a prior
    :func:`setup_telemetry`, safe to call twice, and never raises.
    """
    global _provider, _sdk_provider

    with _lock:
        provider = _sdk_provider
        _sdk_provider = None
        _provider = None

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
