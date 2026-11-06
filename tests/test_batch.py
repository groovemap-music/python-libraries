from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import common.batch as batch_module
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
async def test_failed_settlement_leaves_that_delivery_to_the_broker_and_settles_the_rest(settlement: Settlement, failure: RuntimeError) -> None:
    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "ok"), BatchItemResult(settlement, "failed"), BatchItemResult(Settlement.ACK, "tail")]
    observer = Observer()
    batch = engine(sink, observer, max_pending=3)
    deliveries = [StrictDelivery(), StrictDelivery(ack_error=failure, nack_error=failure), StrictDelivery()]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)

    assert await batch.flush("release") is True

    assert deliveries[0].calls == ["ack"]
    assert deliveries[1].calls == [settlement.value if settlement is Settlement.ACK else "nack:False"]
    assert deliveries[2].calls == ["ack"]
    state = batch.snapshot()
    assert state["pending"]["release"] == 0  # type: ignore[index]
    assert state["lost_deliveries"]["release"] == 1  # type: ignore[index]
    sink.write.assert_awaited_once()
    reported = [call.kwargs["result"] for call in observer.settled.call_args_list]
    assert [result.settlement for result in reported].count(Settlement.REQUEUE) == 1
    assert next(result for result in reported if result.settlement is Settlement.REQUEUE).outcome == "delivery_lost"
    # A lost delivery returns its capacity: the broker's redelivery of it can be submitted at once.
    for value in range(3):
        await asyncio.wait_for(batch.submit("release", value, StrictDelivery()), timeout=0.1)


@pytest.mark.asyncio
async def test_a_whole_batch_from_a_dead_channel_frees_the_engine() -> None:
    """Every delivery of a closed channel fails to settle; none may stay to fail again."""
    writes: list[list[int]] = []

    async def write(_key: str, payloads: list[int]):
        writes.append(list(payloads))
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), max_pending=4, batch_size=2)
    dead = [StrictDelivery(ack_error=RuntimeError("channel closed")) for _ in range(4)]
    for value, delivery in enumerate(dead):
        await batch.submit("release", value, delivery)

    assert await batch.flush("release") is True
    assert await batch.flush("release") is True

    assert writes == [[0, 1], [2, 3]]
    assert batch.snapshot()["pending"]["release"] == 0  # type: ignore[index]
    assert batch.snapshot()["lost_deliveries"]["release"] == 4  # type: ignore[index]


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


@pytest.mark.asyncio
async def test_batch_enqueue_needs_no_write_slot_and_cancelled_flush_retains_work() -> None:
    from common import WriteAdmission

    admission = WriteAdmission(1)
    attempted = asyncio.Event()
    entered = asyncio.Event()

    async def write(_key: str, payloads: list[int]):
        attempted.set()
        async with admission.slot():
            entered.set()
            return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), max_pending=1, batch_size=1)
    delivery = StrictDelivery()
    async with admission.slot():
        # A direct writer holds the entire DB budget. Enqueue still completes;
        # only an actual sink write waits, without changing batch ownership.
        await batch.submit("release", 1, delivery)
        flush = asyncio.create_task(batch.flush("release"))
        await attempted.wait()
        assert not entered.is_set()
        assert delivery.calls == []
        flush.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flush
        assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]
        assert delivery.calls == []
    assert await batch.flush("release") is True
    assert entered.is_set()
    assert delivery.calls == ["ack"]
    assert batch.snapshot()["pending"]["release"] == 0  # type: ignore[index]
    # Settlement also returned the independent pending capacity.
    await batch.submit("release", 2, StrictDelivery())
    assert await batch.flush("release") is True


