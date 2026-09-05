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
| Media taxonomy | `map_discogs_formats`, `map_musicbrainz_release`, `legacy_format_names_to_media`, `flatten_descriptions`, `families_of`, `family_ids`, `medium_ids`, `medium_label` |
| Neo4j | `AsyncResilientNeo4jDriver`, `ResilientNeo4jDriver`, `with_async_neo4j_retry`, `with_neo4j_retry` |
| PostgreSQL | `AsyncPostgreSQLPool`, `AsyncResilientPostgreSQL`, `ResilientPostgreSQLPool` |
| Query diagnostics | `execute_sql`, `is_db_profiling`, `is_debug`, `log_cypher_query`, `log_sql_query` |
| RabbitMQ | `AsyncResilientRabbitMQ`, `ResilientRabbitMQConnection`, `process_message_with_retry` |
| Telemetry | `setup_telemetry`, `shutdown_telemetry`, `get_meter`, `instrument_fastapi_app`, `instrument_httpx`, `start_event_loop_monitor` |
| Tracing | `get_tracer`, `inject_headers`, `extract_context`, `flush_span` |

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
| `otel` | OpenTelemetry API, SDK, the OTLP HTTP/protobuf exporter, and the system-metrics instrumentation | Recording and exporting metrics and spans, and the process view |
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
`MeterProvider` and one process-wide `TracerProvider`, both built from the same resource, and
returns the meter provider. `shutdown_telemetry(timeout_s=5.0)` force-flushes and shuts both
down. `get_meter(name, version=None)` returns a meter for registering instruments and
`get_tracer(name, version=None)` returns a tracer for opening spans. The library configures
transport, resource, sampler, and propagator only; every metric and every domain span is
registered by its consumer.

Both signals are pushed over OTLP HTTP/protobuf. The runtime never exposes a Prometheus scrape
endpoint for OpenTelemetry metrics, and reads only standard OpenTelemetry environment
variables:

| Variable | Meaning | Default |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector base URL, for example `http://otel-collector:4318` | unset, which disables export |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | Metrics-only endpoint override | falls back to `OTEL_EXPORTER_OTLP_ENDPOINT` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Traces-only endpoint override | falls back to `OTEL_EXPORTER_OTLP_ENDPOINT` |
| `OTEL_METRICS_EXPORTER` | `otlp` or `none` | `otlp` |
| `OTEL_TRACES_EXPORTER` | `otlp` or `none` | `otlp` |
| `OTEL_TRACES_SAMPLER` | Sampler name the SDK understands | `parentbased_traceidratio` |
| `OTEL_TRACES_SAMPLER_ARG` | Sampling ratio for the ratio samplers | `1.0` |
| `OTEL_SDK_DISABLED` | `true` makes the SDK itself a no-op, so nothing is recorded even when an endpoint is set | `false` |
| `OTEL_METRIC_EXPORT_INTERVAL` | Push interval in milliseconds | SDK default |
| `OTEL_SERVICE_NAME` | `service.name`, overriding the `service_name` argument | the `service_name` argument |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes, for example `service.namespace=groovemap,deployment.environment.name=dev` | empty |

Behavior the consumer can rely on:

- Telemetry never fails startup. A missing endpoint, `OTEL_METRICS_EXPORTER=none`,
  `OTEL_TRACES_EXPORTER=none`, an absent `otel` extra, or a malformed configuration all fall
  back to a no-op provider for the affected signal and log one line instead of raising. Without
  the extra the fallback is a local stand-in, so nothing in `common` requires an
  `opentelemetry` package to be installed at all.
- The two signals are independent. Metrics can export while tracing is off and the other way
  round; only the endpoint is shared.
- `setup_telemetry` is idempotent. A second call returns the providers the first installed
  without building a second exporter.
- Environment wins over code. `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` override the
  `service.name` and `service.version` the bootstrap derives from its arguments.
- `service_version` defaults to the version of an installed distribution named `service_name`;
  a service whose deployment name differs from its distribution name should pass it
  explicitly, and the attribute is omitted when neither resolves.
- Export temporality is cumulative, so a Prometheus-backed collector reads counters correctly.
- One-shot processes must call `shutdown_telemetry` before exiting or the periodic metric reader
  and the batch span processor drop everything recorded since their last push. Spans are flushed
  and shut down before metrics, so a process that dies mid-shutdown loses the cheaper signal. It
  is safe without a prior `setup_telemetry`, safe to call twice, and never raises.

### Tracing boundary

