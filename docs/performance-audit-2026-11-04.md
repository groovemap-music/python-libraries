# Database layer and small query sites: performance audit

Date: 2026-11-04. Bead: `gm-python-libraries-ct7c`. Read-only audit; nothing here changes production code.

## Scope, method and limits

Static review of `common.postgres_resilient`, `common.db_resilience`, `common.neo4j_resilient`,
`common.query_debug`, `common.tracing`, `common.runtime_metrics`, `common.batch` and `common.identity`,
plus the small query sites in `operations-console`, `operations-toolkit`, `mcp-server` and
`deployment/scripts`. Consumers were located by reading the sibling checkouts.

**What was measured, and what was not.**

- Microbenchmarks ran against a disposable PostgreSQL 19 container (tmpfs, `--rm`, loopback through
  Colima) and against fake cursors for the in-process cost. Source: three runs of the same script.
  Absolute microseconds include Colima's loopback round trip (about 0.4-0.6 ms) and vary 20-40 % run
  to run; the ratios and the "one extra round trip" findings are the stable part.
- **Partial-data SQL timings are lower bounds.** Production Postgres holds 3.97M of 19.6M releases and
  7.9M of 10.3M artists, and the load is still running. The synthetic table used here was 138 MB;
  production is orders of magnitude larger. Each scaling note below says how the cost should grow.
- No Neo4j was started (image size against the disk budget), so Neo4j costs are static estimates. The
  wrappers' own overhead was measured with fake drivers/cursors.
- PgBouncer was not run: the deployment documents session mode only (see Pool findings), so transaction-mode
  behaviour is out of scope.
- Nothing on lux was touched. Operations-console background load is estimated from code, not measured live.
- Disposable resources were removed: container removed, no volumes created, scratch scripts cleared.

## Headline results

1. **The wrapper layer is cheap per statement.** `execute_sql` plus span plus metric costs about 8 us with
   telemetry off and about 33 us with the OTel SDK on, against a 400-600 us round trip: 1-8 %. Retries,
   the circuit breaker and tracing are not where the time goes.
2. **The pool's checkout is.** `AsyncPostgreSQLPool.connection()` runs a `SELECT 1` on every checkout, so a
   unit of work costs about two round trips instead of one (measured 1.0-1.4 ms vs 0.4-0.6 ms bare query).
   `AsyncResilientPostgreSQL.get_connection()` (30 s probe TTL) measured at bare-query cost.
3. **The operations-console background load is steady and mostly redundant**: about 39,000 collection cycles
   a day with zero viewers, roughly 82,000 Postgres and 87,000 Neo4j statements a day, and about 700,000 HTTP
   requests a day, most of which duplicate what exporters already publish.
4. **One small-site query will not survive the full graph**: `operations-toolkit` `system_monitor` runs
   `MATCH (n) RETURN labels(n)[0], count(n)`, a full node scan.

## Measurements

Median in microseconds (three runs; range shown). "Off" is no OTel provider; "on" is the SDK with spans and
metrics enabled and an unreachable exporter.

| Case | Off | On |
| --- | --- | --- |
| A. raw connection, cursor + `SELECT 1` + fetchone (round-trip floor) | 418-582 | 599-615 |
| B. `pool.connection()` checkout, empty body | 622-624 | 679-686 |
| C. checkout + cursor + `SELECT 1` | 1022-1370 | 1228-1231 |
| D. raw connection through `execute_sql` | 490-637 | 580-604 |
| E. `AsyncResilientPostgreSQL.get_connection()` (TTL) + query | 535-701 | 505-519 |
| F0. fake cursor, bare execute (in-process floor) | 0.2 | 0.2 |
| F1. fake cursor through `execute_sql`, short SQL | 7.6 | 32-34 |
| F2. fake cursor through `execute_sql`, ~400-char SQL + params | 8.7 | 33-35 |
| G1. `tracing.db_span` open/close | 5.9 | 22-23 |
| G2. `runtime_metrics.record_db_operation` | 0.3 | 4.1-4.3 |

Reading it:

- B is about one full round trip: the checkout probe. C is about 2-2.5x A. E, which skips the probe inside its
  TTL, is about A.
- F1 vs F2: SQL length barely matters (about 1 us), so the unconditional render-and-redact in `log_sql_query`
  is not measurable next to the span. The span dominates the in-process cost, and it is 4x dearer with the SDK
  on. Fine at one statement per millisecond; worth knowing if a caller issues thousands of tiny statements.
