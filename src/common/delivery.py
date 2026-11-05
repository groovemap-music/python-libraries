"""Transport-neutral broker delivery settlement.

Handlers decide *what* should happen; :func:`run_delivery` is the single place that
performs the terminal broker operation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractContextManager
    from types import TracebackType

logger = structlog.get_logger(__name__)

# How many times a failing delivery may be requeued before it is dead-lettered instead. The broker
# will not do this: on RabbitMQ 4.3.5 and 4.4.0 ``x-delivery-limit`` counts channel and connection
# closes and ``basic.reject``, never ``basic.nack(requeue=True)``. See docs/runtime.md. Paced by
# OutageBackoff's 60 s cap this is about 8.3 hours, so a database outage does not dead-letter
# records that are fine, and a message failing on a healthy backend ends much sooner.
DEFAULT_MAX_REQUEUES = 500


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


def broker_handouts(delivery: object) -> int:
    """Return how many times the broker has handed this message out before this delivery.

    ``x-acquired-count`` rises on every return of any kind, while ``x-delivery-count`` misses nacks,
    so a redelivery's real count is the larger. A first delivery carries neither header, and a
    delivery that exposes no ``headers`` counts as zero.
    """
    headers = getattr(delivery, "headers", None)
    if not isinstance(headers, Mapping):
        return 0
    counts = (headers.get(name) for name in ("x-acquired-count", "x-delivery-count"))
    return max((count for count in counts if isinstance(count, int)), default=0)


def requeues_spent(delivery: object, max_requeues: int | None) -> bool:
    """Return whether ``delivery`` has used its budget. ``None`` means no bound."""
    return max_requeues is not None and broker_handouts(delivery) >= max_requeues


async def requeue_or_dead_letter(delivery: Delivery, *, max_requeues: int | None = DEFAULT_MAX_REQUEUES) -> bool:
    """Requeue ``delivery``, or dead-letter it once its budget is spent. ``True`` if requeued.

    For the ``nack(requeue=True)`` sites outside :func:`run_delivery`, because the broker does not
    bound them. Wait before calling this, or the message comes straight back.
    """
    if requeues_spent(delivery, max_requeues):
        logger.error("☠️ Redelivery budget spent, dead-lettering", handouts=broker_handouts(delivery), max_requeues=max_requeues)
        await delivery.nack(requeue=False)
        return False
    await delivery.nack(requeue=True)
    return True


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
    max_requeues: int | None = None,
) -> DeliveryResult:
    """Run one handler attempt and apply at most one terminal settlement.

    Every requeue, raised or returned by the handler, waits on ``wait_before_requeue`` first. A
    requeue past the budget is dead-lettered instead and reported with the outcome
    ``redelivery_exhausted``. The budget is ``max_requeues``, or ``DEFAULT_MAX_REQUEUES`` when there
    is a wait. An unpaced requeue has no default budget, because a count of instant nacks would
    dead-letter good records seconds into an outage: pass a wait, or ``max_requeues`` explicitly.
    """
    budget = DEFAULT_MAX_REQUEUES if max_requeues is None and wait_before_requeue is not None else max_requeues
    started = perf_counter()
    with _IsolatedObservation(observer, destination, headers) as span:
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            kind = classifier(error)
            if kind is FailureKind.TRANSIENT:
                result = DeliveryResult(Settlement.REQUEUE, kind.value, type(error).__name__)
            else:
                result = DeliveryResult(Settlement.REJECT, kind.value, type(error).__name__)

        if result.settlement is Settlement.REQUEUE:
            if requeues_spent(delivery, budget):
                logger.error(
                    "☠️ Redelivery budget spent, dead-lettering",
                    destination=destination,
                    handouts=broker_handouts(delivery),
                    max_requeues=budget,
                    error_type=result.error_type,
                )
                result = DeliveryResult(Settlement.REJECT, "redelivery_exhausted", result.error_type)
            elif wait_before_requeue is not None:
                await wait_before_requeue()

        await _settle(delivery, result.settlement)
        with contextlib.suppress(Exception):
            observer.settled(entity=entity, result=result, duration_s=perf_counter() - started, span=span)
        return result
