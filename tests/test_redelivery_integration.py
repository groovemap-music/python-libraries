"""What actually ends a message that keeps failing, on a LIVE RabbitMQ broker.

These pin the broker's behaviour as well as the runtime's bound: ``x-delivery-limit`` counts closes
and ``basic.reject`` but never ``basic.nack(requeue=True)``, which is why the runtime counts
``x-acquired-count`` itself. A broker upgrade that changes either count fails here.

Requires RabbitMQ; skipped unless ``RABBITMQ_HOST`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import aio_pika
import httpx
import pytest
import pytest_asyncio

from common.batch import AsyncBatchEngine, BatchPolicy
from common.delivery import DEFAULT_MAX_REQUEUES, DeliveryResult, FailureKind, Settlement, broker_handouts, requeue_or_dead_letter, run_delivery


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

    from aio_pika.abc import AbstractIncomingMessage, AbstractQueue


RABBITMQ_HOST = os.environ.get("RABBITMQ_HOST", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(not RABBITMQ_HOST, reason="RABBITMQ_HOST not set"),
]

USER = os.environ.get("RABBITMQ_USERNAME", "groovemap")
PASSWORD = os.environ.get("RABBITMQ_PASSWORD", "groovemap")
AMQP_PORT = int(os.environ.get("RABBITMQ_AMQP_PORT", "5672"))
MGMT_PORT = int(os.environ.get("RABBITMQ_MGMT_PORT", "15672"))
URL = f"amqp://{USER}:{PASSWORD}@{RABBITMQ_HOST}:{AMQP_PORT}/"
LIMIT = 20
# One past the limit: the broker dead-letters on the return that takes the count over it.
BROKER_DELIVERIES = LIMIT + 1
NACK_DELIVERIES = 300


@dataclass
class Poison:
    """One message on a quorum queue declared as the services declare theirs."""

    consumer: aio_pika.abc.AbstractConnection
    name: str
    dlq: AbstractQueue
    arguments: dict[str, Any]
    publisher: aio_pika.abc.AbstractChannel

    async def publish(self, body: bytes) -> None:
        await self.publisher.default_exchange.publish(aio_pika.Message(body, delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key=self.name)

    async def take(self, connection: aio_pika.abc.AbstractConnection) -> AbstractIncomingMessage | None:
        """Consume one delivery on a new channel and leave it unsettled."""
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(self.name, durable=True, arguments=self.arguments)
        arrived: asyncio.Future[AbstractIncomingMessage] = asyncio.get_running_loop().create_future()

        async def handle(message: AbstractIncomingMessage) -> None:
            if not arrived.done():
                arrived.set_result(message)

        await queue.consume(handle)
        try:
            return await asyncio.wait_for(asyncio.shield(arrived), 3.0)
        except TimeoutError:
            return None

    async def dead_letter_reason(self, wait_s: float = 5.0) -> str | None:
        # Dead-lettering is asynchronous, so a message can be a moment behind the nack that sent it.
        deadline = monotonic() + wait_s
        while (message := await self.dlq.get(fail=False, timeout=5)) is None:
            if monotonic() >= deadline:
                return None
            await asyncio.sleep(0.05)
        await message.ack()
        reason = (message.headers or {}).get("x-first-death-reason")
        return reason if isinstance(reason, str) else None

    async def consume_forever(self, handle: Callable[[AbstractIncomingMessage], Awaitable[None]]) -> None:
        channel = await self.consumer.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(self.name, durable=True, arguments=self.arguments)
        await queue.consume(handle)


@pytest_asyncio.fixture
async def poison(request: pytest.FixtureRequest) -> AsyncIterator[Poison]:
    connection = await aio_pika.connect(URL)
    consumer = await aio_pika.connect(URL)
    channel = await connection.channel(publisher_confirms=True)
    name = f"gm-843l-{uuid.uuid4().hex[:8]}"
    exchange = await channel.declare_exchange(f"{name}.dlx", aio_pika.ExchangeType.FANOUT, durable=True)
    dlq = await channel.declare_queue(f"{name}.dlq", durable=True, arguments={"x-queue-type": "classic"})
    await dlq.bind(exchange)
    arguments: dict[str, Any] = {"x-queue-type": "quorum", "x-delivery-limit": LIMIT, "x-dead-letter-exchange": f"{name}.dlx"}
    arguments.update(getattr(request, "param", {}))
    queue = await channel.declare_queue(name, durable=True, arguments=arguments)
    await channel.default_exchange.publish(aio_pika.Message(b"poison", delivery_mode=aio_pika.DeliveryMode.PERSISTENT), routing_key=name)
    try:
        yield Poison(consumer, name, dlq, arguments, channel)
    finally:
        await consumer.close()
        await queue.delete(if_unused=False, if_empty=False)
        await dlq.delete(if_unused=False, if_empty=False)
        await exchange.delete()
        await connection.close()


def counts(message: AbstractIncomingMessage) -> tuple[object, object]:
    headers = message.headers or {}
    return headers.get("x-delivery-count"), headers.get("x-acquired-count")


async def settle_until(poison: Poison, settle: Callable[[AbstractIncomingMessage], Awaitable[None]], deliveries: int) -> list[tuple[object, object]]:
    """Settle every delivery with ``settle`` until ``deliveries`` have arrived."""
    seen: list[tuple[object, object]] = []
    done = asyncio.Event()

    async def handle(message: AbstractIncomingMessage) -> None:
        seen.append(counts(message))
        if len(seen) >= deliveries:
            done.set()
            return
        await settle(message)

    await poison.consume_forever(handle)
    await asyncio.wait_for(done.wait(), 30)
    return seen


async def test_nack_requeue_never_spends_the_delivery_limit(poison: Poison) -> None:
    seen = await settle_until(poison, lambda message: message.nack(requeue=True), NACK_DELIVERIES)
    assert seen[-1] == (None, NACK_DELIVERIES - 1)
    assert all(delivery_count is None for delivery_count, _ in seen)
    assert await poison.dead_letter_reason(wait_s=1.0) is None


async def test_nack_after_cancelling_the_consumer_never_spends_the_limit(poison: Poison) -> None:
    channel = await poison.consumer.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue(poison.name, durable=True, arguments=poison.arguments)
    seen: list[tuple[object, object]] = []
    for _ in range(BROKER_DELIVERIES + 4):
        arrived: asyncio.Future[AbstractIncomingMessage] = asyncio.get_running_loop().create_future()

        async def handle(message: AbstractIncomingMessage, arrived: asyncio.Future[AbstractIncomingMessage] = arrived) -> None:
            arrived.set_result(message)

        tag = await queue.consume(handle)
        message = await asyncio.wait_for(arrived, 5)
        seen.append(counts(message))
        await queue.cancel(tag)
        await message.nack(requeue=True)
    assert seen[-1] == (None, BROKER_DELIVERIES + 3)
    assert await poison.dead_letter_reason(wait_s=1.0) is None


async def test_nack_after_basic_get_never_spends_the_limit(poison: Poison) -> None:
    channel = await poison.consumer.channel()
    queue = await channel.declare_queue(poison.name, durable=True, arguments=poison.arguments)
    seen: list[tuple[object, object]] = []
    for _ in range(BROKER_DELIVERIES + 4):
        message = await queue.get(fail=True, timeout=5)
        assert message is not None
        seen.append(counts(message))
        await message.nack(requeue=True)
    assert seen[-1] == (None, BROKER_DELIVERIES + 3)
    assert await poison.dead_letter_reason(wait_s=1.0) is None


async def test_a_nacked_message_goes_back_to_the_head(poison: Poison) -> None:
    await poison.publish(b"behind")
    bodies: list[bytes] = []
    done = asyncio.Event()

    async def handle(message: AbstractIncomingMessage) -> None:
        bodies.append(message.body)
        if len(bodies) >= 4:
            done.set()
        if message.body == b"poison" and len(bodies) <= 2:
            await message.nack(requeue=True)
        else:
            await message.ack()

    await poison.consume_forever(handle)
    await asyncio.wait_for(done.wait(), 10)
    assert bodies == [b"poison", b"poison", b"poison", b"behind"]


async def test_reject_requeue_spends_the_delivery_limit(poison: Poison) -> None:
    seen = await settle_until(poison, lambda message: message.reject(requeue=True), BROKER_DELIVERIES)
    assert seen[-1] == (LIMIT, LIMIT)
    # The last delivery is still unsettled, so returning it goes over the limit.
    await poison.consumer.close()
    assert await poison.dead_letter_reason() == "delivery_limit"


# A consumer process dying with a delivery unsettled, as an out-of-memory kill would leave it.
CRASHING_CONSUMER = """
import asyncio, json, os, sys
import aio_pika

