"""The batch engine against a LIVE RabbitMQ broker and a LIVE PostgreSQL server.

The wiring is the loaders': one robust channel shared by every consumer, quorum queues, handlers
that submit and then flush at the batch threshold, and ``run_periodic`` for tails. Each test
decides its outcome from what PostgreSQL stored and what the broker still holds, never from the
engine's own counters, because the failures these cover all reported success: on lux a queue tail
sat unacknowledged for 30 minutes behind a backlog on another queue, the broker's
``consumer_timeout`` closed the shared channel, and the recovered loader then flushed nothing
while reporting healthy.

Requires both services; skipped unless ``RABBITMQ_HOST`` and ``POSTGRES_HOST`` are set. The broker
enforces ``consumer_timeout`` on its channel tick (60 s by default), which bounds the slowest test.
The hung-store test also freezes the server with ``docker pause``, so it needs the container's name
in ``POSTGRES_CONTAINER``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import aio_pika
import httpx
import pytest
import pytest_asyncio
from psycopg import OperationalError, sql
from psycopg.types.json import Jsonb

from common.batch import WRITE_CANCEL_GRACE_S, AsyncBatchEngine, BatchItemResult, BatchPolicy
from common.delivery import Settlement
from common.postgres_resilient import AsyncPostgreSQLPool, PostgreSQLFailureClassifier
from common.rabbitmq_resilient import AsyncResilientRabbitMQ


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from aio_pika.abc import AbstractChannel, AbstractIncomingMessage, AbstractRobustConnection


RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(not (RABBITMQ_HOST and POSTGRES_HOST), reason="RABBITMQ_HOST and POSTGRES_HOST not both set"),
    pytest.mark.timeout(240),
]

USER = os.environ.get("RABBITMQ_USERNAME", "groovemap")
PASSWORD = os.environ.get("RABBITMQ_PASSWORD", "groovemap")
AMQP_PORT = int(os.environ.get("RABBITMQ_AMQP_PORT", "5672"))
MGMT_PORT = int(os.environ.get("RABBITMQ_MGMT_PORT", "15672"))
POSTGRES = {
    "host": POSTGRES_HOST,
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ.get("POSTGRES_USER", "groovemap"),
    "password": os.environ.get("POSTGRES_PASSWORD", "groovemap"),
    "dbname": os.environ.get("POSTGRES_DB", "groovemap"),
}

KEYS = ("artists", "releases")
BATCH_SIZE = 50
PREFETCH = 100
MAX_PENDING = 100
FLUSH_INTERVAL_S = 0.5
# A tail must reach PostgreSQL within a few ticks; 30 minutes of it is what stalled lux.
TAIL_DEADLINE_S = 5.0
PACE_S = 0.03
# A loader's write takes hundreds of milliseconds, in which the next deliveries always arrive. A
# drain faster than the broker's redelivery finds its queue empty, returns, and hides the starvation.
LOADER_WRITE_S = 0.25
# Handlers may wait for the running drain and the one behind it, no more than the delivery limits
# allow. On lux one was retained per record: about 200,000 when the channel closed.
MAX_HANDLERS = 4 * PREFETCH
# Comfortably longer than a tail may take, so the drain never finds its queue empty meanwhile.
BACKLOG = 3000
RECOVERY_DEADLINE_S = 60.0
CONSUMER_TIMEOUT_MS = 1000
CONSUMER_TIMEOUT_DEADLINE_S = 120.0
# Small values for what lux runs at 1500 s and 120 s, so a short failure crosses them as a long one does.
MAX_HOLD_S = 1.0
MAX_WRITE_S = 1.0
TAIL = 3
POSTGRES_CONTAINER = os.environ.get("POSTGRES_CONTAINER", "")


async def _until(label: str, predicate: Callable[[], Any], timeout: float) -> float:
    """Poll an async predicate; return the seconds it took or fail naming what never happened."""
    started = monotonic()
    while monotonic() - started < timeout:
        if await predicate():
            return monotonic() - started
        await asyncio.sleep(0.05)
    pytest.fail(f"timed out after {timeout}s waiting for: {label}")


class Observer:
    def consume(self, _destination: str, _headers: object | None) -> nullcontext[None]:
        return nullcontext()

    def flush(self, _key: str, _size: int, _links: Sequence[object]) -> nullcontext[None]:
        return nullcontext()

    def retry(self, **_kwargs: object) -> None:
        return None

    def settled(self, **_kwargs: object) -> None:
        return None


class AmqpDelivery:
    def __init__(self, message: AbstractIncomingMessage, settled_after: list[float] | None = None) -> None:
        self._message = message
        self._received = monotonic()
        self._settled_after = settled_after

    def _settled(self) -> None:
        if self._settled_after is not None:
            self._settled_after.append(monotonic() - self._received)

    async def ack(self) -> None:
        await self._message.ack()
        self._settled()

    async def nack(self, *, requeue: bool) -> None:
        await self._message.nack(requeue=requeue)
        self._settled()

    @property
    def headers(self) -> dict[str, Any]:
        return dict(self._message.headers or {})


@dataclass
class Sink:
    """Write each batch in one PostgreSQL transaction, as the loaders do."""

    pool: AsyncPostgreSQLPool
    tables: dict[str, str]
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    malformed_once: set[str] = field(default_factory=set)
    # Time each write takes beyond the SQL, so a local server keeps a loader's pace behind the backlog.
    pace_s: dict[str, float] = field(default_factory=dict)
    # What the driver raises when the server is unreachable, for the write this is set on.
    unreachable: bool = False

    async def write(self, key: str, payloads: Sequence[dict[str, Any]]) -> Sequence[BatchItemResult]:
        if self.unreachable:
            raise OperationalError("connection refused")
        if key in self.gates:
            await self.gates[key].wait()
        table = sql.Identifier(self.tables[key])
        statement = sql.SQL(
            "INSERT INTO {table} (id, data) SELECT i, d FROM unnest(%s::text[], %s::jsonb[]) AS incoming(i, d) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, writes = {table}.writes + 1"
        ).format(table=table)
        latest = {payload["id"]: payload for payload in payloads}
        async with self.pool.connection() as connection, connection.transaction():
            await connection.execute(statement, (list(latest), [Jsonb(value) for value in latest.values()]))
        await asyncio.sleep(self.pace_s.get(key, 0.0))
        if key in self.malformed_once:
            self.malformed_once.discard(key)
            return []
        return [BatchItemResult(Settlement.ACK, "processed") for _ in payloads]


class Stack:
    """A loader-shaped consumer of two queues over real services, all named for this test."""

    def __init__(self, run: str, pool: AsyncPostgreSQLPool, connection: AbstractRobustConnection, publisher: AbstractRobustConnection) -> None:
        self.run = run
        self.pool = pool
        self.connection = connection
        self.publisher = publisher
        self.queues = {key: f"gm-batch-{run}-{key}" for key in KEYS}
        self.tables = {key: f"gm_batch_{run}_{key}" for key in KEYS}
        self.sink = Sink(pool, self.tables)
        self.prefetch = PREFETCH
        self.gate_runs: list[int] = []
        self.requeues = 0
        self.gate: Callable[[str], Awaitable[bool]] | None = None
        self.published = dict.fromkeys(KEYS, 0)
        self.arguments: dict[str, Any] = {"x-queue-type": "quorum", "x-delivery-limit": 20}
        self.configure(BATCH_SIZE, PREFETCH, MAX_PENDING)
        self.channel: AbstractChannel | None = None
        self.publishing: AbstractChannel | None = None
        self.periodic: asyncio.Task[None] | None = None
        self.handlers = 0
        self.peak_handlers = 0
        self.stop_backlog = asyncio.Event()
        self.settled_after: list[float] = []

    def configure(self, batch_size: int, prefetch: int, max_pending: int, **limits: Any) -> None:
        self.prefetch = prefetch
        self.max_pending = max_pending
        self.engine: AsyncBatchEngine[str, dict[str, Any]] = AsyncBatchEngine(
            KEYS,
            policy=BatchPolicy(
                batch_size=batch_size,
                flush_interval_s=limits.pop("flush_interval_s", FLUSH_INTERVAL_S),
                max_pending=max_pending,
                max_concurrent_flushes=2,
                min_batch_size=1,
                backoff_initial_s=0.1,
                backoff_max_s=1.0,
                backoff_multiplier=2.0,
                max_drain_retries=5,
                max_poison_retries=5,
                **limits,
            ),
            sink=self.sink,
            classifier=PostgreSQLFailureClassifier(),
            observer=Observer(),
        )

    async def declare(self) -> None:
        async with self.pool.connection() as connection:
            for table in self.tables.values():
                await connection.execute(
                    sql.SQL("CREATE TABLE {table} (id text PRIMARY KEY, data jsonb NOT NULL, writes integer NOT NULL DEFAULT 1)").format(
                        table=sql.Identifier(table)
                    )
                )
        # Publishing has its own connection: these tests take the consumers' down, and a publish
        # in flight when that happens could not say whether it landed.
        self.publishing = await self.publisher.channel()
        for queue in self.queues.values():
            await self.publishing.declare_queue(queue, durable=True, arguments=self.arguments)

    async def consume(self) -> AbstractChannel:
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=self.prefetch, global_=False)
        for key, name in self.queues.items():
            queue = await channel.declare_queue(name, durable=True, arguments=self.arguments)
            await queue.consume(self._handler(key))
        self.channel = channel
        self.periodic = asyncio.create_task(self.engine.run_periodic())
        return channel

    def _handler(self, key: str) -> Callable[[AbstractIncomingMessage], Any]:
        async def handle(message: AbstractIncomingMessage) -> None:
            self.handlers += 1
            self.peak_handlers = max(self.peak_handlers, self.handlers)
            try:
                record = json.loads(message.body)
                if record.get("type") == "extraction_complete":
                    await self._complete(key, message)
                    return
                await self.engine.submit(key, record, AmqpDelivery(message, self.settled_after))
                state = cast("dict[str, dict[str, int]]", self.engine.snapshot())
                if state["pending"][key] >= min(state["batch_sizes"][key], self.max_pending):
                    await self.engine.flush(key)
            finally:
                self.handlers -= 1

        return handle

    async def _complete(self, key: str, message: AbstractIncomingMessage) -> None:
        """What both loaders do with the end of a file: only a drained key may run its purge."""
        assert self.gate is not None
        if not await self.gate(key):
            self.requeues += 1
            await message.nack(requeue=True)
            return
        self.gate_runs.append(await self.rows(key))
        await message.ack()

    async def publish_marker(self, key: str) -> None:
        assert self.publishing is not None
        body = json.dumps({"type": "extraction_complete"}).encode()
        await self.publishing.default_exchange.publish(aio_pika.Message(body), routing_key=self.queues[key])

    async def publish(self, key: str, count: int) -> None:
        assert self.publishing is not None
        start = self.published[key]
        self.published[key] += count
        await asyncio.gather(
            *(
                self.publishing.default_exchange.publish(
                    aio_pika.Message(
                        json.dumps({"id": str(number), "title": f"{key} {number}"}).encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                    ),
                    routing_key=self.queues[key],
                )
                for number in range(start, start + count)
            )
        )

    async def backlog(self, key: str, lead: int) -> None:
        """Keep ``lead`` messages ready until ``stop_backlog``, so ``key`` never drains.

        Stopped rather than cancelled: a publish cut short would be counted without being sent.
        """
        while not self.stop_backlog.is_set():
            if await self.ready(key) < lead:
                await self.publish(key, lead // 2)
            else:
                await asyncio.sleep(0.02)

    async def ready(self, key: str) -> int:
        channel = await self.publisher.channel()
        try:
            declared = await channel.declare_queue(self.queues[key], passive=True, robust=False)
            return int(declared.declaration_result.message_count or 0)
        finally:
            await channel.close()

    async def rows(self, key: str) -> int:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(sql.SQL("SELECT count(*) FROM {table}").format(table=sql.Identifier(self.tables[key])))
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def rewritten(self, key: str) -> int:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(sql.SQL("SELECT count(*) FROM {table} WHERE writes > 1").format(table=sql.Identifier(self.tables[key])))
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    async def consumers(self, key: str) -> int:
        async with httpx.AsyncClient(base_url=f"http://{RABBITMQ_HOST}:{MGMT_PORT}", auth=(USER, PASSWORD), timeout=15.0) as client:
            response = await client.get(f"/api/queues/%2F/{quote(self.queues[key], safe='')}")
        response.raise_for_status()
        return int(response.json().get("consumers", -1))

    async def outstanding(self, key: str) -> dict[str, int]:
        """The broker's own count of what ``key`` still holds, ready or unacknowledged."""
        async with httpx.AsyncClient(base_url=f"http://{RABBITMQ_HOST}:{MGMT_PORT}", auth=(USER, PASSWORD), timeout=15.0) as client:
            response = await client.get(f"/api/queues/%2F/{quote(self.queues[key], safe='')}")
        response.raise_for_status()
        queue = response.json()
        return {name: int(queue.get(name, -1)) for name in ("messages_ready", "messages_unacknowledged")}

    async def assert_everything_stored_and_settled(self) -> None:
        """Every published record is a row, and the broker holds nothing, ready or unacked.

        Asked of the broker while the consumers still run: closing them first would requeue
        deliveries that are stored and only waiting for the tick that acks them.
        """
        for key in KEYS:
            try:
                await _until(f"all {self.published[key]} {key} rows in PostgreSQL", lambda key=key: self._stored_all(key), RECOVERY_DEADLINE_S)
            except pytest.fail.Exception:
                print(f"{key}: rows={await self.rows(key)} broker={await self.outstanding(key)} engine={self.engine.snapshot()}")
                raise
        assert self.periodic is not None
        assert not self.periodic.done(), "run_periodic ended"
        await _until("the engine to hold nothing", self._engine_empty, RECOVERY_DEADLINE_S)
        for key in KEYS:
            await _until(f"the broker to hold nothing for {key}", lambda key=key: self._broker_empty(key), RECOVERY_DEADLINE_S)
            assert await self.rows(key) == self.published[key]

    async def _broker_empty(self, key: str) -> bool:
        return not any((await self.outstanding(key)).values())

    async def _stored_all(self, key: str) -> bool:
        return await self.rows(key) == self.published[key]

    async def _engine_empty(self) -> bool:
        state = cast("dict[str, dict[str, int]]", self.engine.snapshot())
        return not any(state["pending"].values())

    async def teardown(self) -> None:
        self.engine.shutdown()
        if self.periodic is not None:
            self.periodic.cancel()
            await asyncio.gather(self.periodic, return_exceptions=True)
        for gate in self.sink.gates.values():
            gate.set()
        channel = await self.publisher.channel()
        for name in self.queues.values():
            await channel.queue_delete(name)
        async with self.pool.connection() as connection:
            for table in self.tables.values():
                await connection.execute(sql.SQL("DROP TABLE IF EXISTS {table}").format(table=sql.Identifier(table)))


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[Stack]:
    pool = AsyncPostgreSQLPool(POSTGRES, min_connections=1, max_connections=6)
    await pool.initialize()
    run = uuid.uuid4().hex[:12]
    url = f"amqp://{USER}:{PASSWORD}@{RABBITMQ_HOST}:{AMQP_PORT}/"
    manager = AsyncResilientRabbitMQ(f"{url}?name=gm-batch-{run}", retry_delay=1.0)
    connection = await manager.connect()
    publisher = await aio_pika.connect_robust(url)
    built = Stack(run, pool, connection, publisher)
    try:
        yield built
    finally:
        await built.teardown()
        await publisher.close()
        await manager.close()
        await pool.close()


