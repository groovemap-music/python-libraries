"""Deterministic controls for a shared budget before database pool acquisition."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import dataclass

import pytest

from common import WriteAdmission


@dataclass
class PoolProbe:
    active: int = 0
    peak: int = 0


class Write:
    def __init__(self, queue: str, pool: PoolProbe) -> None:
        self.queue = queue
        self.pool = pool
        self.attempted = asyncio.Event()
        self.entered_pool = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, admission: WriteAdmission | None) -> None:
        self.attempted.set()
        async with admission.slot() if admission is not None else nullcontext():
            # This is the connection-pool entry, not a post-acquisition counter.
            self.pool.active += 1
            self.pool.peak = max(self.pool.peak, self.pool.active)
            self.entered_pool.set()
            try:
                await self.release.wait()
            finally:
                self.pool.active -= 1


async def start(writes: list[Write], admission: WriteAdmission | None) -> list[asyncio.Task[None]]:
    tasks = [asyncio.create_task(write.run(admission)) for write in writes]
    await asyncio.gather(*(write.attempted.wait() for write in writes))
    return tasks


async def finish(writes: list[Write], tasks: list[asyncio.Task[None]]) -> None:
    for write in writes:
        write.release.set()
    await asyncio.gather(*tasks)


async def assert_one_permit(admission: WriteAdmission) -> None:
    pool = PoolProbe()
    writes = [Write("recovery", pool), Write("other", pool)]
    tasks = await start(writes, admission)
    assert pool.active == 1
    assert sum(write.entered_pool.is_set() for write in writes) == 1
    await finish(writes, tasks)
    assert pool.peak == 1
    assert pool.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("queues", [("release", "artist", "label", "release", "artist"), ("release",) * 5])
async def test_shared_admission_bounds_mixed_queues_and_lends_spare_capacity(queues: tuple[str, ...]) -> None:
    admission = WriteAdmission(3)
    pool = PoolProbe()
    writes = [Write(queue, pool) for queue in queues]
    tasks = await start(writes, admission)
    assert pool.active == 3
    assert sum(write.entered_pool.is_set() for write in writes) == 3
    await finish(writes, tasks)
    assert all(write.entered_pool.is_set() for write in writes)
    assert pool.peak == 3
    assert pool.active == 0


@pytest.mark.asyncio
async def test_control_without_admission_oversubscribes_pool_entry() -> None:
    """The same workload exposes queue-prefetch oversubscription without a budget."""
    pool = PoolProbe()
    writes = [Write(queue, pool) for queue in ("release", "artist", "label", "release", "artist")]
    tasks = await start(writes, None)
    assert pool.active == pool.peak == 5
    assert pool.peak > 3  # the pool write budget used by the guarded control
    await finish(writes, tasks)


@pytest.mark.asyncio
async def test_waiting_cancellation_does_not_release_another_writers_permit() -> None:
    admission = WriteAdmission(1)
    pool = PoolProbe()
    holder, waiting, recovery = [Write(queue, pool) for queue in ("holder", "waiting", "recovery")]
    holder_task = (await start([holder], admission))[0]
    waiting_task = (await start([waiting], admission))[0]
    assert not waiting.entered_pool.is_set()
    waiting_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task
    recovery_task = (await start([recovery], admission))[0]
    assert not recovery.entered_pool.is_set()
    holder.release.set()
    await holder_task
    await recovery.entered_pool.wait()
    recovery.release.set()
    await recovery_task
    assert pool.peak == 1
    await assert_one_permit(admission)


@pytest.mark.asyncio
async def test_cancellation_inside_work_releases_capacity_and_propagates() -> None:
    admission = WriteAdmission(1)
    pool = PoolProbe()
    write = Write("release", pool)
    task = (await start([write], admission))[0]
    assert write.entered_pool.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.active == 0
    await assert_one_permit(admission)


@pytest.mark.asyncio
async def test_cancellation_after_permit_wakeup_transfers_it_to_next_waiter() -> None:
    admission = WriteAdmission(1)
    holder = admission.slot()
    await holder.__aenter__()
    pool = PoolProbe()
    cancelled, next_writer = Write("cancelled", pool), Write("next", pool)
    tasks = await start([cancelled, next_writer], admission)
    assert pool.active == 0
    # Exit has no suspension once entered: wake the first waiter then cancel it
    # before it resumes. Its assigned permit must be returned to the next waiter.
    await holder.__aexit__(None, None, None)
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    await next_writer.entered_pool.wait()
    assert not cancelled.entered_pool.is_set()
    next_writer.release.set()
    await tasks[1]
    await assert_one_permit(admission)


@pytest.mark.asyncio
async def test_exception_releases_exactly_one_permit() -> None:
    admission = WriteAdmission(1)
    with pytest.raises(ValueError, match="database"):
        async with admission.slot():
            raise ValueError("database")
    await assert_one_permit(admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [0, -1, True, False, 1.5, "2", None])
async def test_capacity_must_be_a_positive_integer(capacity: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        WriteAdmission(capacity)  # type: ignore[arg-type]


def test_construction_requires_a_running_lifespan_loop() -> None:
    with pytest.raises(RuntimeError, match="no running event loop"):
        WriteAdmission(1)


@pytest.mark.parametrize("contended", [False, True])
def test_cross_loop_reuse_is_rejected_and_new_lifespan_is_independent(contended: bool) -> None:
    async def first_lifespan() -> WriteAdmission:
        admission = WriteAdmission(1)
        if contended:
            async with admission.slot():
                write = Write("waiting", PoolProbe())
                task = (await start([write], admission))[0]
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        return admission

    old = asyncio.run(first_lifespan())

    async def next_lifespan() -> None:
        with pytest.raises(RuntimeError, match="different event loop"):
            async with old.slot():
                pytest.fail("old lifespan admitted work on a new loop")
        await assert_one_permit(WriteAdmission(1))

    asyncio.run(next_lifespan())


@pytest.mark.asyncio
async def test_new_lifespan_on_same_loop_has_a_fresh_budget_after_joining_old_work() -> None:
    old = WriteAdmission(1)
    writes = [Write("active", PoolProbe()), Write("waiting", PoolProbe())]
    tasks = await start(writes, old)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    await assert_one_permit(WriteAdmission(1))


@pytest.mark.asyncio
async def test_control_manual_acquisition_without_cleanup_leaks_on_cancellation() -> None:
    """A naive consumer-local acquire/write/release loses capacity on cancellation."""
    semaphore = asyncio.BoundedSemaphore(1)
    entered = asyncio.Event()

    async def unsafe_write() -> None:
        await semaphore.acquire()
        entered.set()
        await asyncio.Future[None]()
        semaphore.release()

    task = asyncio.create_task(unsafe_write())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    attempted = asyncio.Event()
    recovered = asyncio.Event()

    async def recovery() -> None:
        attempted.set()
        async with semaphore:
            recovered.set()

    retry = asyncio.create_task(recovery())
    await attempted.wait()
    assert not recovered.is_set()
    assert not retry.done()
    # Clean up the intentionally broken control, then prove that the waiting
    # recovery was blocked by the leaked permit rather than by task scheduling.
    semaphore.release()
    await retry
    assert recovered.is_set()