@pytest.mark.asyncio
async def test_max_hold_requeues_deliveries_held_past_the_bound_during_outage() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    observer = Observer()
    batch = engine(sink, observer, max_hold_s=0.05, backoff_initial_s=0.01, backoff_max_s=0.01)
    deliveries = [StrictDelivery() for _ in range(3)]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)

    assert await batch.flush("release") is False
    assert all(delivery.calls == [] for delivery in deliveries)

    await asyncio.sleep(0.06)
    # The queue is empty, but the records are unwritten on the broker, so the drain is not complete.
    assert await batch.flush("release") is False

    # Nothing is lost or acked: every delivery goes back to the broker, in order, once.
    assert [delivery.calls for delivery in deliveries] == [["nack:True"]] * 3
    assert batch.snapshot()["pending"]["release"] == 3  # type: ignore[index]
    assert batch.snapshot()["returning"]["release"] == 3  # type: ignore[index]
    outcomes = [call.kwargs["result"] for call in observer.settled.call_args_list]
    assert [(r.settlement, r.outcome) for r in outcomes] == [(Settlement.REQUEUE, "held_timeout")] * 3
    # Capacity is returned: a full max_pending of new work is accepted.
    for value in range(20):
        await asyncio.wait_for(batch.submit("release", value, StrictDelivery()), timeout=1)


@pytest.mark.asyncio
async def test_max_hold_leaves_young_deliveries_and_default_is_unbounded() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    for limit in (None, 60.0):
        batch = engine(sink, max_hold_s=limit)
        delivery = StrictDelivery()
        await batch.submit("release", 1, delivery)
        await batch.flush("release")
        await asyncio.sleep(0.02)
        await batch.flush("release")
        assert delivery.calls == []
        assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_max_hold_leaves_a_delivery_to_the_broker_when_requeue_fails() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    batch = engine(sink, max_hold_s=0.01)
    failing = StrictDelivery(nack_error=RuntimeError("channel closed"))
    survivor = StrictDelivery()
    await batch.submit("release", 1, failing)
    await batch.submit("release", 2, survivor)
    await batch.flush("release")
    await asyncio.sleep(0.02)

    assert await batch.flush("release") is False

    assert failing.calls == ["nack:True"]
    assert survivor.calls == ["nack:True"]
    # The broker holds the lost one too, so it comes back like the one that was nacked.
    assert batch.snapshot()["pending"]["release"] == 2  # type: ignore[index]
    assert batch.snapshot()["lost_deliveries"]["release"] == 1  # type: ignore[index]


async def held_past_the_bound(batch: AsyncBatchEngine[str, int], *deliveries: StrictDelivery) -> None:
    """Fail one write with ``deliveries`` queued, then let them age past ``max_hold_s``."""
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)
    assert await batch.flush("release") is False
    await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_returned_deliveries_stay_pending_past_the_drain_that_released_them() -> None:
    """The next drain can start the moment this one ends, with the records still on the wire."""
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    batch = engine(sink, max_hold_s=0.01, backoff_initial_s=0.001, backoff_max_s=0.001)
    await held_past_the_bound(batch, StrictDelivery(), StrictDelivery())

    assert await batch.flush("release") is False, "the drain that released them"
    assert await batch.flush("release") is False, "the drain behind it, with an empty queue"

    # Redelivered, one at a time, and the store is back: only the last write completes the key.
    sink.write.side_effect = lambda _key, payloads: [BatchItemResult(Settlement.ACK, "processed") for _ in payloads]
    await asyncio.sleep(0.01)
    await batch.submit("release", 0, StrictDelivery())
    assert await batch.flush("release") is False, "one record is back, the other is still out"
    await batch.submit("release", 1, StrictDelivery())
    assert await batch.flush("release") is True
    assert batch.snapshot()["returning"]["release"] == 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_return_that_never_arrives_stops_counting_after_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler can discard a redelivery, and a marker must not wait on it forever."""
    monkeypatch.setattr(batch_module, "RETURN_WINDOW_S", 0.02)
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    batch = engine(sink, max_hold_s=0.01)
    await held_past_the_bound(batch, StrictDelivery())

    assert await batch.flush("release") is False
    await asyncio.sleep(0.03)

    assert await batch.flush("release") is True
    assert batch.snapshot()["returning"]["release"] == 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_dead_lettered_delivery_is_not_expected_back() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    batch = engine(sink, max_hold_s=0.01, max_requeues=1)
    spent = StrictDelivery()
    spent.headers = {"x-acquired-count": 1}  # type: ignore[attr-defined]
    await held_past_the_bound(batch, spent)

    assert await batch.flush("release") is True

    assert spent.calls == ["nack:False"]
    assert batch.snapshot()["returning"]["release"] == 0  # type: ignore[index]


def ok(_key: str, payloads: list[int]) -> list[BatchItemResult]:
    return [BatchItemResult(Settlement.ACK, "processed") for _ in payloads]


@pytest.mark.asyncio
async def test_a_delivery_past_capacity_goes_back_without_waiting_for_a_slot() -> None:
    """The broker's timer runs while it waits. Nothing frees a slot during an outage but the hold."""
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    observer = Observer()
    batch = engine(sink, observer, max_pending=1, max_hold_s=0.05)
    held, waiting = StrictDelivery(), StrictDelivery()
    await batch.submit("release", 1, held)
    assert await batch.flush("release") is False

    await asyncio.wait_for(batch.submit("release", 2, waiting), timeout=1)

    assert waiting.calls == ["nack:True"], "returned from submit, with the slot still taken"
    assert held.calls == []
    assert batch.snapshot()["returning"]["release"] == 1  # type: ignore[index]
    assert observer.settled.call_args.kwargs["result"].outcome == "held_timeout"
    assert await asyncio.wait_for(batch.flush("release"), timeout=1) is False, "it is still owed"