async def main():
    connection = await aio_pika.connect(sys.argv[1])
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.get_queue(sys.argv[2], ensure=False)
    async def handle(message):
        headers = message.headers or {}
        print(json.dumps([headers.get("x-delivery-count"), headers.get("x-acquired-count")]), flush=True)
        os._exit(9)
    await queue.consume(handle)
    await asyncio.sleep(3)

asyncio.run(main())
"""


async def test_cancelling_then_closing_spends_the_delivery_limit(poison: Poison) -> None:
    """The services' shutdown: stop consuming, leave the delivery unsettled, close."""
    seen: list[tuple[object, object]] = []
    for _ in range(BROKER_DELIVERIES + 1):
        connection = await aio_pika.connect(URL)
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(poison.name, durable=True, arguments=poison.arguments)
        arrived: asyncio.Future[AbstractIncomingMessage] = asyncio.get_running_loop().create_future()

        async def handle(message: AbstractIncomingMessage, arrived: asyncio.Future[AbstractIncomingMessage] = arrived) -> None:
            arrived.set_result(message)

        tag = await queue.consume(handle)
        try:
            message = await asyncio.wait_for(arrived, 2)
        except TimeoutError:
            await connection.close()
            break
        seen.append(counts(message))
        await queue.cancel(tag)
        await connection.close()
    assert len(seen) == BROKER_DELIVERIES
    assert seen[-1] == (LIMIT, LIMIT)
    assert await poison.dead_letter_reason() == "delivery_limit"