async def _closes(channel: AbstractChannel) -> asyncio.Future[object]:
    closed: asyncio.Future[object] = asyncio.get_running_loop().create_future()

    def on_close(_sender: object, reason: object = None) -> None:
        if not closed.done():
            closed.set_result(reason)

    channel.close_callbacks.add(on_close)
    return closed


async def _drop_consumer_connection(stack: Stack) -> None:
    """Have the broker close the consumers' connection, with deliveries in flight."""
    name = f"gm-batch-{stack.run}"
    async with httpx.AsyncClient(base_url=f"http://{RABBITMQ_HOST}:{MGMT_PORT}", auth=(USER, PASSWORD), timeout=15.0) as client:
        for _ in range(100):
            connections = (await client.get("/api/connections")).raise_for_status().json()
            matching = [info for info in connections if info.get("client_properties", {}).get("connection_name") == name]
            if matching:
                break
            await asyncio.sleep(0.2)
        else:
            pytest.fail("management API never listed the consumers' connection; are management stats enabled?")
        for info in matching:
            response = await client.delete(f"/api/connections/{quote(info['name'], safe='')}")
            assert response.status_code in (204, 404)


async def test_small_tail_flushes_within_seconds_while_another_queue_stays_backlogged(stack: Stack) -> None:
    await stack.declare()
    stack.sink.pace_s["releases"] = LOADER_WRITE_S
    backlog = asyncio.create_task(stack.backlog("releases", BACKLOG))
    try:
        await _until("releases to backlog", lambda: _at_least(stack.ready("releases"), BACKLOG), 30)
        await stack.consume()
        await _until("the backlog to be draining", lambda: _at_least(stack.rows("releases"), 3 * BATCH_SIZE), 30)

        tail = BATCH_SIZE - 1
        await stack.publish("artists", tail)
        waited = await _until("the artists tail in PostgreSQL", lambda: _at_least(stack.rows("artists"), tail), TAIL_DEADLINE_S)
        print(f"tail of {tail} stored {waited:.2f}s after publishing, under backlog")

        assert await stack.ready("releases") > 0, "the backlog ran out, so nothing was starving"
    finally:
        stack.stop_backlog.set()
        await backlog

    stack.sink.pace_s.clear()
    await stack.assert_everything_stored_and_settled()
    assert stack.peak_handlers < MAX_HANDLERS