@pytest.mark.asyncio
async def test_a_delivery_past_capacity_spends_its_budget_and_a_lost_one_is_accounted_for() -> None:
    sink = AsyncMock()
    sink.write.side_effect = ConnectionError("down")
    batch = engine(sink, max_pending=1, max_hold_s=0.01, max_requeues=1)
    await batch.submit("release", 1, StrictDelivery())
    spent = StrictDelivery()
    spent.headers = {"x-acquired-count": 1}  # type: ignore[attr-defined]
    await batch.submit("release", 2, spent)
    assert spent.calls == ["nack:False"]
    assert batch.snapshot()["returning"]["release"] == 0  # type: ignore[index]

    gone = StrictDelivery(nack_error=RuntimeError("channel closed"))
    await batch.submit("release", 3, gone)
    assert batch.snapshot()["lost_deliveries"]["release"] == 1  # type: ignore[index]
    assert batch.snapshot()["returning"]["release"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_cancelled_handler_waiting_for_capacity_leaves_its_delivery_to_the_broker() -> None:
    sink = AsyncMock()
    batch = engine(sink, max_pending=1, max_hold_s=30.0)
    await batch.submit("release", 1, StrictDelivery())
    waiting = StrictDelivery()
    submit = asyncio.create_task(batch.submit("release", 2, waiting))
    await asyncio.sleep(0)

    submit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit

    assert waiting.calls == []
    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_time_waiting_for_capacity_counts_toward_the_hold() -> None:
    """Otherwise a delivery that gets its slot late starts a second hold on the broker's one timer."""
    gate = asyncio.Event()

    async def write(_key: str, payloads: list[int]) -> list[BatchItemResult]:
        await gate.wait()
        return ok(_key, payloads)

    sink = Mock(write=write)
    batch = engine(sink, max_pending=1, max_hold_s=1.0)
    await batch.submit("release", 1, StrictDelivery())
    drain = asyncio.create_task(batch.flush("release"))
    await asyncio.sleep(0.01)
    submit = asyncio.create_task(batch.submit("release", 2, StrictDelivery()))
    await asyncio.sleep(0.05)

    gate.set()
    await asyncio.wait_for(asyncio.gather(drain, submit), timeout=1)

    assert batch.snapshot()["oldest_pending_age_s"]["release"] >= 0.05  # type: ignore[index]


@pytest.mark.asyncio
async def test_waiting_for_capacity_warns_once_that_max_pending_is_too_small(caplog: pytest.LogCaptureFixture) -> None:
    batch = engine(AsyncMock(), max_pending=1, max_hold_s=0.01)
    batch._flush_capacity = asyncio.Semaphore(0)
    await batch.submit("release", 1, StrictDelivery())
    with caplog.at_level("WARNING", logger="common.batch"):
        await batch.submit("release", 2, StrictDelivery())
        await batch.submit("release", 3, StrictDelivery())

    assert sum("max_pending=1" in message for message in caplog.messages) == 1


@pytest.mark.asyncio
async def test_a_working_key_returns_what_it_cannot_reach_within_the_hold_and_writes_the_rest() -> None:
    """A store slower than the hold: writes succeed, so no backoff ever opens the old gate."""
    written: list[int] = []

    async def write(_key: str, payloads: list[int]) -> list[BatchItemResult]:
        await asyncio.sleep(0.05)
        written.extend(payloads)
        return ok(_key, payloads)

    batch = engine(Mock(write=write), batch_size=2, max_hold_s=0.03)
    deliveries = [StrictDelivery() for _ in range(6)]
    for value, delivery in enumerate(deliveries):
        await batch.submit("release", value, delivery)

    assert await batch.flush("release") is False

    assert written == [0, 1, 2, 3], "the batch about to be written is spared every time, so it makes progress"
    assert [delivery.calls for delivery in deliveries[:4]] == [["ack"]] * 4
    assert [delivery.calls for delivery in deliveries[4:]] == [["nack:True"]] * 2


class HungSink:
    """A store that stops answering: raises nothing and returns nothing until told to."""

    def __init__(self, *, ignores_cancel: bool = False) -> None:
        self.ignores_cancel = ignores_cancel
        self.answer = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = 0
        self.hung = True

    async def write(self, _key: str, payloads: list[int]) -> list[BatchItemResult]:
        if self.hung:
            self.started.set()
            while not self.answer.is_set():
                try:
                    await self.answer.wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    if not self.ignores_cancel:
                        raise
        return [BatchItemResult(Settlement.ACK, "processed") for _ in payloads]


@pytest.mark.asyncio
async def test_a_hung_write_behaves_like_a_failed_write() -> None:
    sink = HungSink()
    observer = Observer()
    # The classifier in engine() calls anything but ConnectionError deterministic. A hang is not.
    batch = engine(sink, observer, max_write_s=0.01, max_hold_s=0.05, backoff_initial_s=0.001, backoff_max_s=0.001, max_poison_retries=1)
    delivery = StrictDelivery()
    await batch.submit("release", 1, delivery)

    assert await asyncio.wait_for(batch.flush("release"), timeout=2) is False

    assert sink.cancelled == 1
    assert delivery.calls == []
    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]
    assert batch.snapshot()["transient_attempts"]["release"] == 1  # type: ignore[index]
    assert observer.retry.call_args.kwargs["kind"] is FailureKind.TRANSIENT
    # Which is what hands the hold its chance: the same hang now ends in a release.
    await asyncio.sleep(0.06)
    assert await asyncio.wait_for(batch.flush("release"), timeout=2) is False
    assert delivery.calls == ["nack:True"]