Spans go to the same collector over OTLP HTTP/protobuf through a `BatchSpanProcessor`. Tracing
is configured entirely from the environment variables above, so a service turns it on by
setting an endpoint and nothing else.

- The sampler is the SDK's env-configured sampler. The bootstrap defaults `OTEL_TRACES_SAMPLER`
  to `parentbased_traceidratio` and `OTEL_TRACES_SAMPLER_ARG` to `1.0`, so a deployment only
  ever sets the ratio: dev keeps `1.0`, production turns it down. An operator value always wins,
  and a blank value counts as unset. A ratio of `0` drops root spans while a sampled parent
  still keeps its children, so a trace that starts upstream stays whole.
- The global propagator is W3C TraceContext plus baggage. `OTEL_PROPAGATORS` still wins when an
  operator sets it.
- `get_tracer(name, version=None)` returns a tracer from the installed provider, or a no-op
  tracer before `setup_telemetry` and without the `otel` extra.
- `inject_headers(headers)` writes `traceparent` and `tracestate` into a mutable header mapping
  and returns it; `extract_context(headers)` reads them back and returns a context to pass as
  `start_as_current_span(..., context=...)`, or `None` when the headers carry no readable
  context. Both accept `str` and `bytes` values, so an AMQP header dict round-trips unchanged,
  and both are no-ops without the extra. A malformed `traceparent` starts a new trace rather
  than failing the message that delivered it.

Span names are low-cardinality by construction and follow the GrooveMap OpenTelemetry
conventions:

| Span | Name | Kind |
| --- | --- | --- |
| HTTP server and client | from the instrumentors, route-templated | `SERVER`, `CLIENT` |
| Database operation | `{db.operation.name} {db.system.name}` | `CLIENT` |
| Broker publish | `publish {messaging.destination.name}` | `PRODUCER` |
| Broker consume | `process {messaging.destination.name}` | `CONSUMER` |
| Batch flush | `flush {store} {entity}` | `INTERNAL` |

Attribute values come from the same closed sets the metric attributes use. A span never carries
a statement, an id, a file name, or free text, and an error sets status `ERROR` with `error.type`
only. Span metrics (call counts and duration per span name) are derived by the collector, never
emitted by a service.

### HTTP instrumentation

`instrument_fastapi_app(app, *, excluded_urls="health,ready,metrics")` emits
`http.server.request.duration` with `http.route` and `http.response.status_code`, and a `SERVER`
span per request. The route is the templated path (`/artists/{artist_id}`), never the raw one,
so both the attribute and the span name stay low-cardinality; `/health` and `/ready` are
excluded by default because probes would otherwise dominate the histogram.

`instrument_httpx(client=None)` emits `http.client.request.duration` with `server.address` and
the response status code, and a `CLIENT` span per request that writes `traceparent` onto the
outbound request. Pass a client to instrument only that client, or nothing to instrument every
httpx client in the process.

Both need the `otel-http` extra. Without it each returns `False` after logging one line, so a
service that has not installed the extra still starts and serves normally. Both return `True`
when instrumentation was applied. Call them after `setup_telemetry` so they bind to the
configured providers; spans appear only once a `TracerProvider` is live.

Both default `OTEL_SEMCONV_STABILITY_OPT_IN` to `http` before the first instrumentation
initializes. The contrib packages otherwise emit the pre-stable names (`http.server.duration`
in milliseconds). An operator value wins; a blank value counts as unset.

### Runtime metrics

`setup_telemetry` also installs `opentelemetry-instrumentation-system-metrics` with a
process-scoped configuration, so every service reports its own CPU, memory, threads, file
descriptors, and garbage collection without writing a line. No `system.*` host metric is
collected: a host is scraped once by node-exporter, and a service reporting host numbers would
multiply them by however many containers share the machine.

These are the instrument names the pinned instrumentor version emits, and the Prometheus names
the collector's remote-write translation produces from them. The deployment metric catalog
copies this list, so change it here first.

| Instrument | Kind, unit | Attributes | Prometheus name |
| --- | --- | --- | --- |
| `process.cpu.time` | observable counter, seconds | `type` (`user`, `system`) | `process_cpu_time_seconds_total` |
| `process.cpu.utilization` | observable gauge, ratio | none | `process_cpu_utilization_ratio` |
| `process.memory.usage` | observable up-down counter, bytes | none | `process_memory_usage_bytes` |
| `process.memory.virtual` | observable up-down counter, bytes | none | `process_memory_virtual_bytes` |
| `process.thread.count` | observable up-down counter | none | `process_thread_count` |
| `process.open_file_descriptor.count` | observable up-down counter | none | `process_open_file_descriptor_count` |
| `process.context_switches` | observable counter | `type` (`involuntary`, `voluntary`) | `process_context_switches_total` |
| `cpython.gc.collections` | observable counter, collections | `generation`, `cpython.gc.generation` | `cpython_gc_collections_total` |
| `groovemap.runtime.event_loop.lag` | histogram, seconds | none | `groovemap_runtime_event_loop_lag_seconds` |

