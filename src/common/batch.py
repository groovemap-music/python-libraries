"""Dependency-light asynchronous batching for Discogs consumers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from functools import partial
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from common.delivery import Delivery, DeliveryObserver, DeliveryResult, FailureClassifier, FailureKind, Settlement


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from types import TracebackType


logger = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT")
KeyT = TypeVar("KeyT")
PayloadT_contra = TypeVar("PayloadT_contra", contravariant=True)
KeyT_contra = TypeVar("KeyT_contra", contravariant=True)


@dataclass(frozen=True)
class BatchItemResult:
    settlement: Settlement
    outcome: str

    def __post_init__(self) -> None:
        if self.settlement not in (Settlement.ACK, Settlement.REJECT):
            raise ValueError("batch item settlement must be ACK or REJECT")


@dataclass(frozen=True)
class BatchPolicy:
    batch_size: int
    flush_interval_s: float
    max_pending: int
    max_concurrent_flushes: int
    min_batch_size: int
    backoff_initial_s: float
    backoff_max_s: float
    backoff_multiplier: float
    max_drain_retries: int
    max_poison_retries: int
    # Longest a delivery may stay unacknowledged while its key is in transient backoff. Set it
    # below the broker's consumer_timeout (RabbitMQ default 30 minutes): past the bound the
    # delivery is returned with nack(requeue=True) instead of being held until the broker closes
    # the channel. None keeps the historical unbounded hold.
    max_hold_s: float | None = None

    def __post_init__(self) -> None:
        positive = (
            self.batch_size,
            self.flush_interval_s,
            self.max_pending,
            self.max_concurrent_flushes,
            self.min_batch_size,
            self.backoff_initial_s,
            self.backoff_max_s,
            self.backoff_multiplier,
            self.max_drain_retries,
            self.max_poison_retries,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("batch policy values must be positive")
        if self.max_hold_s is not None and (isinstance(self.max_hold_s, bool) or not self.max_hold_s > 0):
            raise ValueError("batch policy max_hold_s must be positive when set")
        if self.min_batch_size > self.batch_size:
            raise ValueError("min_batch_size cannot exceed batch_size")


class BatchSink(Protocol[KeyT_contra, PayloadT_contra]):
    async def write(self, key: KeyT_contra, payloads: Sequence[PayloadT_contra]) -> Sequence[BatchItemResult]: ...


class BatchObserver(DeliveryObserver, Protocol[KeyT_contra]):
    def flush(self, key: KeyT_contra, size: int, links: Sequence[object]) -> AbstractContextManager[Any]: ...

    def retry(self, *, key: KeyT_contra, kind: FailureKind, attempt: int, delay_s: float, span: Any) -> None: ...


@dataclass(frozen=True, eq=False)
class _Pending[PayloadT]:
    payload: PayloadT
    delivery: Delivery
    span_context: object | None
    received_at: float = 0.0


@dataclass(eq=False)
class _Drain:
    """A drain of one key and the number of callers still waiting for its outcome."""

    task: asyncio.Task[bool]
    waiters: int = 0


def _retrieve_outcome(task: asyncio.Task[bool]) -> None:
    """Mark a drain's exception as seen; every caller that wanted it has it from ``result()``."""
    if not task.cancelled():
        task.exception()


class _IsolatedContext:
    def __init__(self, factory: Callable[[], AbstractContextManager[Any]]) -> None:
        self._factory = factory
        self._context: AbstractContextManager[Any] | None = None

    def __enter__(self) -> Any:
        try:
            self._context = self._factory()
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
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.__exit__(exc_type, exc, traceback)
        return False


