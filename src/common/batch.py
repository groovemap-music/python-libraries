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

from common.delivery import Delivery, DeliveryObserver, DeliveryResult, FailureClassifier, FailureKind, Settlement, requeue_or_dead_letter


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager
    from types import TracebackType


logger = logging.getLogger(__name__)

# Longest one sink write may run before it counts as a failed write. A store that stops answering
# raises nothing and no driver read timeout applies, so without this the key's drain waits forever.
DEFAULT_MAX_WRITE_S = 120.0
# How long a cancelled write gets to finish before the engine stops waiting for it. One cancel frees a
# Neo4j write at once. A PostgreSQL write on a frozen server stays in its rollback until the server
# answers, so the bound cannot depend on cancellation finishing.
WRITE_CANCEL_GRACE_S = 5.0
# How long deliveries returned to the broker count as pending while they are not back. Redelivery
# takes milliseconds, so this only runs out when a handler discards the record instead of submitting
# it, or the channel is slow to recover. It keeps that case from blocking a marker forever.
RETURN_WINDOW_S = 30.0

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
    # Longest a delivery may wait in the engine, counted from submit(), whether it is queued or still
    # waiting for capacity. Set it below the broker's consumer_timeout (RabbitMQ default 30 minutes):
    # past the bound the delivery is returned with nack(requeue=True) instead of being held until
    # the broker closes the channel. Set max_pending to at least the total prefetch of every
    # channel feeding this engine, or deliveries spend their max_requeues budget waiting for
    # capacity. None keeps the historical unbounded hold.
    max_hold_s: float | None = None
    # How many times a held delivery may go back to the broker before it is dead-lettered instead,
    # which nothing else does: RabbitMQ does not spend x-delivery-limit on nack(requeue=True). The
    # outage survived is this times max_hold_s, so set the two together. It needs a delivery that
    # exposes the broker's ``headers``. None keeps requeueing without a bound.
    max_requeues: int | None = None
    # Longest one sink write may run. Past it the write is cancelled and counts as a transient
    # failure, which is what lets max_hold_s act on a store that hangs instead of failing. A delivery
    # is settled or back on the broker within max_hold_s + 2 * (max_write_s + WRITE_CANCEL_GRACE_S) +
    # flush_interval_s: one write that started just inside the hold, and one other key's write it
    # waited behind for a flush slot, which is one write with 4 keys and 2 slots. Keep that below the
    # broker's consumer_timeout, and keep this above the longest legitimate write. None keeps the
    # historical unbounded write.
    max_write_s: float | None = DEFAULT_MAX_WRITE_S

    def __post_init__(self) -> None:
        if self.max_requeues is not None and self.max_requeues < 1:
            raise ValueError("max_requeues must be a positive integer or None")
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
        if self.max_write_s is not None and (isinstance(self.max_write_s, bool) or not self.max_write_s > 0):
            raise ValueError("batch policy max_write_s must be positive when set")
        if self.min_batch_size > self.batch_size:
            raise ValueError("min_batch_size cannot exceed batch_size")


class BatchWriteTimeoutError(TimeoutError):
    """A sink write ran past ``BatchPolicy.max_write_s``. Always transient: the store hung, the record did not fail."""


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


