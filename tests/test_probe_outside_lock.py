"""The health probe must not run under the handle lock, and a busy driver is not a dead one.

Unit tests use a gated fake probe. The integration test (``NEO4J_URI`` set, ``integration`` marker) saturates a LIVE Neo4j
Bolt pool: a fake cannot reproduce the driver's real acquisition wait.
"""

import asyncio
import os
import time
from unittest.mock import Mock

import pytest

from common.db_resilience import AsyncResilientConnection, ResilientConnection
from common.neo4j_resilient import AsyncResilientNeo4jDriver


def _manager(probe, **kwargs) -> AsyncResilientConnection:  # type: ignore[no-untyped-def]
    manager: AsyncResilientConnection = AsyncResilientConnection(lambda: object(), probe, health_check_ttl=0.01, **kwargs)
    return manager


async def _seed(manager: AsyncResilientConnection) -> object:
    existing = object()
    manager._connection = existing
    manager._last_healthy_at = time.monotonic() - 1.0  # TTL expired
    return existing


class TestAsyncProbeOutsideLock:
    @pytest.mark.asyncio
    async def test_slow_probe_does_not_block_other_callers(self) -> None:
        release = asyncio.Event()

        async def slow_probe(_conn: object) -> bool:
            await release.wait()
            return True

        manager = _manager(slow_probe, probe_timeout=30.0)
        existing = await _seed(manager)
        first = asyncio.create_task(manager.get_connection())
        await asyncio.sleep(0.05)  # first caller is now parked inside the probe
        results = await asyncio.wait_for(asyncio.gather(*[manager.get_connection() for _ in range(10)]), timeout=1.0)
        assert all(r is existing for r in results)
        assert not first.done()
        release.set()
        assert await first is existing
        await manager.close()

    @pytest.mark.asyncio
    async def test_probe_timeout_means_busy_so_connection_is_kept(self) -> None:
        async def never_answers(_conn: object) -> bool:
            await asyncio.sleep(60)
            return True

        manager = _manager(never_answers, probe_timeout=0.05)
        existing = await _seed(manager)
        started = time.perf_counter()
        assert await manager.get_connection() is existing
        assert time.perf_counter() - started < 1.0
        assert manager._failed_probes == 0  # a busy driver is not a failed probe
        await manager.close()

    @pytest.mark.asyncio
    async def test_dead_connection_is_still_replaced_after_threshold(self) -> None:
        fresh = object()
        factory_calls = []

        def factory() -> object:
            factory_calls.append(1)
            return fresh

        manager: AsyncResilientConnection = AsyncResilientConnection(
            factory, Mock(side_effect=[False, False, False, True]), health_check_ttl=0.0, unhealthy_threshold=3, close_grace_period=0
        )
        existing = object()
        manager._connection = existing
        manager._last_healthy_at = time.monotonic() - 1.0
        assert await manager.get_connection() is existing
        assert await manager.get_connection() is existing
        assert await manager.get_connection() is fresh  # third consecutive failure: replaced
        assert factory_calls == [1]
        await manager.close()


class TestSyncDriverProbeTtl:
    def test_probe_runs_once_inside_ttl(self) -> None:
        probe = Mock(return_value=True)
        manager: ResilientConnection = ResilientConnection(lambda: object(), probe, health_check_ttl=30.0)
        conn = manager.get_connection()
        for _ in range(5):
            assert manager.get_connection() is conn
        assert probe.call_count == 1  # only the creation test; no per-call probe

    def test_ttl_zero_probes_every_call(self) -> None:
        probe = Mock(return_value=True)
        manager: ResilientConnection = ResilientConnection(lambda: object(), probe, health_check_ttl=0)
        manager.get_connection()
        manager.get_connection()
        assert probe.call_count == 2


_URI = os.environ.get("NEO4J_URI", "")


@pytest.mark.integration
@pytest.mark.skipif(not _URI, reason="NEO4J_URI not set")
@pytest.mark.asyncio
async def test_saturated_live_neo4j_does_not_stall_callers() -> None:
    driver = AsyncResilientNeo4jDriver(
        _URI,
        (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
        max_connection_pool_size=2,
        connection_acquisition_timeout=8.0,
    )
    driver.health_check_ttl = 0.2
    driver.probe_timeout = 1.0
    raw = await driver.get_connection()
    held = []
    for _ in range(2):
        session = raw.session(database="neo4j")
        await session.run("RETURN 1")
        held.append(session)
    await asyncio.sleep(0.4)

    async def timed() -> float:
        started = time.perf_counter()
        await driver.get_connection()
        return time.perf_counter() - started

    waits = await asyncio.gather(*[timed() for _ in range(20)])
    assert max(waits) < 4.0  # was one 8 s acquisition wait per queued caller
    assert driver._connection is raw  # a saturated driver was kept, not torn down
    for session in held:
        await session.close()
    await driver.close()