- Console statements, measured on a 138 MB database in one segment: `pg_database_size` and the
  `pg_stat_activity` count were both under the noise of a round trip (about 1.5 ms through pgbench, against
  1.8 ms for `SELECT 1`). **Lower bound.** `pg_database_size` stats every relation file, so it grows with the
  file count: at production size (hundreds of 1 GB segments across heap, TOAST and indexes) expect low
  milliseconds up to tens of milliseconds per call. `pg_stat_activity` grows with backends, not data.
- `httpx.AsyncClient()` create plus close: 4.4 ms median (7.1 ms p95), CPU only. The console creates five a cycle.

## Ranked improvements

Gain is per affected call unless stated. Risk is for the change, not the status quo. Follow-up beads are
proposed at the end; none are filed from this bead.

| # | Change | Owning repo | Est. gain | Risk |
| --- | --- | --- | --- | --- |
| 1 | Replace the full node scan in `system_monitor` with count-store queries | operations-toolkit | Minutes of full-graph scan and a page-cache flush avoided; O(labels) hits | Low |
| 2 | Console: cache or share the Neo4j and Postgres readings; slow the idle cycle | operations-console | About 80 % fewer idle DB statements; more with the idle cadence | Low-medium |
| 3 | Pool: skip the checkout `SELECT 1` inside a TTL; recover on first-use failure | python-libraries | One round trip per checkout (about 40-50 % of a small unit of work) | Medium |
| 4 | Async Neo4j driver: move the health probe out of the handle lock, bound its wait | python-libraries | Removes a possible 60 s stall of all new sessions under saturation | Medium |
| 5 | Pool: idle shrink and max lifetime, so the backend footprint stops ratcheting | python-libraries | Frees session-mode backends after bursts | Medium |
| 6 | Pool and Neo4j wait/usage metrics, and a checkout-latency metric | python-libraries | Makes sizing evidence-based; no runtime gain by itself | Low |
| 7 | Console: one long-lived `httpx.AsyncClient` | operations-console | About 22 ms CPU a cycle (about 1 % of a core) and about 8 sockets/s of churn | Low |
| 8 | `discogs_import_status.py`: fold the two latch psql calls into one | deployment | One of about five process spawns per poll (about 0.3-1 s, unmeasured) | Low |
| 9 | Tidy: guard `log_sql_query` with `isEnabledFor`; quote identifiers in the toolkit's Postgres query | python-libraries, operations-toolkit | Microseconds; one correctness fix | Low |

### 1. `system_monitor`: full node scan (operations-toolkit)

`utilities/system_monitor.py` runs `MATCH (n) RETURN labels(n)[0] as label, count(n) as count ORDER BY count DESC
LIMIT 10` through `cypher-shell`. The `labels(n)[0]` projection prevents the count-store plan, so it is an
all-nodes scan. It is on demand, not polled, but it is linear in nodes: the graph has 19.6M releases alone,
and the console's own comments put the graph at hundreds of millions of nodes. It reads the whole node store
through the page cache on a live database. Intended behaviour: per-label counts from the count store, as the
console's gauge queries already do (`CALL () { MATCH (n:Label) RETURN count(n) }` unioned per label, 10 hits
for 10 labels). Not run here (no Neo4j); the plan argument is from the console's own profiled evidence in
`dashboard/telemetry.py`. Scaling: linear in node count, so it only worsens.

### 2. Operations-console steady background load (quantified)

`DashboardApp.collect_metrics_loop` runs forever from startup, sleeps 2 s between cycles, and does all its
work whether or not a browser is connected. Only the final broadcast is skipped with no clients. Assuming a
0.2 s cycle (network-bound; the cycle is awaited, so it adds to the 2 s), about 2.2 s per cycle gives roughly
**39,000 cycles a day** (0.45 a second).