async def _at_least(value: Any, floor: int) -> bool:
    return bool(await value >= floor)


async def _loader_gate(stack: Stack, key: str) -> bool:
    """discogs-sql-loader's PostgreSQLBatchProcessor.flush_queue, whose backoff is this one's."""
    for attempt in range(1, 6):
        if await stack.engine.flush(key):
            return True
        if cast("dict[str, dict[str, int]]", stack.engine.snapshot())["pending"][key] == 0:
            return True
        await asyncio.sleep(0.1 * 2 ** (attempt - 1))
    return False


async def _enricher_gate(stack: Stack, key: str) -> bool:
    """discogs-graph-enricher's Neo4jBatchProcessor.flush_queue, whose backoff is this one's."""
    for attempt in range(1, 6):
        if await stack.engine.flush(key):
            return True
        await asyncio.sleep(0.1 * 2 ** (attempt - 1))
    return False


@pytest.mark.parametrize("gate", [_loader_gate, _enricher_gate], ids=["loader-purge", "enricher-maintenance"])
async def test_a_purge_cannot_run_while_records_are_in_flight_at_batch_size_500(stack: Stack, gate: Callable[[Stack, str], Awaitable[bool]]) -> None:
    """Lux's settings. Another queue's backlog holds the shared capacity, so these records sit in
    submit rather than the queue when the marker behind them asks whether the key has drained.
    """
    stack.configure(batch_size=500, prefetch=1000, max_pending=1000)
    stack.gate = lambda key: gate(stack, key)
    await stack.declare()
    stack.sink.pace_s["releases"] = LOADER_WRITE_S
    backlog = asyncio.create_task(stack.backlog("releases", BACKLOG))
    try:
        await _until("releases to backlog", lambda: _at_least(stack.ready("releases"), BACKLOG), 30)
        await stack.consume()
        await _until("the backlog to hold capacity", lambda: _at_least(stack.rows("releases"), 500), 30)

        # Under prefetch, so the marker is delivered with every record before it still in flight.
        records = 900
        await stack.publish("artists", records)
        await stack.publish_marker("artists")
        await _until("the purge to run", lambda: _ran(stack), RECOVERY_DEADLINE_S)

        assert stack.gate_runs == [records], f"purge saw {stack.gate_runs} of {records} rows"
        # A marker requeued 20 times is dead-lettered, and then the purge never runs at all.
        assert stack.requeues < 5
    finally:
        stack.stop_backlog.set()
        await backlog

    stack.sink.pace_s.clear()
    await stack.assert_everything_stored_and_settled()


