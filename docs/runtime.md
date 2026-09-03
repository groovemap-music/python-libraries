# `groovemap-runtime` contract

`groovemap-runtime` supplies dependency-light runtime primitives through the `common` import
package. Its supported interpreter line is Python 3.14, pinned to Python 3.14.5 in GrooveMap CI.
The distribution does not install a console command.

## Stable imports

The names in `common.__all__` are the stable public import surface. Import from `common`, not
from an implementation module, when a name appears here.

| Capability | Stable names |
| --- | --- |
| Configuration and logging | `neo4j_security_kwargs`, `parse_postgres_host_port`, `setup_logging` |
| Data and diagnostics | `normalize_record`, `describe_exception` |
| Generic resilience | `AsyncResilientConnection`, `CircuitBreaker`, `CircuitBreakerConfig`, `CircuitOpenError`, `CircuitState`, `ConnectionEstablishmentError`, `DatabaseUnavailableError`, `ExponentialBackoff`, `ResilientConnection`, `async_resilient_connection`, `resilient_connection` |
| Health and outage control | `HealthServer`, `OutageBackoff` |
| Neo4j | `AsyncResilientNeo4jDriver`, `ResilientNeo4jDriver`, `with_async_neo4j_retry`, `with_neo4j_retry` |
| PostgreSQL | `AsyncPostgreSQLPool`, `AsyncResilientPostgreSQL`, `ResilientPostgreSQLPool` |
| Query diagnostics | `execute_sql`, `is_db_profiling`, `is_debug`, `log_cypher_query`, `log_sql_query` |
| RabbitMQ | `AsyncResilientRabbitMQ`, `ResilientRabbitMQConnection`, `process_message_with_retry` |
| Telemetry | `setup_telemetry`, `shutdown_telemetry`, `get_meter` |

These imports are lazy. Importing `common` does not load optional database, broker, metrics, or
OpenTelemetry SDK clients until the corresponding capability is requested. Other names in `common.*` modules are
implementation details or transitional service helpers and do not carry compatibility promises.
In particular, a leading-underscore helper is private even if an existing GrooveMap service still
imports it during migration.

## Optional capabilities

| Extra | Enables | Required for |
| --- | --- | --- |
| `metrics` | `prometheus-client` | Serving `/metrics` from `HealthServer` |
| `otel` | OpenTelemetry SDK and the OTLP HTTP/protobuf exporter | Exporting metrics from `setup_telemetry` |
| `otel-http` | FastAPI and httpx OpenTelemetry instrumentation | Instrumenting inbound and outbound HTTP |
| `neo4j` | Neo4j driver | Neo4j connection and retry helpers |
| `postgres` | Psycopg | PostgreSQL pools and query execution |
| `rabbitmq` | aio-pika and pika | Async and synchronous broker resilience |
| `all` | Every optional dependency | Development and full validation only |

Consumers should install only the extras they use, for example
`groovemap-runtime[postgres,otel]`. The base package includes structured logging, record
normalization, and the OpenTelemetry **API** — the API alone is a small pure-Python dependency
and is what supplies working no-op instruments when the `otel` extra is absent. The SDK, the
exporter, and their protobuf dependency arrive only with the extra.

## Configuration boundary

The stable configuration functions deliberately remain small:

- `parse_postgres_host_port(value, default_port=5432)` parses host, embedded-port, and IPv6
  forms without reading application settings.
- `neo4j_security_kwargs()` maps `NEO4J_TLS_ENABLED` and `NEO4J_TLS_VERIFY` to Neo4j driver
  security options. Verification defaults to enabled whenever TLS is enabled.
- `setup_logging(service_name, level=None, log_file=None)` configures JSON logs and binds
  `service` plus `ENVIRONMENT` context. `level` takes precedence over `LOG_LEVEL`; an invalid
  value falls back to `INFO` and emits a warning.