@pytest.mark.timeout(90)
async def test_a_forced_connection_close_spends_the_delivery_limit(poison: Poison) -> None:
    """Two cycles show the count moving: the management API lists a new connection in seconds, not at once."""
    seen: list[tuple[object, object]] = []
    async with httpx.AsyncClient(base_url=f"http://{RABBITMQ_HOST}:{MGMT_PORT}", auth=(USER, PASSWORD), timeout=15.0) as client:
        for cycle in range(3):
            label = f"{poison.name}-{cycle}"
            connection = await aio_pika.connect(f"{URL}?name={label}")
            message = await poison.take(connection)
            assert message is not None
            seen.append(counts(message))
            while True:
                listed = [
                    row["name"]
                    for row in (await client.get("/api/connections")).json()
                    if (row.get("client_properties") or {}).get("connection_name") == label
                ]
                if listed:
                    break
                await asyncio.sleep(0.25)
            (await client.delete(f"/api/connections/{quote(listed[0], safe='')}")).raise_for_status()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.close(), 2)
    assert seen == [(None, None), (1, 1), (2, 2)]


async def test_a_killed_consumer_spends_the_delivery_limit(poison: Poison) -> None:
    seen: list[tuple[object, object]] = []
    for _ in range(BROKER_DELIVERIES + 1):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", CRASHING_CONSUMER, URL, poison.name, stdout=asyncio.subprocess.PIPE)
        output, _ = await process.communicate()
        if not output:
            break
        first, second = json.loads(output)
        seen.append((first, second))
    assert len(seen) == BROKER_DELIVERIES
    assert seen[-1] == (LIMIT, LIMIT)
    assert await poison.dead_letter_reason() == "delivery_limit"


@pytest.mark.parametrize("close", ["channel", "connection"])
async def test_every_close_spends_the_delivery_limit(poison: Poison, close: str) -> None:
    seen: list[tuple[object, object]] = []
    for _ in range(BROKER_DELIVERIES + 2):
        connection = await aio_pika.connect(URL)
        message = await poison.take(connection)
        if message is None:
            await connection.close()
            break
        seen.append(counts(message))
        if close == "channel":
            await message.channel.close()
        await connection.close()
    assert len(seen) == BROKER_DELIVERIES
    assert seen[-1] == (LIMIT, LIMIT)
    assert await poison.dead_letter_reason() == "delivery_limit"


