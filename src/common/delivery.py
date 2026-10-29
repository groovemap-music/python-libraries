"""Transport-neutral broker delivery settlement.

Handlers decide *what* should happen; :func:`run_delivery` is the single place that
performs the terminal broker operation.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Protocol


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractContextManager
    from types import TracebackType


class WriteAdmission:
    """One event-loop-owned database work budget and callback drain tracker.

    Construct once per service lifespan in its running loop. Callers supply the
    pool-sized capacity and enter :meth:`slot` before acquiring a connection.
    Wrap each complete delivery callback in :meth:`handler` and call :meth:`drain`
    only after stopping every producer that can start a new callback.
    """

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("write admission capacity must be a positive integer")
        self._loop = asyncio.get_running_loop()
        self._semaphore = asyncio.BoundedSemaphore(capacity)
        self._scopes = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("write admission belongs to a different event loop; create one for this lifespan")

    @contextlib.asynccontextmanager
    async def _tracked_scope(self) -> AsyncIterator[None]:
        self._check_loop()
        self._scopes += 1
        self._idle.clear()
        try:
            yield
        finally:
            self._scopes -= 1
            if self._scopes == 0:
                self._idle.set()

    @contextlib.asynccontextmanager
    async def handler(self) -> AsyncIterator[None]:
        """Track a complete callback without consuming a database write permit.

        Nested handler scopes count independently. Use this around the full
        delivery callback, including any enqueue or retry coordination.
        """
        async with self._tracked_scope():
            yield

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Bound actual work, releasing on success, exception, or cancellation.

        The drain tracker includes waiters as well as permit holders. Waiting
        cancellation never releases a permit it did not acquire. A fresh context
        is required for each attempt; enqueue and retry delays stay outside.
        """
        async with self._tracked_scope(), self._semaphore:
            yield

    async def drain(self, *, timeout: float) -> bool:
        """Wait for all tracked handlers and SQL scopes, without canceling work.

        The owner must stop delivery and background producers before draining.
        A timeout returns ``False`` and leaves every handler, waiter, and permit
        untouched. A drain called from inside a tracked scope counts that scope;
        it therefore cannot report idle until that scope exits.
        """
        self._check_loop()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("write admission drain timeout must be a finite non-negative number")
        try:
            finite_timeout = isfinite(timeout)
        except OverflowError:
            finite_timeout = False
        if not finite_timeout or timeout < 0:
            raise ValueError("write admission drain timeout must be a finite non-negative number")
        if self._scopes == 0:
            return True
        if timeout == 0:
            return False
        try:
            async with asyncio.timeout(timeout):
                while self._scopes:
                    await self._idle.wait()
        except TimeoutError:
            return False
        return True


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