Consumers own their settings models, required-variable validation, secret acquisition,
deployment defaults, and process lifecycle. The shared
[organization emoji vocabulary](https://github.com/groovemap-music/.github/blob/main/docs/emoji-guide.md)
defines log-message markers; applications should link to that canonical guide instead of a
repository-local `docs/emoji-guide.md` path.

## Health and metrics boundary

`HealthServer(port, health_func, metrics_enabled=False)` serves:

- `GET /health`: `200` only when `health_func()` returns `{"status": "healthy"}`; otherwise
  `503`. Exceptions are converted to an unhealthy response.
- `GET /metrics`: `200` only when `metrics_enabled=True`; otherwise `404`. Enabling this route
  requires the `metrics` extra.
- every other route: `404`.

The consumer owns metric registration, port selection, startup, and shutdown. It must call
`start_background()` and `stop()` as part of its own lifecycle. The library does not start a
server during import and does not create application-specific metrics.

## Telemetry boundary

`setup_telemetry(service_name, *, service_version=None)` installs one process-wide
`MeterProvider` and returns it. `shutdown_telemetry(timeout_s=5.0)` force-flushes and shuts it
down. `get_meter(name, version=None)` returns a meter for registering instruments. The library
configures transport and resource only; every metric is registered by its consumer.

Metrics are pushed over OTLP HTTP/protobuf. The runtime never exposes a Prometheus scrape
endpoint for OpenTelemetry metrics, and reads only standard OpenTelemetry environment
variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector base URL, for example `http://otel-collector:4318` | unset, which disables export |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | Metrics-only endpoint override | falls back to `OTEL_EXPORTER_OTLP_ENDPOINT` |
| `OTEL_METRICS_EXPORTER` | `otlp` or `none` | `otlp` |
| `OTEL_METRIC_EXPORT_INTERVAL` | Push interval in milliseconds | SDK default |
| `OTEL_SERVICE_NAME` | `service.name`, overriding the `service_name` argument | the `service_name` argument |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes, for example `service.namespace=groovemap,deployment.environment.name=dev` | empty |

Behavior the consumer can rely on:

- Telemetry never fails startup. A missing endpoint, `OTEL_METRICS_EXPORTER=none`, an absent
  `otel` extra, or a malformed configuration all fall back to the API no-op `MeterProvider`
  and log one line instead of raising.
- `setup_telemetry` is idempotent. A second call returns the provider the first installed
  without building a second exporter.
- Environment wins over code. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` override the
  `service.name` and `service.version` the bootstrap derives from its arguments.
- `service_version` defaults to the version of an installed distribution named `service_name`;
  a service whose deployment name differs from its distribution name should pass it
  explicitly, and the attribute is omitted when neither resolves.
- Export temporality is cumulative, so a Prometheus-backed collector reads counters correctly.
- One-shot processes must call `shutdown_telemetry` before exiting or the periodic reader drops
  everything recorded since its last push. It is safe without a prior `setup_telemetry`, safe
  to call twice, and never raises.

Only metrics are configured today. Tracing is a deliberate non-goal of this boundary and would
be added as a sibling provider built from the same resource.

### Metrics the wrappers emit for free

Once a service calls `setup_telemetry`, the resilience wrappers it already uses report these
without any further code. Instruments are built lazily, so a service that never configures an
endpoint pays only for one no-op instrument per metric.

| Metric | Instrument | Attributes | Emitted by |
| --- | --- | --- | --- |
| `db.client.operation.duration` | histogram, seconds | `db.system.name`, `db.operation.name`, `error.type` on failure | PostgreSQL pools, Neo4j drivers, `execute_sql` |
| `groovemap.pipeline.reconnects` | counter | `system` | every resilient connection wrapper, on each reconnect |
| `groovemap.pipeline.circuit_breaker.state` | observable gauge | `system` | every live `CircuitBreaker`; 0 closed, 1 half-open, 2 open |
| `messaging.client.consumed.messages` | counter | `messaging.system`, `messaging.destination.name`, `messaging.operation.name`, `error.type` on failure | `process_message_with_retry` |
| `messaging.client.operation.duration` | histogram, seconds | same as the message counters | `process_message_with_retry` |

`db.operation.name` is a short verb (`session`, `execute`). SQL and Cypher text, record ids, and
hostnames are never attribute values. `CircuitBreakerConfig` takes an optional `system` label for
the gauge; it defaults to the lowercased breaker `name`, so name a breaker after its backing
system or set `system` explicitly. `ResilientConnection` and `AsyncResilientConnection` take the
same optional `system` keyword for the reconnect counter.

## Compatibility boundary

See the repository [compatibility and release policy](compatibility-and-releases.md). Public root
imports and their documented behavior are compatibility-controlled; internal modules, private
helpers, deployment defaults, and consumer behavior are not.