| Source | Per cycle | Per day |
| --- | --- | --- |
| Postgres: `pg_stat_activity` count + `pg_database_size` | 2 statements | about 78,000 |
| Postgres: connection probe (`SELECT 1`, 30 s TTL) | n/a | about 2,900 |
| Postgres: embeddings job (cached 60 s, loose-index scan) | n/a | about 1,400 |
| Neo4j: `dbms.components` + `apoc.meta.stats` in one session | 2 queries, 1 session | about 78,000 |
| Neo4j: TTL probe (extra session, `RETURN 1`, 30 s) | n/a | about 2,900 |
| Neo4j: telemetry gauges (nodes, relationships, transactions, JMX), one refresh a 60 s export interval | 4 queries | about 5,800 |
| HTTP: 2 pipelines x (2 services + 1 RabbitMQ scrape) + 12 HTTP probes + 1 Valkey TCP ping | about 19 requests | about 740,000 |
| `httpx.AsyncClient` constructions (2 pipelines x 2 + 1 platform) | 5 | about 195,000 |

Findings:

- `dbms.components` returns the server version, which cannot change while the process runs. Cache it for the
  life of the Neo4j connection: that alone removes half the Neo4j queries.
- `apoc.meta.stats` is count-store backed, so it is cheap per call, but the same node and relationship totals
  are already published by the 60 s telemetry gauges and by `postgres-exporter`/VictoriaMetrics for the
  Postgres side. The console's database panel needs minutes-fresh values, not 2 s. Reading them at 30-60 s
  cuts both stores by about 95 %.
- `pg_database_size` is the one statement whose server cost grows with the load (file count); it should not
  run at 0.45 Hz against a database that is about to be hundreds of GB.
- Idle cadence: the 2 s cycle exists for the live queue rates and the event detector. With no websocket
  client the detector still needs continuous samples, so the trade-off is event latency. Proposed: 10 s when
  idle, 2 s with a viewer, decided by the owning bead.
- A new `httpx.AsyncClient` each cycle means a fresh TLS-context build (4.4 ms) and new TCP connections for
  about 19 requests every 2.2 s, about 700,000 short-lived connections a day. A single client held for the
  lifespan keeps them alive.
- Live measurement was not requested and is not needed to rank these; it would refine the 0.2 s cycle
  assumption only.

### 3. Pool checkout probe

`_pooled_connection` calls `_test_connection` (cursor + `SELECT 1` + fetch) on every checkout, then
`set_autocommit(True)` on return (a local flag in psycopg 3, no round trip). `AsyncResilientConnection`
already solves this with `health_check_ttl` (30 s) and a consecutive-failure threshold; the pool did not take
it. Measured: B (probe only) is one round trip; C is 2-2.5x a bare query. Consumers: `catalog-api` (per
request), both SQL loaders (per batch), `insights`, `database-schema`'s initializer and several scripts.
Batch loaders pay it once per hundreds of rows, so their gain is small; `catalog-api` and `insights` pay it
per request and gain most. **Risk**: a connection that died inside the TTL reaches the caller as a first-use
error, so the change needs a one-time reconnect-and-retry for idempotent checkouts, and it touches the code
`gm-python-libraries-ojfe` is changing (failure classification). Sequence it after ojfe lands. A cheaper
alternative with no staleness risk: check `conn.closed`/`conn.broken` locally and probe only when the
connection sat idle longer than the TTL.

### 4. Async Neo4j health probe holds the handle lock

`AsyncResilientConnection.get_connection` holds `self._lock` while `_connection_is_usable` awaits the probe.
The probe opens a driver session and runs `RETURN 1`, so with all 50 Bolt connections busy it queues for a
free one for up to `connection_acquisition_timeout` (60 s), and every new `session()` caller waits behind the
lock for that time, once per 30 s TTL window. The docstring recognises that a pool-acquisition timeout means
the connection is busy and working. Not reproduced (no Neo4j); this is a static finding. The fix is to run the
probe outside the lock (single-flight) or bound it far below 60 s; a busy driver should not be probed at all.
The sync `ResilientNeo4jDriver` has the same lock-around-I/O shape and, worse, probes with a fresh session on
**every** `session()` call with no TTL. No sibling repo constructs it (grep of the checkouts), so it is a
latent hazard rather than a current cost; the bead should delete or align it.

### 5. Pool never shrinks

`AsyncPostgreSQLPool` has no idle timeout and no maximum connection lifetime; the health loop replenishes up
to `min_connections` but never closes extras. After a burst the process holds its high-water mark of
session-mode PgBouncer backends until restart. The documented budget is at least 60 usable connections; the
documented maxima (api 8, tableinator 12, brainztableinator 12, insights 4, dashboard 1) sum to 37 with no
slack for other pools in the same process or for the one-shot scripts. `max_connections=20` is the class
default, so any caller that omits the argument can claim 20 backends on its own. Intended behaviour:
close connections idle beyond a timeout down to the minimum, and recycle on age.