@pytest.mark.asyncio
async def test_a_write_that_ignores_cancellation_is_left_behind_and_the_key_moves_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PostgreSQL rollback on a frozen server outlives the cancel, so the bound cannot wait for it."""
    monkeypatch.setattr(batch_module, "WRITE_CANCEL_GRACE_S", 0.01)
    sink = HungSink(ignores_cancel=True)
    batch = engine(sink, max_write_s=0.01)
    await batch.submit("release", 1, StrictDelivery())

    assert await asyncio.wait_for(batch.flush("release"), timeout=2) is False

    assert batch.snapshot()["abandoned_writes"]["release"] == 1  # type: ignore[index]
    sink.answer.set()
    await asyncio.sleep(0.01)
    assert batch.snapshot()["abandoned_writes"]["release"] == 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_write_that_finishes_after_its_cancel_is_still_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its rows are stored, so failing it would only write them again."""
    monkeypatch.setattr(batch_module, "WRITE_CANCEL_GRACE_S", 0.5)
    sink = HungSink(ignores_cancel=True)
    batch = engine(sink, max_write_s=0.01)
    delivery = StrictDelivery()
    await batch.submit("release", 1, delivery)
    flush = asyncio.create_task(batch.flush("release"))
    await asyncio.wait_for(sink.started.wait(), timeout=1)
    while sink.cancelled == 0:
        await asyncio.sleep(0.001)

    sink.answer.set()

    assert await asyncio.wait_for(flush, timeout=2) is True
    assert delivery.calls == ["ack"]


