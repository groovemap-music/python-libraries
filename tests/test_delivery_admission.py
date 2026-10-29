"""Admission never turns pool contention or cancellation into delivery loss."""

import asyncio

import pytest

from common import WriteAdmission


@pytest.mark.parametrize("capacity", [0, -1])
def test_admission_rejects_nonpositive_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        WriteAdmission(capacity)


@pytest.mark.asyncio
async def test_shared_capacity_and_cancelled_waiter_and_holder() -> None:
    admission = WriteAdmission(2)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = peak = 0

    async def write() -> None:
        nonlocal active, peak
        async with admission.slot():
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

    tasks = [asyncio.create_task(write()) for _ in range(8)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.sleep(0)
        assert active == peak == 2
        tasks[-1].cancel()
        tasks[0].cancel()
        results = await asyncio.gather(tasks[-1], tasks[0], return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks[1:-1]), timeout=2)
        # Reacquire the entire budget, not just one slot: a leak must fail here.
        async with asyncio.timeout(2), admission.slot(), admission.slot():
            assert active == 0 and peak == 2
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failure_returns_slot() -> None:
    admission = WriteAdmission(1)
    with pytest.raises(RuntimeError, match="write failed"):
        async with admission.slot():
            raise RuntimeError("write failed")
    async with asyncio.timeout(2), admission.slot():
        pass