async def _ran(stack: Stack) -> bool:
    return bool(stack.gate_runs)


async def _held(stack: Stack, key: str, count: int) -> bool:
    return cast("dict[str, dict[str, int]]", stack.engine.snapshot())["pending"][key] == count


async def _tail_held_past_the_bound(stack: Stack, **limits: Any) -> None:
    """A tail of a type, one failed write, then the hold passes and the store comes back."""
    stack.configure(BATCH_SIZE, PREFETCH, MAX_PENDING, max_hold_s=MAX_HOLD_S, **limits)
    await stack.declare()
    await stack.consume()
    stack.sink.unreachable = True
    await stack.publish("artists", TAIL)
    await _until("the tail to be held", lambda: _held(stack, "artists", TAIL), 10)
    assert await stack.engine.flush("artists") is False
    await asyncio.sleep(MAX_HOLD_S + 0.1)
    stack.sink.unreachable = False


@pytest.mark.parametrize("gate", [_loader_gate, _enricher_gate], ids=["loader-purge", "enricher-maintenance"])
async def test_a_marker_cannot_land_while_its_records_are_on_the_broker(stack: Stack, gate: Callable[[Stack, str], Awaitable[bool]]) -> None:
    """The drain that answers the marker is the one that returns the tail to RabbitMQ."""
    stack.gate = lambda key: gate(stack, key)
    # No periodic flush, or it writes the tail first and this passes without the fix.
    await _tail_held_past_the_bound(stack, flush_interval_s=600.0)

    await stack.publish_marker("artists")
    await _until("the purge to run", lambda: _ran(stack), RECOVERY_DEADLINE_S)

    assert stack.gate_runs == [TAIL], f"purge saw {stack.gate_runs} of {TAIL} rows"
    assert stack.requeues < 5
    await stack.assert_everything_stored_and_settled()


