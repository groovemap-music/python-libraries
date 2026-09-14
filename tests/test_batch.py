from __future__ import annotations

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest

from common.batch import AsyncBatchEngine, BatchItemResult, BatchPolicy
from common.delivery import FailureKind, Settlement


def policy(**changes: object):  # type: ignore[no-untyped-def]
    values = {
        "batch_size": 4,
        "flush_interval_s": 0.01,
        "max_pending": 20,
        "max_concurrent_flushes": 1,
        "min_batch_size": 1,
        "backoff_initial_s": 0.01,
        "backoff_max_s": 1.0,
        "backoff_multiplier": 2.0,
        "max_drain_retries": 3,
        "max_poison_retries": 3,
    }
    values.update(changes)
    return BatchPolicy(**values)  # type: ignore[arg-type]


class Observer:
    def __init__(self) -> None:
        self.links: list[object] = []
        self.retry = Mock()
        self.settled = Mock()

    def consume(self, _destination: str, _headers: object | None):  # type: ignore[no-untyped-def]
        return nullcontext()

    def flush(self, _key: str, _size: int, links: list[object]):  # type: ignore[no-untyped-def]
        self.links = list(links)
        return nullcontext("span")


def engine(sink: object, observer: Observer | None = None, **changes: object):  # type: ignore[no-untyped-def]
    return AsyncBatchEngine(
        ["release"],
        policy=policy(**changes),
        sink=sink,  # type: ignore[arg-type]
        classifier=lambda error: FailureKind.TRANSIENT if isinstance(error, ConnectionError) else FailureKind.DETERMINISTIC,
        observer=observer or Observer(),
    )


@pytest.mark.asyncio
async def test_success_settles_each_delivery_from_owner_results() -> None:
    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "stored"), BatchItemResult(Settlement.REJECT, "invalid")]
    batch = engine(sink)
    first, second = AsyncMock(), AsyncMock()
    await batch.submit("release", 1, first)
    await batch.submit("release", 2, second)

    assert await batch.flush("release") is True
    first.ack.assert_awaited_once()
    second.nack.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
async def test_transient_failure_retains_order_without_settlement_or_poison() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    batch = engine(sink)
    deliveries = [AsyncMock() for _ in range(3)]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)

    assert await batch.flush("release") is False
    assert batch.snapshot()["pending"]["release"] == 3  # type: ignore[index]
    assert batch.snapshot()["poison_attempts"]["release"] == 0  # type: ignore[index]
    assert [call.args[1] for call in sink.write.await_args_list] == [[0, 1, 2]]
    assert all(delivery.mock_calls == [] for delivery in deliveries)


@pytest.mark.asyncio
async def test_deterministic_failure_shrinks_then_rejects_poison_in_order() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ValueError("bad")
    batch = engine(sink, max_poison_retries=3, max_drain_retries=3)
    deliveries = [AsyncMock() for _ in range(3)]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)

    assert await batch.flush("release") is True
    assert [call.args[1] for call in sink.write.await_args_list[:3]] == [[0, 1, 2], [0, 1], [0]]
    assert all(delivery.nack.await_count == 1 for delivery in deliveries)


@pytest.mark.asyncio
async def test_bounded_give_up_retains_work() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ValueError("bad")
    batch = engine(sink, max_poison_retries=10, max_drain_retries=2)
    delivery = AsyncMock()
    await batch.submit("release", 1, delivery)
    assert await batch.flush("release") is False
    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]
    delivery.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_while_holding_capacity_restores_original_order() -> None:
    entered = asyncio.Event()

    async def write(_key: str, _payloads: list[int]):
        entered.set()
        await asyncio.Future()

    sink = Mock(write=write)
    batch = engine(sink)
    deliveries = [AsyncMock() for _ in range(3)]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)
    task = asyncio.create_task(batch.flush("release"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert batch.snapshot()["pending"]["release"] == 3  # type: ignore[index]
    assert all(delivery.mock_calls == [] for delivery in deliveries)


@pytest.mark.asyncio
async def test_observers_are_isolated_and_links_are_capped() -> None:
    observer = Observer()
    observer.settled.side_effect = RuntimeError("metrics")
    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "ok") for _ in range(70)]
    batch = engine(sink, observer, batch_size=70, max_pending=70)
    deliveries = [AsyncMock() for _ in range(70)]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery, span_context=value)
    assert await batch.flush("release") is True
    assert len(observer.links) == 64
    assert all(delivery.ack.await_count == 1 for delivery in deliveries)


@pytest.mark.asyncio
async def test_periodic_path_retries_retained_transient_work() -> None:
    sink = AsyncMock()
    sink.write.side_effect = [ConnectionError("down"), [BatchItemResult(Settlement.ACK, "ok")]]
    batch = engine(sink, backoff_initial_s=0.001)
    delivery = AsyncMock()
    await batch.submit("release", 1, delivery)
    assert await batch.flush("release") is False
    task = asyncio.create_task(batch.run_periodic())
    for _ in range(20):
        if delivery.ack.await_count:
            break
        await asyncio.sleep(0.01)
    batch.shutdown()
    await task
    delivery.ack.assert_awaited_once()
