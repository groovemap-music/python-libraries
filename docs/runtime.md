# `groovemap-runtime` contract

`groovemap-runtime` supplies dependency-light runtime primitives through the `common` import
package. Its supported interpreter line is Python 3.14, pinned to Python 3.14.7 in GrooveMap CI.
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
| Media taxonomy | `map_discogs_formats`, `map_musicbrainz_release`, `legacy_format_names_to_media`, `families_of`, `family_ids`, `medium_ids`, `medium_label` |
| Neo4j | `AsyncResilientNeo4jDriver`, `ResilientNeo4jDriver`, `with_async_neo4j_retry`, `with_neo4j_retry` |
| PostgreSQL | `AsyncPostgreSQLPool`, `AsyncResilientPostgreSQL`, `ResilientPostgreSQLPool` |
| Query diagnostics | `execute_sql`, `is_db_profiling`, `is_debug`, `log_cypher_query`, `log_sql_query` |
| RabbitMQ | `AsyncResilientRabbitMQ`, `ResilientRabbitMQConnection`, `process_message_with_retry` |
| Telemetry | `setup_telemetry`, `shutdown_telemetry`, `get_meter`, `instrument_fastapi_app`, `instrument_httpx` |

These imports are lazy. Importing `common` does not load optional database, broker, metrics, or
OpenTelemetry clients until the corresponding capability is requested. Other names in `common.*`
modules are implementation details or transitional service helpers and do not carry
compatibility promises.
In particular, a leading-underscore helper is private even if an existing GrooveMap service still
imports it during migration.

## Optional capabilities

| Extra | Enables | Required for |
| --- | --- | --- |
| `metrics` | `prometheus-client` | Serving `/metrics` from `HealthServer` |
| `neo4j` | Neo4j driver | Neo4j connection and retry helpers |
| `otel` | OpenTelemetry API, SDK, and the OTLP HTTP/protobuf exporter | Recording and exporting metrics |
| `otel-http` | FastAPI and httpx OpenTelemetry instrumentation | Instrumenting inbound and outbound HTTP |
| `postgres` | Psycopg | PostgreSQL pools and query execution |
| `rabbitmq` | aio-pika and pika | Async and synchronous broker resilience |
| `all` | Every optional dependency | Development and full validation only |

Consumers should install only the extras they use, for example
`groovemap-runtime[postgres,otel]`. The base package includes structured logging and record
normalization only. No OpenTelemetry package is a base dependency: `common` imports and runs
with none installed, and every instrument is a local no-op until the `otel` extra is present.
That keeps an existing consumer working against its current lockfile, which pins this library's
base dependencies and would otherwise be missing a newly added one at runtime.

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
| `OTEL_SDK_DISABLED` | `true` makes the SDK itself a no-op, so nothing is recorded even when an endpoint is set | `false` |
| `OTEL_METRIC_EXPORT_INTERVAL` | Push interval in milliseconds | SDK default |
| `OTEL_SERVICE_NAME` | `service.name`, overriding the `service_name` argument | the `service_name` argument |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes, for example `service.namespace=groovemap,deployment.environment.name=dev` | empty |

Behavior the consumer can rely on:

- Telemetry never fails startup. A missing endpoint, `OTEL_METRICS_EXPORTER=none`, an absent
  `otel` extra, or a malformed configuration all fall back to a no-op `MeterProvider` and log
  one line instead of raising. Without the extra the fallback is a local stand-in, so nothing
  in `common` requires an `opentelemetry` package to be installed at all.
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

### HTTP instrumentation

`instrument_fastapi_app(app, *, excluded_urls="health,ready,metrics")` emits
`http.server.request.duration` with `http.route` and `http.response.status_code`. The route is
the templated path (`/artists/{artist_id}`), never the raw one, so the attribute stays
low-cardinality; `/health` and `/ready` are excluded by default because probes would otherwise
dominate the histogram.

`instrument_httpx(client=None)` emits `http.client.request.duration` with `server.address` and
the response status code. Pass a client to instrument only that client, or nothing to
instrument every httpx client in the process.

Both need the `otel-http` extra. Without it each returns `False` after logging one line, so a
service that has not installed the extra still starts and serves normally. Both return `True`
when instrumentation was applied. Call them after `setup_telemetry` so they bind to the
configured provider.

Both default `OTEL_SEMCONV_STABILITY_OPT_IN` to `http` before the first instrumentation
initializes. The contrib packages otherwise emit the pre-stable names (`http.server.duration`
in milliseconds). An operator value wins; a blank value counts as unset.

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

## Media taxonomy boundary

The canonical media vocabulary that [ADR 0007 in the `design`
repository](https://github.com/groovemap-music/design/blob/main/docs/adr/0007-canonical-media-taxonomy.md)
makes authoritative is vendored into this distribution as package data, and `common.media` is
the single Python mapper every GrooveMap service shares. The two Rust producers carry their own
mappers; all three are held to the design repository's conformance fixtures, which this
repository re-runs in `tests/test_media.py`, so the same input yields the same block everywhere.

- `map_discogs_formats(formats)` maps a Discogs `formats` list. Both provider shapes are
  accepted: the normalized releases-event shape, whose descriptions arrive as
  `{"description": [...]}` or as a bare string, and the Discogs API shape, whose `descriptions`
  is already a flat list.
- `map_musicbrainz_release(release)` maps a MusicBrainz release: its `media` entries (`format`,
  `position`, `track_count`), `status`, `packaging`, and `release_group` primary and secondary
  types.
- `legacy_format_names_to_media(names)` derives a best-effort block from a flat list of raw
  Discogs format names such as `["Vinyl", "LP", "Album"]`, for events and stored records that
  predate the canonical block. A name the vocabulary knows as a format name opens a format
  entry; every other name is a description of the entry that precedes it, and names before the
  first format name attach to it. Prefer `map_discogs_formats` whenever the raw structure
  survives.
- `families_of(media_block)` returns the sorted, unique family ids a block covers.
- `family_ids()` and `medium_ids()` return the closed id sets, in vocabulary order, for
  validating a caller-supplied media filter. `medium_label(medium_id)` returns a medium's
  human-readable label and raises `KeyError` for an unknown id.

Every mapper returns a plain JSON-ready `dict` of `dict`, `list`, `str`, number, and `None`, so
the block can be attached to an event or written to a JSONB column without a serializer. The
mappers read only the standard library, so they add nothing to the base install. Malformed
input is skipped rather than raised on, and any raw value the vocabulary does not recognize is
preserved under `unmapped` rather than dropped.

Ordering is fixed by the ADR because independent implementations must agree byte for byte:
`items` follow source order, `source.descriptions` are kept as received, and every other list
is sorted and de-duplicated. The first value wins for scalar release facts. Every field is
always present, holding `null` or an empty list when unknown.

`common.agent_tools.schemas` carries the same shape as `MediaBlock`, `MediaItem`, and
`MediaSource` typed dictionaries for consumers that want the block statically checked.

## Compatibility boundary

See the repository [compatibility and release policy](compatibility-and-releases.md). Public root
imports and their documented behavior are compatibility-controlled; internal modules, private
helpers, deployment defaults, and consumer behavior are not.