async def test_the_drain_behind_a_release_does_not_see_an_empty_key(stack: Stack) -> None:
    """A caller that shares the next drain runs it the moment the release ends, records on the wire."""
    # No periodic flush, so the release is the one this test asks for and nothing writes the redelivery.
    await _tail_held_past_the_bound(stack, flush_interval_s=600.0)

    assert await stack.engine.flush("artists") is False, "the drain that released the tail"
    assert await stack.engine.flush("artists") is False, "the drain right behind it"

    await _until("the redelivered tail to drain", lambda: stack.engine.flush("artists"), 10)
    assert await stack.rows("artists") == TAIL
    await stack.assert_everything_stored_and_settled()


@pytest.mark.skipif(not POSTGRES_CONTAINER, reason="POSTGRES_CONTAINER not set")
@pytest.mark.timeout(120)
async def test_a_frozen_store_returns_deliveries_inside_the_bound_and_inside_the_consumer_timeout(stack: Stack) -> None:
    """``docker pause``: the server accepts bytes and answers nothing, so the write raises nothing.

    The drain has to end anyway, and one cancel does not end this write, because the driver's
    rollback waits on the same frozen server. If a delivery stayed past the bound, the broker's own
    timeout would cancel the consumer, and that is what this checks, not just the engine's account.
    """
    frozen_s = 20.0
    bound_s = MAX_HOLD_S + 2 * (MAX_WRITE_S + WRITE_CANCEL_GRACE_S) + FLUSH_INTERVAL_S
    consumer_timeout_s = bound_s + 2.0
    stack.arguments["x-consumer-timeout"] = int(consumer_timeout_s * 1000)
    stack.configure(BATCH_SIZE, PREFETCH, MAX_PENDING, max_hold_s=MAX_HOLD_S, max_write_s=MAX_WRITE_S)
    await stack.declare()
    await stack.consume()
    await stack.publish("artists", TAIL)
    await _until("the tail to be held", lambda: _held(stack, "artists", TAIL), 10)

    subprocess.run(["docker", "pause", POSTGRES_CONTAINER], check=True, timeout=30)  # noqa: S603, S607
    try:
        await asyncio.sleep(frozen_s)
        returned = list(stack.settled_after)
        state = cast("dict[str, dict[str, int]]", stack.engine.snapshot())
    finally:
        subprocess.run(["docker", "unpause", POSTGRES_CONTAINER], check=True, timeout=30)  # noqa: S603, S607

    print(
        f"\nFROZEN {frozen_s:g}s: {len(returned)} returns, longest hold {max(returned, default=0.0):.2f}s, bound {bound_s:.2f}s, left {state['abandoned_writes']}"
    )
    assert len(returned) >= TAIL, "nothing came back during the freeze"
    assert max(returned) <= bound_s
    await stack.assert_everything_stored_and_settled()
    consumers = await stack.consumers("artists")
    assert consumers == 1, "the broker cancelled the consumer, so a delivery passed the consumer timeout"


