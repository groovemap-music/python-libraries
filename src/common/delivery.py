"""Transport-neutral broker delivery settlement.

Handlers decide *what* should happen; :func:`run_delivery` is the single place that
performs the terminal broker operation.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Protocol


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from contextlib import AbstractContextManager
    from types import TracebackType


class Settlement(StrEnum):
    ACK = "ack"
    REQUEUE = "requeue"
    REJECT = "reject"
    DEFER = "defer"


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    DETERMINISTIC = "deterministic"


class Delivery(Protocol):
    async def ack(self) -> None: ...

    async def nack(self, *, requeue: bool) -> None: ...


@dataclass(frozen=True)
class DeliveryResult:
    settlement: Settlement
    outcome: str
    error_type: str | None = None


class FailureClassifier(Protocol):
    def __call__(self, error: BaseException) -> FailureKind: ...


class DeliveryObserver(Protocol):
    def consume(self, destination: str, headers: object | None) -> AbstractContextManager[Any]: ...

    def settled(self, *, entity: str, result: DeliveryResult, duration_s: float, span: Any) -> None: ...


class _IsolatedObservation:
    def __init__(self, observer: DeliveryObserver, destination: str, headers: object | None) -> None:
        self._observer = observer
        self._destination = destination
        self._headers = headers
        self._context: AbstractContextManager[Any] | None = None

    def __enter__(self) -> Any:
        try:
            self._context = self._observer.consume(self._destination, self._headers)
            return self._context.__enter__()
        except Exception:
            self._context = None
            return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self._context is None:
            return False
        with contextlib.suppress(Exception):
            self._context.__exit__(exc_type, exc, traceback)
        return False


async def _settle(delivery: Delivery, settlement: Settlement) -> None:
    if settlement is Settlement.ACK:
        await delivery.ack()
    elif settlement is Settlement.REQUEUE:
        await delivery.nack(requeue=True)
    elif settlement is Settlement.REJECT:
        await delivery.nack(requeue=False)
    return


async def run_delivery(
    delivery: Delivery,
    operation: Callable[[], Awaitable[DeliveryResult]],
    *,
    classifier: FailureClassifier,
    observer: DeliveryObserver,
    destination: str,
    entity: str,
    headers: object | None = None,
    wait_before_requeue: Callable[[], Awaitable[None]] | None = None,
) -> DeliveryResult:
    """Run one handler attempt and apply at most one terminal settlement."""
    started = perf_counter()
    with _IsolatedObservation(observer, destination, headers) as span:
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            kind = classifier(error)
            if kind is FailureKind.TRANSIENT:
                if wait_before_requeue is not None:
                    await wait_before_requeue()
                result = DeliveryResult(Settlement.REQUEUE, kind.value, type(error).__name__)
            else:
                result = DeliveryResult(Settlement.REJECT, kind.value, type(error).__name__)

        await _settle(delivery, result.settlement)
        with contextlib.suppress(Exception):
            observer.settled(entity=entity, result=result, duration_s=perf_counter() - started, span=span)
        return result