@pytest.mark.asyncio
async def test_cancelling_a_flush_cancels_its_bounded_write_and_keeps_the_work() -> None:
    sink = HungSink()
    batch = engine(sink, max_write_s=30.0)
    delivery = StrictDelivery()
    await batch.submit("release", 1, delivery)
    flush = asyncio.create_task(batch.flush("release"))
    await asyncio.wait_for(sink.started.wait(), timeout=1)

    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(flush, timeout=2)

    assert sink.cancelled == 1
    assert delivery.calls == []
    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_max_write_none_keeps_the_unbounded_write() -> None:
    sink = HungSink()
    batch = engine(sink, max_write_s=None)
    await batch.submit("release", 1, StrictDelivery())
    flush = asyncio.create_task(batch.flush("release"))
    await asyncio.wait_for(sink.started.wait(), timeout=1)
    await asyncio.sleep(0.03)
    assert not flush.done()

    sink.answer.set()

    assert await asyncio.wait_for(flush, timeout=1) is True


def test_writes_are_bounded_by_default_and_fit_under_the_consumer_timeout() -> None:
    """The 1500 s hold the services run with, checked against RabbitMQ's 1800 s."""
    bounded = policy(max_hold_s=1500.0, flush_interval_s=5.0, max_concurrent_flushes=2)
    assert bounded.max_write_s == batch_module.DEFAULT_MAX_WRITE_S
    assert bounded.max_write_s is not None
    assert bounded.max_hold_s is not None
    assert bounded.max_hold_s + 2 * (bounded.max_write_s + batch_module.WRITE_CANCEL_GRACE_S) + bounded.flush_interval_s < 1800.0


@pytest.mark.parametrize("value", [0, -1, True])
def test_max_write_must_be_positive(value: object) -> None:
    with pytest.raises(ValueError, match="max_write_s"):
        policy(max_write_s=value)


@pytest.mark.parametrize("value", [0, -1.0, True])
def test_max_hold_must_be_positive_when_set(value: object) -> None:
    with pytest.raises(ValueError, match="max_hold_s"):
        policy(max_hold_s=value)


class Backlog:
    """A sink for one key under constant backlog: every write of ``busy`` submits another batch."""

    def __init__(self, batch: AsyncBatchEngine[str, int], *, busy: str = "release") -> None:
        self.batch = batch
        self.busy = busy
        self.written: dict[str, list[int]] = {}

    async def write(self, key: str, payloads: list[int]):
        self.written.setdefault(key, []).extend(payloads)
        await asyncio.sleep(0)
        if key == self.busy:
            for value in payloads:
                await self.batch.submit(key, value + 1000, StrictDelivery())
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]


def backlogged(**changes: object) -> tuple[AsyncBatchEngine[str, int], Backlog]:
    holder: list[Backlog] = []

    class Sink:
        async def write(self, key: str, payloads: list[int]):
            return await holder[0].write(key, payloads)

    batch = engine(Sink(), keys=["release", "artist"], max_concurrent_flushes=2, **changes)
    holder.append(Backlog(batch))
    return batch, holder[0]