def _retrieve_outcome(task: asyncio.Future[Any]) -> None:
    """Mark a task's exception as seen; every caller that wanted it has it from ``result()``."""
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
        self._arriving = dict.fromkeys(keys, 0)
        self._arrivals = {key: asyncio.Event() for key in keys}
        self._next_drain: dict[KeyT, _Drain | None] = dict.fromkeys(keys)
        self._periodic_failures = dict.fromkeys(keys, 0)
        self._lost_deliveries = dict.fromkeys(keys, 0)
        self._returning = dict.fromkeys(keys, 0)
        self._returning_until = dict.fromkeys(keys, 0.0)
        self._abandoned_writes: dict[KeyT, set[asyncio.Future[Any]]] = {key: set() for key in keys}
        self._warned_headerless = False
        self._warned_capacity = False
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
        if self._policy.max_requeues is not None and not self._warned_headerless and not hasattr(delivery, "headers"):
            self._warned_headerless = True
            logger.warning(f"⚠️ {key} deliveries expose no broker headers, so max_requeues={self._policy.max_requeues} can never be reached")
        if self._returning[key] > 0:
            self._returning[key] -= 1
        # The broker's timer started before this call, so the hold starts here and not at the append.
        item = _Pending(payload, delivery, span_context, monotonic())
        self._arriving[key] += 1
        try:
            if not await self._capacity_within_hold(key, item):
                return
            try:
                self._queues[key].append(item)
            except BaseException:
                self._capacity.release()
                raise
        finally:
            self._arriving[key] -= 1
            self._arrivals[key].set()

    async def _capacity_within_hold(self, key: KeyT, item: _Pending[PayloadT]) -> bool:
        """Take a capacity slot, or return ``item`` to the broker if none came within ``max_hold_s``.

        A delivery waiting here is not in any queue, so no drain can reach it, and nothing else
        frees it: slots come back only when other deliveries settle, which during an outage is one
        hold at a time. Deliveries past ``max_pending`` would each wait out every hold ahead of them.
        """
        limit = self._policy.max_hold_s
        if limit is None:
            await self._capacity.acquire()
            return True
        if self._capacity.locked() and not self._warned_capacity:
            self._warned_capacity = True
            logger.warning(
                f"⚠️ {key} delivery is waiting for capacity, so max_pending={self._policy.max_pending} is below what the broker "
                "has delivered: set it to at least the total prefetch of every channel, or deliveries spend their requeues waiting"
            )
        try:
            async with asyncio.timeout(limit):
                await self._capacity.acquire()
        except TimeoutError:
            lost: list[tuple[_Pending[PayloadT], Exception]] = []
            try:
                await self._return_to_broker(key, item, lost)
            finally:
                self._report_lost(key, lost)
            return False
        return True

    def _pending(self, key: KeyT) -> int:
        """Work the engine owes this key: queued, inside :meth:`submit`, or on its way back from the broker."""
        return len(self._queues[key]) + self._arriving[key] + self._returning_now(key)

    def _returning_now(self, key: KeyT) -> int:
        """Deliveries returned to the broker that have not come back, for up to ``RETURN_WINDOW_S``.

        A returned record is still unwritten, so it counts as pending until a handler resubmits it.
        It has to outlive the drain that released it: the next drain can start the moment this one
        ends, and every gate that reads ``flush()`` or ``snapshot()["pending"]`` would otherwise see
        an empty queue while the record is on the wire. Any submit pays one off, because the broker
        returns a requeued delivery ahead of the messages behind it.
        """
        if self._returning[key] > 0 and monotonic() >= self._returning_until[key]:
            self._returning[key] = 0
        return self._returning[key]

    async def _next_arrival(self, key: KeyT) -> bool:
        """Wait for a :meth:`submit` to finish; ``False`` if none did within a tick."""
        arrival = self._arrivals[key]
        arrival.clear()
        try:
            async with asyncio.timeout(self._policy.flush_interval_s):
                await arrival.wait()
        except TimeoutError:
            return False
        return True

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

    async def _write(self, key: KeyT, items: Sequence[_Pending[PayloadT]]) -> Sequence[BatchItemResult]:
        """Run one sink write for at most ``max_write_s``, or raise :class:`BatchWriteTimeoutError`.

        The write gets its own task because a timeout inside it would rely on cancellation
        finishing, and on a frozen server a driver's cleanup can wait on the socket as long as the
        write did. A write that outlives :data:`WRITE_CANCEL_GRACE_S` is left running, so its
        batch may be written twice. Sinks are already idempotent, because every delivery is at
        least once. The connection pool bounds how many such writes can pile up.
        """
        payloads = [item.payload for item in items]
        limit = self._policy.max_write_s
        if limit is None:
            return await self._sink.write(key, payloads)
        write = asyncio.ensure_future(self._sink.write(key, payloads))
        write.add_done_callback(_retrieve_outcome)
        try:
            await asyncio.wait({write}, timeout=limit)
        except asyncio.CancelledError:
            await self._stop_write(key, write)
            raise
        if write.done():
            return write.result()
        await self._stop_write(key, write)
        if write.done() and not write.cancelled() and write.exception() is None:
            return write.result()
        raise BatchWriteTimeoutError(f"{key} write ran past {limit:g} s")

    async def _stop_write(self, key: KeyT, write: asyncio.Future[Any]) -> None:
        """Cancel ``write`` and wait out the grace, keeping a reference if it will not stop."""
        abandoned = self._abandoned_writes[key]
        abandoned.add(write)
        write.add_done_callback(abandoned.discard)
        write.cancel()
        await asyncio.wait({write}, timeout=WRITE_CANCEL_GRACE_S)
        if not write.done():
            logger.warning(f"⚠️ {key} write did not stop within {WRITE_CANCEL_GRACE_S:g} s of being cancelled; leaving it to finish on its own")

    async def _return_to_broker(self, key: KeyT, item: _Pending[PayloadT], lost: list[tuple[_Pending[PayloadT], Exception]]) -> None:
        """Requeue ``item``, or dead-letter it once ``max_requeues`` is spent.

        ``nack(requeue=True)`` loses nothing: the broker redelivers the message and the consumer
        submits it again, until ``max_requeues`` sends it to the dead-letter queue. A failed nack
        means the channel is gone, and the broker redelivers those too. Each one counts as pending
        until it is back (see :meth:`_returning_now`), so no drain reports a key complete while its
        records are on the broker. One sent to the dead-letter queue is not coming back and does not
        count, the same as a rejected poison record: that is what a purge veto is for.
        """
        now = monotonic()
        # Counted before the nack: the redelivery can reach submit() before the nack returns.
        self._returning[key] = self._returning_now(key) + 1
        self._returning_until[key] = now + RETURN_WINDOW_S
        try:
            requeued = await requeue_or_dead_letter(item.delivery, max_requeues=self._policy.max_requeues)
        except Exception as error:
            lost.append((item, error))
            return
        except BaseException:
            self._returning[key] = max(0, self._returning[key] - 1)
            raise
        if not requeued:
            self._returning[key] = max(0, self._returning[key] - 1)
        with contextlib.suppress(Exception):
            self._observer.settled(
                entity=str(key),
                result=DeliveryResult(Settlement.REQUEUE, "held_timeout") if requeued else DeliveryResult(Settlement.REJECT, "redelivery_exhausted"),
                duration_s=now - item.received_at,
                span=None,
            )

    async def _release_expired(self, key: KeyT) -> None:
        """Return queued deliveries held past ``max_hold_s`` to the broker.

        Only the longest-held prefix is released, so surviving items keep their order. A key in
        backoff writes nothing, so nothing is spared. A working key spares the batch it writes next:
        if the store is slower than the hold, returning the oldest records just before writing them
        would write nothing, and every record would spend its requeues going round. Those behind
        that batch go back, because the broker's timer does not care that the store is working.
        """
        limit = self._policy.max_hold_s
        if limit is None:
            return
        spared = 0 if self._transient_attempts[key] else self._batch_sizes[key]
        queue = self._queues[key]
        now = monotonic()
        lost: list[tuple[_Pending[PayloadT], Exception]] = []
        try:
            while len(queue) > spared and now - queue[spared].received_at >= limit:
                item = queue[spared]
                del queue[spared]
                try:
                    await self._return_to_broker(key, item, lost)
                except BaseException:
                    queue.insert(spared, item)
                    raise
                self._capacity.release()
        finally:
            self._report_lost(key, lost)

    async def flush(self, key: KeyT) -> bool:
        """Settle ``key``'s work; ``True`` only if nothing at all is pending when it ends.

        Callers gate destructive steps on ``True``, so a handler still inside :meth:`submit`
        counts as pending: the drain waits for it, for up to a tick at a time. So does a delivery
        returned to the broker that is not back yet. Each drain is
        bounded by the work present when it starts, so a key under constant backlog still returns,
        with ``False``. Callers do not line up behind a running drain: they share the single drain
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
        no_progress = 0
        remaining = self._pending(key)
        while remaining > 0:
            await self._release_expired(key)
            if not self._queues[key]:
                if self._arriving[key] and await self._next_arrival(key):
                    continue
                break
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
                        results = list(await self._write(key, items))
                    except asyncio.CancelledError:
                        self._restore(key, items)
                        raise
                    except Exception as error:
                        # Still classified, because an owner's classifier also marks the flush failed.
                        kind = self._classifier(error)
                        if isinstance(error, BatchWriteTimeoutError):
                            kind = FailureKind.TRANSIENT
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
        return self._pending(key) == 0

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
            "pending": {key: self._pending(key) for key in self._queues},
            "batch_sizes": dict(self._batch_sizes),
            "transient_attempts": dict(self._transient_attempts),
            "poison_attempts": dict(self._poison_attempts),
            "shutdown": self._shutdown.is_set(),
            "oldest_pending_age_s": {key: monotonic() - queue[0].received_at if queue else 0.0 for key, queue in self._queues.items()},
            "periodic_failures": dict(self._periodic_failures),
            "lost_deliveries": dict(self._lost_deliveries),
            "returning": {key: self._returning_now(key) for key in self._queues},
            "abandoned_writes": {key: len(writes) for key, writes in self._abandoned_writes.items()},
        }
