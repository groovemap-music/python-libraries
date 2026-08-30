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

These imports are lazy. Importing `common` does not load optional database, broker, or metrics
clients until the corresponding capability is requested. Other names in `common.*` modules are
implementation details or transitional service helpers and do not carry compatibility promises.
In particular, a leading-underscore helper is private even if an existing GrooveMap service still
imports it during migration.

## Optional capabilities

| Extra | Enables | Required for |
| --- | --- | --- |
| `metrics` | `prometheus-client` | Serving `/metrics` from `HealthServer` |
| `neo4j` | Neo4j driver | Neo4j connection and retry helpers |
| `postgres` | Psycopg | PostgreSQL pools and query execution |
| `rabbitmq` | aio-pika and pika | Async and synchronous broker resilience |
| `all` | Every optional dependency | Development and full validation only |

Consumers should install only the extras they use, for example
`groovemap-runtime[postgres,metrics]`. The base package includes structured logging and record
normalization.

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

## Compatibility boundary

See the repository [compatibility and release policy](compatibility-and-releases.md). Public root
imports and their documented behavior are compatibility-controlled; internal modules, private
helpers, deployment defaults, and consumer behavior are not.