@pytest.mark.asyncio
async def test_flush_returns_under_constant_backlog_having_settled_what_it_found() -> None:
    batch, sink = backlogged(batch_size=2, max_pending=1000)
    await batch.submit("release", 1, StrictDelivery())
    await batch.submit("release", 2, StrictDelivery())

    # Returned, so nothing starves, and False, because a backlog is not "nothing pending".
    assert await asyncio.wait_for(batch.flush("release"), timeout=1.0) is False

    assert sink.written["release"][:2] == [1, 2]
    assert batch.snapshot()["pending"]["release"] > 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_periodic_tail_flushes_while_another_key_stays_backlogged() -> None:
    batch, sink = backlogged(batch_size=4, max_pending=100000, flush_interval_s=0.01)
    tail = StrictDelivery()
    await batch.submit("artist", 7, tail)
    await batch.submit("release", 1, StrictDelivery())
    periodic = asyncio.create_task(batch.run_periodic())

    await asyncio.wait_for(tail.ack_entered.wait(), timeout=1.0)

    assert sink.written["artist"] == [7]
    assert batch.snapshot()["pending"]["release"] > 0  # type: ignore[index]
    batch.shutdown()
    await asyncio.wait_for(periodic, timeout=1.0)


@pytest.mark.asyncio
async def test_handlers_share_the_next_drain_instead_of_queueing_one_each() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    writes: list[list[int]] = []

    async def write(_key: str, payloads: list[int]):
        writes.append(list(payloads))
        entered.set()
        await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), max_pending=1000)
    await batch.submit("release", 0, StrictDelivery())
    running = asyncio.create_task(batch.flush("release"))
    await entered.wait()
    waiters = []
    for value in range(1, 201):
        await batch.submit("release", value, StrictDelivery())
        waiters.append(asyncio.create_task(batch.flush("release")))
    await asyncio.sleep(0)

    # One running drain and one waiting: not a task on the key's lock for every handler.
    assert len(asyncio.all_tasks()) - len(waiters) == 5  # this test, running, its drain, its write, the next drain

    release.set()
    assert await running is False  # the next drain's work was still queued when it ended
    assert await asyncio.gather(*waiters) == [True] * len(waiters)
    assert [value for batch_written in writes for value in batch_written] == list(range(201))
    assert batch.snapshot()["pending"]["release"] == 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_caller_joining_a_running_drain_is_covered_by_the_next_one() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(_key: str, payloads: list[int]):
        entered.set()
        await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write))
    await batch.submit("release", 1, StrictDelivery())
    first = asyncio.create_task(batch.flush("release"))
    await entered.wait()
    late = StrictDelivery()
    await batch.submit("release", 2, late)
    second = asyncio.create_task(batch.flush("release"))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second) == [False, True]
    assert late.calls == ["ack"]


@pytest.mark.asyncio
async def test_cancelling_one_waiter_leaves_a_shared_drain_running() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(_key: str, payloads: list[int]):
        entered.set()
        await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write))
    delivery = StrictDelivery()
    await batch.submit("release", 1, delivery)
    cancelled = asyncio.create_task(batch.flush("release"))
    kept = asyncio.create_task(batch.flush("release"))
    await entered.wait()

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()

    assert await kept is True
    assert delivery.calls == ["ack"]


@pytest.mark.asyncio
async def test_a_new_caller_does_not_join_a_drain_being_cancelled() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def write(_key: str, payloads: list[int]):
        entered.set()
        await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write))
    await batch.submit("release", 1, StrictDelivery())
    running = asyncio.create_task(batch.flush("release"))
    await entered.wait()
    queued = StrictDelivery()
    await batch.submit("release", 2, queued)
    waiting = asyncio.create_task(batch.flush("release"))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.sleep(0)
    replacement = asyncio.create_task(batch.flush("release"))
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()

    assert await asyncio.gather(running, replacement) == [False, True]
    assert queued.calls == ["ack"]