@pytest.mark.parametrize("poison", [{"x-consumer-timeout": 1000}], indirect=True)
@pytest.mark.timeout(150)
async def test_consumer_timeout_returns_the_message_without_spending_the_limit(poison: Poison) -> None:
    """The broker cancels the consumer and requeues, like a nack: a timed-out hold is uncounted."""
    connection = await aio_pika.connect(URL)
    message = await poison.take(connection)
    assert message is not None
    assert counts(message) == (None, None)
    second = await asyncio.wait_for(_redelivered(poison), 120)
    assert counts(second) == (None, 1)
    assert not message.channel.is_closed
    await connection.close()


async def _redelivered(poison: Poison) -> AbstractIncomingMessage:
    connection = await aio_pika.connect(URL)
    while True:
        message = await poison.take(connection)
        if message is not None:
            return message


async def test_counts_are_separate_and_the_limit_sees_only_closes(poison: Poison) -> None:
    connection = await aio_pika.connect(URL)
    await settle_until_closed(poison, connection, nacks=9)
    seen: list[tuple[object, object]] = []
    for _ in range(BROKER_DELIVERIES + 2):
        message = await poison.take(connection)
        if message is None:
            break
        seen.append(counts(message))
        await message.channel.close()
    await connection.close()
    assert seen[0] == (1, 10)
    assert seen[-1] == (LIMIT, 29)
    assert await poison.dead_letter_reason() == "delivery_limit"


async def settle_until_closed(poison: Poison, connection: aio_pika.abc.AbstractConnection, *, nacks: int) -> None:
    """Nack ``nacks`` times, then close the channel on the delivery after them."""
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue(poison.name, durable=True, arguments=poison.arguments)
    seen = 0
    done = asyncio.Event()

    async def handle(message: AbstractIncomingMessage) -> None:
        nonlocal seen
        seen += 1
        if seen > nacks:
            done.set()
            return
        await message.nack(requeue=True)

    await queue.consume(handle)
    await asyncio.wait_for(done.wait(), 10)
    await channel.close()


class Transient:
    def __call__(self, _error: BaseException) -> FailureKind:
        return FailureKind.TRANSIENT


class Observer:
    def __init__(self) -> None:
        self.settled_results: list[DeliveryResult] = []

    def consume(self, _destination: str, _headers: object | None) -> nullcontext[None]:
        return nullcontext()

    def flush(self, _key: str, _size: int, _links: Sequence[object]) -> nullcontext[None]:
        return nullcontext()

    def retry(self, **_kwargs: object) -> None:
        return None

    def settled(self, *, result: DeliveryResult, **_kwargs: object) -> None:
        self.settled_results.append(result)


async def failing() -> DeliveryResult:
    raise ConnectionError("still failing")


async def test_a_failing_message_dead_letters_at_the_default_bound(poison: Poison) -> None:
    """A real broker and the shipped number: exactly budget + 1 deliveries, then the DLQ."""
    observer = Observer()
    deliveries = 0
    done = asyncio.Event()

    async def handle(message: AbstractIncomingMessage) -> None:
        nonlocal deliveries
        deliveries += 1
        result = await run_delivery(
            message, failing, classifier=Transient(), observer=observer, destination="q", entity="e", max_requeues=DEFAULT_MAX_REQUEUES
        )
        if result.settlement is Settlement.REJECT:
            done.set()

    await poison.consume_forever(handle)
    await asyncio.wait_for(done.wait(), 60)
    assert deliveries == DEFAULT_MAX_REQUEUES + 1
    assert observer.settled_results[-1].outcome == "redelivery_exhausted"
    assert observer.settled_results[-1].error_type == "ConnectionError"
    assert await poison.dead_letter_reason() == "rejected"