Smaller pool observations: the 30 s health loop drains every idle connection from the queue and probes them
one after another, so for that window checkouts see an empty queue and may mint new backends (bounded by
`max_connections`; the window is about 1 ms per idle connection). Pool exhaustion waits
`get_delay(0..4)` per attempt, so a saturated pool raises `ConnectionEstablishmentError` after roughly 15-19 s.
Session mode permits server-side prepared statements, and psycopg already auto-prepares after five executions
(`prepare_threshold=5`), so no change is proposed there; transaction mode is documented as unsupported.

### 6. Observability for sizing

`pool.connection()` records `db.client.operation.duration{operation="session"}` over the whole span from
checkout to return, so that histogram is connection **hold time**, not checkout latency. Nothing reports
pool in-use, idle, waiters, or wait time, so saturation (the cause of the earlier PgBouncer exhaustion
analysis) is invisible until it fails. Add gauges for in-use/idle/waiting, a wait-time histogram, and a
separate checkout-latency measurement.

### 7-9. Smaller items

- **Console HTTP client**: see section 2. `mcp-server` already holds one client for the lifespan and has no
  database access (it calls catalog-api over HTTP); nothing to change there.
- **`discogs_import_status.py`**: each observation spawns `docker compose exec` for `psql` twice
  (`RELATIONS_SQL`, then `LATCH_SQL`), once for `cypher-shell` (a JVM start) and once for `rabbitmqctl`.
  Nothing scales with data (the latch table and `ExtractionCompletion` nodes are tiny, assumed from the
  schema; not measured against the stack). Whether the two latch tables exist is constant after the first
  poll, so the first query can be cached across `--wait` polls (30 s default interval), or both folded into
  one statement.
- **`system_monitor` Postgres query**: `pg_relation_size(schemaname||'.'||relname)` builds an unquoted name,
  which fails for any mixed-case or special-character table; use `relid` (`pg_relation_size(relid)`).
- **`query_debug.log_sql_query`** renders the statement and lower-cases it for redaction on every call, even
  with DEBUG off. Unmeasurable here (about 1 us), but guard it with `isEnabledFor(DEBUG)`.

## Checked and found fine

- Batch engine: flush path holds a per-key lock, not a connection, across the sink call; batch size adapts
  down on failure and up by one on success. No connection is held across an await by the engine itself; the
  sink owns that.
- `identity.resolve_aliases`: three round trips for any batch size using array parameters, one statement per
  step. No per-row loop.
- Neo4j session reuse: sessions are opened per call on a single pooled driver (`max_connection_pool_size`
  50, 30 min lifetime); sessions are cheap. No transaction reuse problem found.
- Serialisation: this repository has no jsonb or orjson hot path in the database layer
  (`json.loads` only for vendored vocabularies at import, `json.dumps` in the health server). The console's
  `model_dump(mode="json")` then `orjson.dumps` happens only while a client is connected; not worth a bead.
- Circuit breaker: the async lock is taken only on connection creation and state change, not per query.

## Coordination

`gm-python-libraries-ojfe` changes failure classification in `postgres_resilient.py`. Items 3 and 5 edit the
same pool; they should be sequenced after it merges. Items 4 and 6 touch different code.

## Proposed follow-up beads (not filed)

1. operations-toolkit: replace the `MATCH (n)` scan in `system_monitor` with count-store queries; quote
   relation names (item 1, 9).
2. operations-console: cache Neo4j version; read database stats on a 30-60 s cadence; idle cadence; one shared
   `httpx.AsyncClient` (items 2, 7).
3. python-libraries: pool checkout probe behind a TTL with first-use recovery, after ojfe (item 3).
4. python-libraries: async Neo4j probe outside the handle lock; remove or align the sync resilient driver
   (item 4).
5. python-libraries: pool idle timeout, max lifetime, and a `max_connections` default that fits the 60-backend
   budget (item 5).
6. python-libraries: pool usage and checkout-latency metrics (item 6, 9).
7. deployment: fold or cache the latch `psql` calls in `discogs_import_status.py` (item 8).
8. Re-measure the Postgres console statements and the Neo4j items once the Postgres load is complete.