@pytest.mark.asyncio
async def test_flush_still_reports_a_drain_failure_to_its_caller() -> None:
    sink = AsyncMock()
    sink.write.return_value = []
    batch = engine(sink)
    await batch.submit("release", 1, StrictDelivery())

    with pytest.raises(ValueError, match="exactly one result"):
        await batch.flush("release")

    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_flush_failure_does_not_end_periodic_flushing(caplog: pytest.LogCaptureFixture) -> None:
    sink = AsyncMock()
    sink.write.side_effect = [[], [BatchItemResult(Settlement.ACK, "ok")]]
    batch = engine(sink)
    delivery = StrictDelivery()
    await batch.submit("release", 1, delivery)
    periodic = asyncio.create_task(batch.run_periodic())

    await asyncio.wait_for(delivery.ack_entered.wait(), timeout=1.0)

    assert not periodic.done()
    assert batch.snapshot()["periodic_failures"]["release"] == 1  # type: ignore[index]
    assert "Periodic flush of release failed" in caplog.text
    batch.shutdown()
    await asyncio.wait_for(periodic, timeout=1.0)


@pytest.mark.asyncio
async def test_a_failing_key_does_not_stop_another_keys_ticks() -> None:
    async def write(key: str, payloads: list[int]):
        if key == "release":
            raise ValueError("boom")
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), keys=["release", "artist"], max_poison_retries=1000000, max_drain_retries=1)
    await batch.submit("release", 1, StrictDelivery())
    periodic = asyncio.create_task(batch.run_periodic())
    await asyncio.sleep(0.03)
    tail = StrictDelivery()
    await batch.submit("artist", 2, tail)

    await asyncio.wait_for(tail.ack_entered.wait(), timeout=1.0)

    batch.shutdown()
    await asyncio.wait_for(periodic, timeout=1.0)


@pytest.mark.asyncio
async def test_cancelling_run_periodic_stops_every_tick() -> None:
    batch = engine(AsyncMock(), keys=["release", "artist"], flush_interval_s=60.0)
    periodic = asyncio.create_task(batch.run_periodic())
    await asyncio.sleep(0)
    periodic.cancel()
    with pytest.raises(asyncio.CancelledError):
        await periodic
    assert len(asyncio.all_tasks()) == 1


@pytest.mark.asyncio
async def test_snapshot_reports_how_long_the_oldest_delivery_has_waited() -> None:
    batch = engine(AsyncMock(), keys=["release", "artist"])
    await batch.submit("release", 1, StrictDelivery())
    await asyncio.sleep(0.01)

    ages = batch.snapshot()["oldest_pending_age_s"]  # type: ignore[index]

    assert ages["release"] >= 0.01
    assert ages["artist"] == 0.0


async def _settled_write(_key: str, payloads: list[int]) -> list[BatchItemResult]:
    await asyncio.sleep(0)
    return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]


@pytest.mark.asyncio
async def test_flush_true_covers_handlers_still_waiting_to_enter_the_queue() -> None:
    """A purge is gated on True: it must not arrive with records still inside submit."""
    written: list[int] = []

    async def write(key: str, payloads: list[int]):
        written.extend(payloads)
        return await _settled_write(key, payloads)

    batch = engine(Mock(write=write), batch_size=2, max_pending=2)
    handlers = [asyncio.create_task(batch.submit("release", value, StrictDelivery())) for value in range(20)]
    await asyncio.sleep(0)

    assert await asyncio.wait_for(batch.flush("release"), timeout=1.0) is True

    assert sorted(written) == list(range(20))
    assert batch.snapshot()["pending"]["release"] == 0  # type: ignore[index]
    await asyncio.gather(*handlers)


@pytest.mark.asyncio
async def test_flush_covers_a_handler_blocked_by_another_keys_capacity() -> None:
    release = asyncio.Event()

    async def write(key: str, payloads: list[int]):
        if key == "release":
            await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), keys=["release", "artist"], batch_size=1, max_pending=1, max_concurrent_flushes=2, flush_interval_s=5.0)
    await batch.submit("release", 1, StrictDelivery())
    other = asyncio.create_task(batch.flush("release"))
    await asyncio.sleep(0.01)
    waiting = StrictDelivery()
    arriving = asyncio.create_task(batch.submit("artist", 2, waiting))
    await asyncio.sleep(0)
    flush = asyncio.create_task(batch.flush("artist"))
    await asyncio.sleep(0.01)
    assert not flush.done()

    release.set()

    assert await asyncio.wait_for(flush, timeout=1.0) is True
    assert waiting.calls == ["ack"]
    await asyncio.gather(other, arriving)