async def test_a_wait_that_takes_time_reaches_the_default_budget(poison: Poison, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default, earned by a real sleep; the threshold is lowered only to keep this quick."""
    monkeypatch.setattr("common.delivery.MIN_PACED_WAIT_S", 0.001)
    deliveries = 0
    done = asyncio.Event()

    async def wait() -> None:
        await asyncio.sleep(0.002)

    async def handle(message: AbstractIncomingMessage) -> None:
        nonlocal deliveries
        deliveries += 1
        result = await run_delivery(
            message, failing, classifier=Transient(), observer=Observer(), destination="q", entity="e", wait_before_requeue=wait
        )
        if result.settlement is Settlement.REJECT:
            done.set()

    await poison.consume_forever(handle)
    await asyncio.wait_for(done.wait(), 40)
    assert deliveries == DEFAULT_MAX_REQUEUES + 1
    assert await poison.dead_letter_reason() == "rejected"


async def test_a_no_op_wait_cannot_dead_letter_seconds_into_an_outage(poison: Poison) -> None:
    """The shape t4h measured: a wait that returns at once earns no budget, so the message stays."""
    deliveries = 0
    done = asyncio.Event()
    target = DEFAULT_MAX_REQUEUES * 2

    async def wait() -> None:
        return None

    async def handle(message: AbstractIncomingMessage) -> None:
        nonlocal deliveries
        deliveries += 1
        if deliveries >= target:
            done.set()
            return
        await run_delivery(message, failing, classifier=Transient(), observer=Observer(), destination="q", entity="e", wait_before_requeue=wait)

    await poison.consume_forever(handle)
    await asyncio.wait_for(done.wait(), 60)
    assert await poison.dead_letter_reason(wait_s=1.0) is None


async def test_without_the_bound_the_same_message_never_ends(poison: Poison) -> None:
    """The control: the broker alone lets it pass the budget and keep going."""
    deliveries = 0
    done = asyncio.Event()
    target = DEFAULT_MAX_REQUEUES * 2

    async def handle(message: AbstractIncomingMessage) -> None:
        nonlocal deliveries
        deliveries += 1
        if deliveries >= target:
            done.set()
            return
        await run_delivery(message, failing, classifier=Transient(), observer=Observer(), destination="q", entity="e", max_requeues=None)

    await poison.consume_forever(handle)
    await asyncio.wait_for(done.wait(), 60)
    assert await poison.dead_letter_reason(wait_s=1.0) is None


async def test_closes_and_nacks_both_count_towards_the_budget(poison: Poison) -> None:
    """Closes reach the runtime's count too, so a mixed history cannot outlast the bound."""
    connection = await aio_pika.connect(URL)
    for _ in range(3):
        message = await poison.take(connection)
        assert message is not None
        await message.channel.close()
    message = await poison.take(connection)
    assert message is not None
    assert broker_handouts(message) == 3
    assert await requeue_or_dead_letter(message, max_requeues=3) is False
    await connection.close()
    assert await poison.dead_letter_reason() == "rejected"


class AmqpDelivery:
    """The adapter shape a service needs: settlement plus the broker's headers."""

    def __init__(self, message: AbstractIncomingMessage) -> None:
        self._message = message
        self.headers = message.headers

    async def ack(self) -> None:
        await self._message.ack()

    async def nack(self, *, requeue: bool) -> None:
        await self._message.nack(requeue=requeue)


class DownSink:
    async def write(self, _key: str, _payloads: Sequence[Any]) -> Sequence[Any]:
        raise ConnectionError("database down")


async def test_a_held_delivery_dead_letters_at_the_engine_bound(poison: Poison) -> None:
    """An outage that never ends: the hold, the requeue and the budget all on the real broker."""
    observer = Observer()
    engine: AsyncBatchEngine[str, bytes] = AsyncBatchEngine(
        ["k"],
        policy=BatchPolicy(
            batch_size=1,
            flush_interval_s=0.05,
            max_pending=10,
            max_concurrent_flushes=1,
            min_batch_size=1,
            backoff_initial_s=0.01,
            backoff_max_s=0.02,
            backoff_multiplier=2.0,
            max_drain_retries=1,
            max_poison_retries=1,
            max_hold_s=0.1,
            max_requeues=3,
        ),
        sink=cast("Any", DownSink()),
        classifier=Transient(),
        observer=cast("Any", observer),
    )
    deliveries = 0

    async def handle(message: AbstractIncomingMessage) -> None:
        nonlocal deliveries
        deliveries += 1
        await engine.submit("k", message.body, AmqpDelivery(message))

    periodic = asyncio.create_task(engine.run_periodic())
    await poison.consume_forever(handle)
    started = monotonic()
    while monotonic() - started < 30 and not any(result.outcome == "redelivery_exhausted" for result in observer.settled_results):
        await asyncio.sleep(0.02)
    engine.shutdown()
    await periodic
    assert deliveries == 4
    assert [result.outcome for result in observer.settled_results] == ["held_timeout", "held_timeout", "held_timeout", "redelivery_exhausted"]
    assert await poison.dead_letter_reason() == "rejected"