async def test_a_flush_failure_does_not_end_periodic_flushing(stack: Stack) -> None:
    await stack.declare()
    stack.sink.malformed_once.add("artists")
    await stack.consume()

    await stack.publish("artists", 7)

    await stack.assert_everything_stored_and_settled()
    state = cast("dict[str, dict[str, int]]", stack.engine.snapshot())
    assert state["periodic_failures"]["artists"] == 1


async def test_consumer_timeout_with_deliveries_in_flight_loses_nothing(stack: Stack) -> None:
    """The lux failure: the broker closes the channel every consumer shares, mid-load."""
    stack.arguments["x-consumer-timeout"] = CONSUMER_TIMEOUT_MS
    await stack.declare()
    # A write that cannot finish holds these deliveries past the timeout, as starvation did.
    gate = asyncio.Event()
    stack.sink.gates["artists"] = gate
    await stack.publish("artists", 2 * BATCH_SIZE + 7)
    backlog = asyncio.create_task(stack.backlog("releases", 500))
    try:
        channel = await stack.consume()
        closed = await _closes(channel)
        reason = await asyncio.wait_for(closed, timeout=CONSUMER_TIMEOUT_DEADLINE_S)
        # A channel error, or the broker cancels the consumer and aio-pika's re-consume reuses
        # the tag, which closes the connection: RabbitMQ 4.3 does the second, and so might lux.
        assert any(text in str(reason) for text in ("timed out", "PRECONDITION_FAILED", "NOT_ALLOWED", "reuse consumer tag")), reason
        assert await stack.rows("artists") == 0

        gate.set()
        # Real processing after the loss, not just a live-looking consumer.
        floor = await stack.rows("releases") + 3 * BATCH_SIZE
        await _until("releases to keep landing after the channel loss", lambda: _at_least(stack.rows("releases"), floor), RECOVERY_DEADLINE_S)
    finally:
        stack.stop_backlog.set()
        await backlog

    await stack.assert_everything_stored_and_settled()
    state = cast("dict[str, dict[str, int]]", stack.engine.snapshot())
    assert state["lost_deliveries"]["artists"] > 0, "the channel loss did not catch a delivery in flight"


async def test_a_dropped_connection_with_deliveries_in_flight_loses_nothing(stack: Stack) -> None:
    await stack.declare()
    stack.sink.pace_s["releases"] = PACE_S
    backlog = asyncio.create_task(stack.backlog("releases", 500))
    try:
        await stack.consume()
        await _until("the backlog to be draining", lambda: _at_least(stack.rows("releases"), 3 * BATCH_SIZE), 30)

        await _drop_consumer_connection(stack)

        floor = await stack.rows("releases") + 3 * BATCH_SIZE
        await _until("releases to keep landing after the drop", lambda: _at_least(stack.rows("releases"), floor), RECOVERY_DEADLINE_S)
    finally:
        stack.stop_backlog.set()
        await backlog

    await stack.assert_everything_stored_and_settled()
    state = cast("dict[str, dict[str, int]]", stack.engine.snapshot())
    assert state["lost_deliveries"]["releases"] > 0, "the drop did not catch a delivery in flight"