@pytest.mark.asyncio
async def test_flush_reports_false_when_a_handler_cannot_enter_within_a_tick() -> None:
    """Honest, and bounded: a key blocked by another's capacity does not hold its flush forever."""
    release = asyncio.Event()

    async def write(key: str, payloads: list[int]):
        if key == "release":
            await release.wait()
        return [BatchItemResult(Settlement.ACK, "ok") for _ in payloads]

    batch = engine(Mock(write=write), keys=["release", "artist"], batch_size=1, max_pending=1, max_concurrent_flushes=2, flush_interval_s=0.01)
    await batch.submit("release", 1, StrictDelivery())
    other = asyncio.create_task(batch.flush("release"))
    await asyncio.sleep(0.01)
    arriving = asyncio.create_task(batch.submit("artist", 2, StrictDelivery()))
    await asyncio.sleep(0)

    assert await asyncio.wait_for(batch.flush("artist"), timeout=1.0) is False
    assert batch.snapshot()["pending"]["artist"] == 1  # type: ignore[index]

    release.set()
    await asyncio.gather(other, arriving)


@pytest.mark.asyncio
async def test_a_cancelled_submit_leaves_nothing_arriving() -> None:
    batch = engine(AsyncMock(), max_pending=1, flush_interval_s=5.0)
    await batch.submit("release", 1, StrictDelivery())
    cancelled = asyncio.create_task(batch.submit("release", 2, StrictDelivery()))
    await asyncio.sleep(0)
    assert batch.snapshot()["pending"]["release"] == 2  # type: ignore[index]

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert batch.snapshot()["pending"]["release"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_drain_stops_waiting_when_the_last_arrival_is_cancelled() -> None:
    sink = AsyncMock()
    sink.write.return_value = [BatchItemResult(Settlement.ACK, "ok")]
    batch = engine(sink, max_pending=1, batch_size=1, flush_interval_s=60.0)
    await batch.submit("release", 1, StrictDelivery())
    arriving = asyncio.create_task(batch.submit("release", 2, StrictDelivery()))
    await asyncio.sleep(0)
    flush = asyncio.create_task(batch.flush("release"))
    await asyncio.sleep(0)
    arriving.cancel()

    assert await asyncio.wait_for(flush, timeout=1.0) is True

    with pytest.raises(asyncio.CancelledError):
        await arriving


def _headerless_engine(max_requeues: int | None) -> AsyncBatchEngine[str, int]:
    return AsyncBatchEngine(
        ["k"],
        policy=BatchPolicy(
            batch_size=10,
            flush_interval_s=1.0,
            max_pending=10,
            max_concurrent_flushes=1,
            min_batch_size=1,
            backoff_initial_s=0.01,
            backoff_max_s=0.02,
            backoff_multiplier=2.0,
            max_drain_retries=1,
            max_poison_retries=1,
            max_hold_s=1.0,
            max_requeues=max_requeues,
        ),
        sink=AsyncMock(),
        classifier=lambda _error: FailureKind.TRANSIENT,
        observer=MagicMock(),
    )


@pytest.mark.asyncio
async def test_a_budget_with_headerless_deliveries_warns_once() -> None:
    engine = _headerless_engine(20)
    delivery = SimpleNamespace(ack=AsyncMock(), nack=AsyncMock())
    with patch("common.batch.logger") as logger:
        await engine.submit("k", 1, delivery)
        await engine.submit("k", 2, delivery)
    assert logger.warning.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_requeues", "delivery"), [(None, SimpleNamespace()), (20, SimpleNamespace(headers={}))])
async def test_no_warning_without_a_budget_or_with_headers(max_requeues: int | None, delivery: object) -> None:
    engine = _headerless_engine(max_requeues)
    with patch("common.batch.logger") as logger:
        await engine.submit("k", 1, delivery)  # type: ignore[arg-type]
    logger.warning.assert_not_called()


def test_a_budget_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="max_requeues"):
        _headerless_engine(0)