Two of them are platform-conditional and simply absent where the interpreter or the operating
system cannot supply them: `process.open_file_descriptor.count` is not emitted on Windows, and
the `cpython.gc.*` instruments are not emitted on PyPy. Instruments are registered against the
configured provider only, so a service with no endpoint pays nothing.

`start_event_loop_monitor(interval_s=1.0)` starts the one runtime signal no library provides.
It samples how much longer each `interval_s` sleep actually took, which is the time the loop
could not run a ready callback, and records it into `groovemap.runtime.event_loop.lag` with
explicit boundaries from one millisecond to five seconds. Call it from the service's running
loop after `setup_telemetry`. It returns the sampling task, or `None` when there is nothing to
sample into: before `setup_telemetry`, with metrics export off, without the `otel` extra, or
outside a running loop. It is idempotent per loop, never raises, and `shutdown_telemetry`
cancels it.

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
| `messaging.client.sent.messages` | counter | same, with `messaging.operation.name` `send` | the RabbitMQ wrappers' `publish` |
| `messaging.client.operation.duration` | histogram, seconds | same as the message counters | `process_message_with_retry` and `publish` |

`db.operation.name` is a short verb (`session`, `execute`). SQL and Cypher text, record ids, and
hostnames are never attribute values. `CircuitBreakerConfig` takes an optional `system` label for
the gauge; it defaults to the lowercased breaker `name`, so name a breaker after its backing
system or set `system` explicitly. `ResilientConnection` and `AsyncResilientConnection` take the
same optional `system` keyword for the reconnect counter.

### Spans the wrappers emit for free

The same choke points open spans once a `TracerProvider` is live. Nothing else in a service has
to change, and with tracing off every one of them is a no-op.

| Span | Kind | Attributes | Opened by |
| --- | --- | --- | --- |
| `{db.operation.name} {db.system.name}` | `CLIENT` | `db.system.name`, `db.operation.name`, `error.type` on failure | PostgreSQL pools, Neo4j drivers, `execute_sql` |
| `publish {destination}` | `PRODUCER` | `messaging.system`, `messaging.destination.name`, `messaging.operation.name` | the RabbitMQ wrappers' `publish` |
| `process {destination}` | `CONSUMER` | the same three, plus `groovemap.pipeline.retry.count` after a retry | `process_message_with_retry` |
| `flush {store} {entity}` | `INTERNAL` | `db.system.name`, `groovemap.entity` | `flush_span`, in a consumer's batch flush |

The destination is the exchange name, or the routing key when publishing through the default
exchange. Both are configuration, never a per-message value, so the span name stays
low-cardinality. A statement is never attached to a span, and a failure sets status `ERROR` with
`error.type` only: no message, no stack trace, no span event carrying a payload.

`ResilientRabbitMQConnection.publish(routing_key, body, *, exchange="", properties=None,
mandatory=False)` and `AsyncResilientRabbitMQ.publish(message, routing_key, *, exchange=None)`
inject `traceparent` and `tracestate` into the outbound message headers, and
`process_message_with_retry` extracts them, which is what puts an extractor's publish and a
consumer's processing in one trace. The synchronous path copies the `BasicProperties` it is
given before injecting, so a publisher that reuses one properties object does not pin every
later message to the first message's trace.

Retries never nest spans. One delivery is one `process` span however many attempts it took, and
the attempts are reported as `groovemap.pipeline.retry.count`, an integer, and only when there
was more than one.

`flush_span(store, entity, links=None)` is for a consumer's batch flush. Pass the span contexts
of the messages in the batch — `span.get_span_context()` from each delivery, or ready-made
`Link` objects — and at most 64 are attached, because a batch of ten thousand rows would
otherwise carry ten thousand links into the collector.

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
- `flatten_descriptions(descriptions)` flattens either provider description shape — the
  normalized `{"description": [...]}` (or bare-string) form, or the Discogs API's already-flat
  list — into a plain list of strings. `map_discogs_formats` uses it internally, and consumers
  that need to inspect a raw provider `descriptions` value on its own (outside a full format
  entry) can call it directly.
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
