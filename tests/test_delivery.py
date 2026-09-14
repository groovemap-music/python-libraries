from __future__ import annotations

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock

import pytest

from common.delivery import DeliveryResult, FailureKind, Settlement, run_delivery


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
