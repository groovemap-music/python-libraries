from __future__ import annotations

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from common.delivery import DEFAULT_MAX_REQUEUES, DeliveryResult, FailureKind, Settlement, broker_handouts, requeue_or_dead_letter, run_delivery


class Observer:
    def __init__(self) -> None:
        self.settled = Mock()

    def consume(self, _destination: str, _headers: object | None):  # type: ignore[no-untyped-def]
        return nullcontext("span")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settlement", "ack", "nack"),
    [(Settlement.ACK, 1, None), (Settlement.REQUEUE, 0, True), (Settlement.REJECT, 0, False), (Settlement.DEFER, 0, None)],
)
async def test_result_has_exactly_one_terminal_operation(settlement: Settlement, ack: int, nack: bool | None) -> None:
    delivery = AsyncMock()
    observer = Observer()
    result = DeliveryResult(settlement, "owner-outcome")

    assert (
        await run_delivery(delivery, AsyncMock(return_value=result), classifier=Mock(), observer=observer, destination="queue", entity="release")
        == result
    )
    assert delivery.ack.await_count == ack
    if nack is None:
        delivery.nack.assert_not_awaited()
    else:
        delivery.nack.assert_awaited_once_with(requeue=nack)


@pytest.mark.asyncio
async def test_transient_exception_waits_once_and_requeues() -> None:
    delivery, wait = AsyncMock(), AsyncMock()
    operation = AsyncMock(side_effect=RuntimeError("down"))
    result = await run_delivery(
        delivery,
        operation,
        classifier=lambda _: FailureKind.TRANSIENT,
        observer=Observer(),
        destination="queue",
        entity="release",
        wait_before_requeue=wait,
    )
    assert result == DeliveryResult(Settlement.REQUEUE, "transient", "RuntimeError")
    operation.assert_awaited_once()
    wait.assert_awaited_once()
    delivery.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_cancelled_error_has_no_side_effects() -> None:
    delivery, classifier, wait = AsyncMock(), Mock(), AsyncMock()
    observer = Observer()
    with pytest.raises(asyncio.CancelledError):
        await run_delivery(
            delivery,
            AsyncMock(side_effect=asyncio.CancelledError),
            classifier=classifier,
            observer=observer,
            destination="queue",
            entity="release",
            wait_before_requeue=wait,
        )
    classifier.assert_not_called()
    wait.assert_not_awaited()
    observer.settled.assert_not_called()
    delivery.ack.assert_not_awaited()
    delivery.nack.assert_not_awaited()


@pytest.mark.asyncio
async def test_observer_failure_cannot_change_settlement() -> None:
    delivery = AsyncMock()
    observer = Observer()
    observer.settled.side_effect = RuntimeError("metrics")
    await run_delivery(
        delivery,
        AsyncMock(return_value=DeliveryResult(Settlement.ACK, "ok")),
        classifier=Mock(),
        observer=observer,
        destination="queue",
        entity="release",
    )
    delivery.ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", [Settlement.ACK, Settlement.REQUEUE, Settlement.REJECT])
async def test_terminal_failure_is_visible_without_a_second_call(settlement: Settlement) -> None:
    delivery = AsyncMock()
    failure = RuntimeError("broker settlement failed")
    if settlement is Settlement.ACK:
        delivery.ack.side_effect = failure
    else:
        delivery.nack.side_effect = failure
    with pytest.raises(RuntimeError, match="broker settlement failed"):
        await run_delivery(
            delivery,
            AsyncMock(return_value=DeliveryResult(settlement, "handled")),
            classifier=Mock(),
            observer=Observer(),
            destination="queue",
            entity="release",
        )
    assert delivery.ack.await_count + delivery.nack.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["enter", "exit"])
async def test_observer_context_failures_are_isolated(failure_point: str) -> None:
    class BrokenContext:
        def __enter__(self):  # type: ignore[no-untyped-def]
            if failure_point == "enter":
                raise RuntimeError("observer enter")
            return "span"

        def __exit__(self, *_args: object) -> None:
            if failure_point == "exit":
                raise RuntimeError("observer exit")

    observer = Observer()
    observer.consume = Mock(return_value=BrokenContext())  # type: ignore[method-assign]
    delivery = AsyncMock()
    result = await run_delivery(
        delivery,
        AsyncMock(return_value=DeliveryResult(Settlement.ACK, "ok")),
        classifier=Mock(),
        observer=observer,
        destination="queue",
        entity="release",
    )
    assert result.settlement is Settlement.ACK
    delivery.ack.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.DETERMINISTIC])
async def test_admitted_write_releases_before_retry_and_settlement(kind: FailureKind) -> None:
    from common import WriteAdmission

    admission = WriteAdmission(1)
    delivery = AsyncMock()
    observer = Observer()
    observer.settled.side_effect = RuntimeError("metrics")
    retried = False

    async def operation() -> DeliveryResult:
        async with admission.slot():
            raise ConnectionError("database")

    async def wait() -> None:
        nonlocal retried
        async with admission.slot():
            retried = True

    async def settle(*, requeue: bool) -> None:
        assert requeue is (kind is FailureKind.TRANSIENT)
        async with admission.slot():
            pass

    delivery.nack.side_effect = settle
    result = await run_delivery(
        delivery,
        operation,
        classifier=lambda _: kind,
        observer=observer,
        destination="queue",
        entity="release",
        wait_before_requeue=wait,
    )
    assert retried is (kind is FailureKind.TRANSIENT)
    assert result.settlement is (Settlement.REQUEUE if kind is FailureKind.TRANSIENT else Settlement.REJECT)
    delivery.nack.assert_awaited_once_with(requeue=kind is FailureKind.TRANSIENT)
    delivery.ack.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiting", [False, True])
