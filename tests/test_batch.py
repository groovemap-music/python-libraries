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


def engine(sink: object, observer: Observer | None = None, *, keys: list[str] | None = None, **changes: object):  # type: ignore[no-untyped-def]
    return AsyncBatchEngine(
        keys or ["release"],
        policy=policy(**changes),
        sink=sink,  # type: ignore[arg-type]
        classifier=lambda error: FailureKind.TRANSIENT if isinstance(error, ConnectionError) else FailureKind.DETERMINISTIC,
        observer=observer or Observer(),
    )


class StrictDelivery:
    def __init__(self, *, ack_error: BaseException | None = None, nack_error: BaseException | None = None) -> None:
        self.ack_error = ack_error
        self.nack_error = nack_error
        self.calls: list[str] = []
        self.ack_entered = asyncio.Event()

    async def ack(self) -> None:
        assert not self.calls, "delivery received a second terminal call"
        self.calls.append("ack")
        self.ack_entered.set()
        if self.ack_error is not None:
            raise self.ack_error

    async def nack(self, *, requeue: bool) -> None:
        assert not self.calls, "delivery received a second terminal call"
        self.calls.append(f"nack:{requeue}")
        if self.nack_error is not None:
            raise self.nack_error


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


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [[], [object()]])
async def test_malformed_sink_results_restore_without_classification_or_settlement(results: list[object]) -> None:
    sink = AsyncMock()
    sink.write.return_value = results
    classifier = Mock()
    batch = AsyncBatchEngine(
        ["release"],
        policy=policy(),
        sink=sink,
        classifier=classifier,
        observer=Observer(),  # type: ignore[arg-type]
    )
    delivery = StrictDelivery()
    await batch.submit("release", 1, delivery)
    with pytest.raises((AttributeError, ValueError)):
        await batch.flush("release")
    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]
    assert delivery.calls == []
    classifier.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("settlement", "failure"), [(Settlement.ACK, RuntimeError("ack")), (Settlement.REJECT, RuntimeError("nack"))])
async def test_settlement_failure_retains_only_unsettled_tail(settlement: Settlement, failure: RuntimeError) -> None:
    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "ok"), BatchItemResult(settlement, "failed"), BatchItemResult(Settlement.ACK, "tail")]
    batch = engine(sink)
    deliveries = [StrictDelivery(), StrictDelivery(ack_error=failure, nack_error=failure), StrictDelivery()]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)
    with pytest.raises(RuntimeError, match=str(failure)):
        await batch.flush("release")
    assert deliveries[0].calls == ["ack"]
    assert deliveries[1].calls == [settlement.value if settlement is Settlement.ACK else "nack:False"]
    assert deliveries[2].calls == []
    assert batch.snapshot()["pending"]["release"] == 2  # type: ignore[index]


@pytest.mark.asyncio
async def test_cancellation_during_settlement_retains_only_unsettled_tail() -> None:
    blocker = asyncio.Future[None]()

    class BlockingDelivery(StrictDelivery):
        async def ack(self) -> None:
            assert not self.calls
            self.calls.append("ack")
            self.ack_entered.set()
            await blocker

    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "ok") for _ in range(3)]
    batch = engine(sink)
    deliveries = [StrictDelivery(), BlockingDelivery(), StrictDelivery()]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)
    task = asyncio.create_task(batch.flush("release"))
    await deliveries[1].ack_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert deliveries[0].calls == ["ack"]
    assert deliveries[1].calls == ["ack"]
    assert deliveries[2].calls == []
    assert batch.snapshot()["pending"]["release"] == 2  # type: ignore[index]


@pytest.mark.asyncio
async def test_cancellation_waiting_for_global_flush_capacity_restores_batch() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(key: str, payloads: list[int]):
        if key == "a":
            entered.set()
            await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), keys=["a", "b"])
    await batch.submit("a", 1, StrictDelivery())
    waiting = StrictDelivery()
    await batch.submit("b", 2, waiting)
    first = asyncio.create_task(batch.flush("a"))
    await entered.wait()
    second = asyncio.create_task(batch.flush("b"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert batch.snapshot()["pending"]["b"] == 1  # type: ignore[index]
    assert waiting.calls == []
    release.set()
    await first


@pytest.mark.asyncio
async def test_same_key_serializes_and_different_keys_obey_global_limit() -> None:
    active = 0
    maximum = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def write(_key: str, payloads: list[int]):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        await release.wait()
        active -= 1
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), keys=["a", "b"], batch_size=1, max_concurrent_flushes=1)
    for key in ("a", "a", "b"):
        await batch.submit(key, 1, StrictDelivery())
    tasks = [asyncio.create_task(batch.flush(key)) for key in ("a", "a", "b")]
    await entered.wait()
    await asyncio.sleep(0)
    assert maximum == 1
    release.set()
    assert await asyncio.gather(*tasks) == [True, True, True]
    assert maximum == 1


@pytest.mark.asyncio
async def test_different_keys_use_available_concurrency() -> None:
    active = 0
    maximum = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def write(_key: str, payloads: list[int]):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            both_entered.set()
        await release.wait()
        active -= 1
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), keys=["a", "b"], max_concurrent_flushes=2)
    await batch.submit("a", 1, StrictDelivery())
    await batch.submit("b", 2, StrictDelivery())
    tasks = [asyncio.create_task(batch.flush(key)) for key in ("a", "b")]
    await asyncio.wait_for(both_entered.wait(), timeout=0.1)
    assert maximum == 2
    release.set()
    assert await asyncio.gather(*tasks) == [True, True]


@pytest.mark.asyncio
async def test_backpressure_releases_capacity_once_after_settlement() -> None:
    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "ok")]
    batch = engine(sink, max_pending=1, batch_size=1)
    await batch.submit("release", 1, StrictDelivery())
    second = asyncio.create_task(batch.submit("release", 2, StrictDelivery()))
    await asyncio.sleep(0)
    assert not second.done()
    await batch.flush("release")
    await asyncio.wait_for(second, timeout=0.1)
