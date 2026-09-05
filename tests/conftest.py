"""Shared pytest fixtures.

The telemetry suites assert on what an in-memory OpenTelemetry provider recorded, so they must
not inherit the ambient OpenTelemetry configuration. `OTEL_SDK_DISABLED=true` in particular
turns every SDK meter into a no-op, which makes those assertions fail with an empty collection
and no error anywhere. Continuous-integration runners set these variables to keep their own
instrumentation quiet, so an unisolated suite passes on a developer's machine and fails there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator


# Every standard OpenTelemetry variable that changes what the SDK records or exports. The
# tracing bootstrap also *writes* OTEL_TRACES_SAMPLER and OTEL_TRACES_SAMPLER_ARG when they are
# unset, so leaving them in place would let one test's sampler decide another test's spans.
OTEL_ENVIRONMENT = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_METRICS_EXEMPLAR_FILTER",
    "OTEL_METRICS_EXPORTER",
    "OTEL_METRIC_EXPORT_INTERVAL",
    "OTEL_PROPAGATORS",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "OTEL_TRACES_EXPORTER",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
)


@pytest.fixture(autouse=True)
def isolated_otel_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test against a known-empty OpenTelemetry configuration."""
    for name in OTEL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    yield