async def test_admission_cancellation_preserves_delivery_ownership(cancel_waiting: bool) -> None:
    from common import WriteAdmission

    admission = WriteAdmission(1)
    blocker = admission.slot()
    if cancel_waiting:
        await blocker.__aenter__()
    delivery, classifier, wait = AsyncMock(), Mock(), AsyncMock()
    observer = Observer()
    attempted = asyncio.Event()
    entered = asyncio.Event()

    async def operation() -> DeliveryResult:
        attempted.set()
        async with admission.slot():
            entered.set()
            await asyncio.Future[None]()
        return DeliveryResult(Settlement.ACK, "ok")

    task = asyncio.create_task(
        run_delivery(
            delivery,
            operation,
            classifier=classifier,
            observer=observer,
            destination="queue",
            entity="release",
            wait_before_requeue=wait,
        )
    )
    await attempted.wait()
    assert entered.is_set() is (not cancel_waiting)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    if cancel_waiting:
        await blocker.__aexit__(None, None, None)
    async with admission.slot():
        pass
    classifier.assert_not_called()
    wait.assert_not_awaited()
    observer.settled.assert_not_called()
    delivery.ack.assert_not_awaited()
    delivery.nack.assert_not_awaited()


def _message(headers: object) -> AsyncMock:
    message = AsyncMock()
    message.headers = headers
    return message


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (None, 0),
        ({}, 0),
        ({"x-acquired-count": 7}, 7),
        ({"x-delivery-count": 3}, 3),
        ({"x-acquired-count": 29, "x-delivery-count": 20}, 29),
        ({"x-acquired-count": "9"}, 0),
    ],
)
def test_broker_handouts_reads_the_larger_count(headers: object, expected: int) -> None:
    assert broker_handouts(_message(headers)) == expected


def test_a_delivery_without_headers_counts_as_a_first_delivery() -> None:
    assert broker_handouts(object()) == 0


@pytest.mark.asyncio
async def test_requeue_within_budget_nacks_with_requeue() -> None:
    message = _message({"x-acquired-count": 2})
    assert await requeue_or_dead_letter(message, max_requeues=3) is True
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_requeue_past_budget_dead_letters() -> None:
    message = _message({"x-acquired-count": 3})
    assert await requeue_or_dead_letter(message, max_requeues=3) is False
    message.nack.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
async def test_no_budget_always_requeues() -> None:
    message = _message({"x-acquired-count": 10_000})
    assert await requeue_or_dead_letter(message, max_requeues=None) is True
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_run_delivery_dead_letters_a_spent_transient_failure_without_waiting() -> None:
    message = _message({"x-acquired-count": 5})
    wait = AsyncMock()
    observer = MagicMock()
    observer.consume.return_value = nullcontext()

    async def operation() -> DeliveryResult:
        raise ConnectionError("down")

    result = await run_delivery(
        message,
        operation,
        classifier=lambda _error: FailureKind.TRANSIENT,
        observer=observer,
        destination="q",
        entity="e",
        wait_before_requeue=wait,
        max_requeues=5,
    )

    assert result == DeliveryResult(Settlement.REJECT, "redelivery_exhausted", "ConnectionError")
    wait.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=False)


@pytest.mark.asyncio
async def test_run_delivery_bounds_a_requeue_the_handler_returned() -> None:
    message = _message({"x-acquired-count": 5})
    observer = MagicMock()
    observer.consume.return_value = nullcontext()

    async def operation() -> DeliveryResult:
        return DeliveryResult(Settlement.REQUEUE, "failed", "Reconciliation")

    result = await run_delivery(
        message,
        operation,
        classifier=lambda _error: FailureKind.TRANSIENT,
        observer=observer,
        destination="q",
        entity="e",
        max_requeues=5,
    )

    assert result == DeliveryResult(Settlement.REJECT, "redelivery_exhausted", "Reconciliation")


@pytest.mark.asyncio
async def test_run_delivery_paces_a_requeue_the_handler_returned() -> None:
    message = _message({"x-acquired-count": 1})
    wait = AsyncMock()
    observer = MagicMock()
    observer.consume.return_value = nullcontext()

    async def operation() -> DeliveryResult:
        return DeliveryResult(Settlement.REQUEUE, "failed", "Reconciliation")

    await run_delivery(
        message,
        operation,
        classifier=lambda _error: FailureKind.TRANSIENT,
        observer=observer,
        destination="q",
        entity="e",
        wait_before_requeue=wait,
    )

    wait.assert_awaited_once()
    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_an_unpaced_requeue_has_no_default_budget() -> None:
    message = _message({"x-acquired-count": 100_000})
    observer = MagicMock()
    observer.consume.return_value = nullcontext()

    async def operation() -> DeliveryResult:
        raise ConnectionError("down")

    await run_delivery(message, operation, classifier=lambda _error: FailureKind.TRANSIENT, observer=observer, destination="q", entity="e")

    message.nack.assert_awaited_once_with(requeue=True)


@pytest.mark.asyncio
async def test_a_paced_requeue_gets_the_default_budget() -> None:
    message = _message({"x-acquired-count": DEFAULT_MAX_REQUEUES})
    observer = MagicMock()
    observer.consume.return_value = nullcontext()

    async def operation() -> DeliveryResult:
        raise ConnectionError("down")

    result = await run_delivery(
        message,
        operation,
        classifier=lambda _error: FailureKind.TRANSIENT,
        observer=observer,
        destination="q",
        entity="e",
        wait_before_requeue=AsyncMock(),
    )

    assert result.outcome == "redelivery_exhausted"