class AsyncBatchEngine[KeyT, PayloadT]:
    """Own queued delivery settlement after :meth:`submit` completes."""

    def __init__(
        self,
        keys: Sequence[KeyT],
        *,
        policy: BatchPolicy,
        sink: BatchSink[KeyT, PayloadT],
        classifier: FailureClassifier,
        observer: BatchObserver[KeyT],
    ) -> None:
        if len(set(keys)) != len(keys):
            raise ValueError("batch keys must be unique")
        self._policy = policy
        self._sink = sink
        self._classifier = classifier
        self._observer = observer
        self._queues = {key: deque[_Pending[PayloadT]]() for key in keys}
        self._locks = {key: asyncio.Lock() for key in keys}
        self._capacity = asyncio.Semaphore(policy.max_pending)
        self._flush_capacity = asyncio.Semaphore(policy.max_concurrent_flushes)
        self._batch_sizes = dict.fromkeys(keys, policy.batch_size)
        self._transient_attempts = dict.fromkeys(keys, 0)
        self._poison_attempts = dict.fromkeys(keys, 0)
        self._retry_at = dict.fromkeys(keys, 0.0)
        self._next_drain: dict[KeyT, _Drain | None] = dict.fromkeys(keys)
        self._periodic_failures = dict.fromkeys(keys, 0)
        self._lost_deliveries = dict.fromkeys(keys, 0)
        self._shutdown = asyncio.Event()

    async def submit(
        self,
        key: KeyT,
        payload: PayloadT,
        delivery: Delivery,
        *,
        span_context: object | None = None,
    ) -> None:
        if key not in self._queues:
            raise KeyError(key)
        await self._capacity.acquire()
        try:
            self._queues[key].append(_Pending(payload, delivery, span_context, monotonic()))
        except BaseException:
            self._capacity.release()
            raise

    def _pop(self, key: KeyT) -> list[_Pending[PayloadT]]:
        queue = self._queues[key]
        return [queue.popleft() for _ in range(min(len(queue), self._batch_sizes[key]))]

    def _restore(self, key: KeyT, items: Sequence[_Pending[PayloadT]]) -> None:
        queue = self._queues[key]
        queue.extendleft(reversed(items))

    def _delay(self, attempt: int) -> float:
        return min(
            self._policy.backoff_max_s,
            self._policy.backoff_initial_s * self._policy.backoff_multiplier ** max(0, attempt - 1),
        )

    def _retry(self, key: KeyT, kind: FailureKind, attempt: int, delay_s: float, span: Any) -> None:
        with contextlib.suppress(Exception):
            self._observer.retry(key=key, kind=kind, attempt=attempt, delay_s=delay_s, span=span)

    def _observe_settled(self, key: KeyT, result: BatchItemResult, started: float, span: Any) -> None:
        delivery_result = DeliveryResult(result.settlement, result.outcome)
        with contextlib.suppress(Exception):
            self._observer.settled(entity=str(key), result=delivery_result, duration_s=perf_counter() - started, span=span)

    def _report_lost(self, key: KeyT, lost: Sequence[tuple[_Pending[PayloadT], Exception]]) -> None:
        """Account for deliveries whose settlement failed; see :meth:`_settle`."""
        if not lost:
            return
        self._lost_deliveries[key] += len(lost)
        error_type = type(lost[-1][1]).__name__
        logger.warning(f"⚠️ {len(lost)} {key} deliveries could not be settled ({error_type}); the broker holds them and will redeliver")
        now = monotonic()
        for item, error in lost:
            with contextlib.suppress(Exception):
                self._observer.settled(
                    entity=str(key),
                    result=DeliveryResult(Settlement.REQUEUE, "delivery_lost", type(error).__name__),
                    duration_s=now - item.received_at,
                    span=None,
                )

    async def _settle(
        self,
        key: KeyT,
        items: Sequence[_Pending[PayloadT]],
        results: Sequence[BatchItemResult],
        started: float,
        span: Any,
        lost: list[tuple[_Pending[PayloadT], Exception]],
    ) -> None:
        """Apply each result, leaving a delivery that cannot be settled to the broker.

        Delivery tags die with their channel, so a failed ack or nack can never succeed later, and
        retaining the item would rewrite it and fail again forever while it holds capacity. The
        broker still owns an unsettled message and redelivers it once the channel is gone, so
        dropping the local copy loses nothing. The rest of the batch is still settled.
        """
        for index, (item, result) in enumerate(zip(items, results, strict=True)):
            try:
                if result.settlement is Settlement.ACK:
                    await item.delivery.ack()
                else:
                    await item.delivery.nack(requeue=False)
            except Exception as error:
                self._capacity.release()
                lost.append((item, error))
            except BaseException:
                self._restore(key, items[index:])
                raise
            else:
                self._capacity.release()
                self._observe_settled(key, result, started, span)

    async def _release_expired(self, key: KeyT) -> None:
        """Return deliveries held past ``max_hold_s`` during transient backoff to the broker.

        ``nack(requeue=True)`` loses nothing: the broker redelivers the message and the consumer
        submits it again. Only the longest-held prefix is released, so surviving items keep their
        order. A failed nack means the channel is gone, and the broker redelivers those too.
        """
        limit = self._policy.max_hold_s
        if limit is None or self._transient_attempts[key] == 0:
            return
        queue = self._queues[key]
        now = monotonic()
        lost: list[tuple[_Pending[PayloadT], Exception]] = []
        try:
            while queue and now - queue[0].received_at >= limit:
                item = queue.popleft()
                try:
                    await item.delivery.nack(requeue=True)
                except Exception as error:
                    self._capacity.release()
                    lost.append((item, error))
                    continue
                except BaseException:
                    queue.appendleft(item)
                    raise
                self._capacity.release()
                with contextlib.suppress(Exception):
                    self._observer.settled(
                        entity=str(key),
                        result=DeliveryResult(Settlement.REQUEUE, "held_timeout"),
                        duration_s=now - item.received_at,
                        span=None,
                    )
        finally:
            self._report_lost(key, lost)

    async def flush(self, key: KeyT) -> bool:
        """Settle what is queued for ``key`` now; ``False`` if a failure left some of it queued.

        Work submitted after this call belongs to a later flush, so a key under constant backlog
        still returns. Callers do not line up behind a running drain: they share the single drain
        that starts after it, which covers everything they queued, so the number of waiting tasks
        stays bounded however fast deliveries arrive. That drain is cancelled only when its last
        waiter is, leaving its work queued; one cancelled handler stops no one else's write.
        """
        if key not in self._queues:
            raise KeyError(key)
        drain = self._next_drain[key]
        if drain is None or drain.task.done():
            drain = _Drain(asyncio.create_task(self._drain(key)))
            drain.task.add_done_callback(_retrieve_outcome)
            self._next_drain[key] = drain
        drain.waiters += 1
        try:
            await asyncio.wait({drain.task})
        except asyncio.CancelledError:
            drain.waiters -= 1
            if drain.waiters == 0 and not drain.task.done():
                if self._next_drain[key] is drain:
                    self._next_drain[key] = None
                drain.task.cancel()
                await asyncio.wait({drain.task})
            raise
        drain.waiters -= 1
        return drain.task.result()

    async def _drain(self, key: KeyT) -> bool:
        async with self._locks[key]:
            # The scope is fixed from here, so a later caller needs the drain after this one.
            self._next_drain[key] = None
            return await self._drain_locked(key)

    async def _drain_locked(self, key: KeyT) -> bool:
        await self._release_expired(key)
        no_progress = 0
        remaining = len(self._queues[key])
        while remaining > 0 and self._queues[key]:
            if monotonic() < self._retry_at[key]:
                return False
            items = self._pop(key)
            lost: list[tuple[_Pending[PayloadT], Exception]] = []
            acquired = False
            started = perf_counter()
            links = [item.span_context for item in items if item.span_context is not None][:64]
            try:
                await self._flush_capacity.acquire()
                acquired = True
                observation = partial(self._observer.flush, key, len(items), links)
                with _IsolatedContext(observation) as span:
                    try:
                        results = list(await self._sink.write(key, [item.payload for item in items]))
                    except asyncio.CancelledError:
                        self._restore(key, items)
                        raise
                    except Exception as error:
                        kind = self._classifier(error)
                        self._batch_sizes[key] = max(self._policy.min_batch_size, self._batch_sizes[key] // 2)
                        if kind is FailureKind.TRANSIENT:
                            self._transient_attempts[key] += 1
                            attempt = self._transient_attempts[key]
                            delay = self._delay(attempt)
                            self._retry_at[key] = monotonic() + delay
                            self._restore(key, items)
                            self._retry(key, kind, attempt, delay, span)
                            return False

                        self._poison_attempts[key] += 1
                        attempt = self._poison_attempts[key]
                        if attempt >= self._policy.max_poison_retries:
                            rejected = [BatchItemResult(Settlement.REJECT, "poison") for _ in items]
                            await self._settle(key, items, rejected, started, span, lost)
                            self._poison_attempts[key] = 0
                            no_progress = 0
                            remaining -= len(items)
                            continue
                        self._restore(key, items)
                        self._retry(key, kind, attempt, 0.0, span)
                        no_progress += 1
                        if no_progress >= self._policy.max_drain_retries:
                            return False
                        continue

                    try:
                        if len(results) != len(items):
                            raise ValueError("batch sink must return exactly one result per payload")
                        if any(result.settlement not in (Settlement.ACK, Settlement.REJECT) for result in results):
                            raise ValueError("batch sink returned a non-terminal result")
                    except Exception:
                        self._restore(key, items)
                        raise

                    await self._settle(key, items, results, started, span, lost)
                    remaining -= len(items)
                    self._batch_sizes[key] = min(self._policy.batch_size, self._batch_sizes[key] + 1)
                    self._transient_attempts[key] = 0
                    self._poison_attempts[key] = 0
                    self._retry_at[key] = 0.0
                    no_progress = 0
            except asyncio.CancelledError:
                if not any(item in self._queues[key] for item in items):
                    self._restore(key, items)
                raise
            finally:
                if acquired:
                    self._flush_capacity.release()
                self._report_lost(key, lost)
        return True

    async def flush_all(self) -> bool:
        results = await asyncio.gather(*(self.flush(key) for key in self._queues))
        return all(results)

    async def run_periodic(self) -> None:
        """Flush every key on its own clock until :meth:`shutdown`, whatever any one key is doing."""
        await asyncio.gather(*(self._run_periodic_key(key) for key in self._queues))

    async def _run_periodic_key(self, key: KeyT) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self._policy.flush_interval_s)
            except TimeoutError:
                try:
                    await self.flush(key)
                except Exception:
                    self._periodic_failures[key] += 1
                    logger.exception(f"❌ Periodic flush of {key} failed; its work stays queued for the next tick")

    def shutdown(self) -> None:
        self._shutdown.set()

    def snapshot(self) -> object:
        return {
            "pending": {key: len(queue) for key, queue in self._queues.items()},
            "batch_sizes": dict(self._batch_sizes),
            "transient_attempts": dict(self._transient_attempts),
            "poison_attempts": dict(self._poison_attempts),
            "shutdown": self._shutdown.is_set(),
            "oldest_pending_age_s": {key: monotonic() - queue[0].received_at if queue else 0.0 for key, queue in self._queues.items()},
            "periodic_failures": dict(self._periodic_failures),
            "lost_deliveries": dict(self._lost_deliveries),
        }
