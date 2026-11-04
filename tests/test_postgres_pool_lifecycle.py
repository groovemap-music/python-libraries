"""Idle shrink, lifetime recycling and wait metrics for AsyncPostgreSQLPool.

Unit tests drive the health loop on fakes. The integration tests (``POSTGRES_HOST`` set, ``integration`` marker) count real
server backends in ``pg_stat_activity``: a mock cannot show that a backend was actually released.
"""

import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch

import psycopg
import pytest

from common.postgres_resilient import AsyncPostgreSQLPool


def _fake_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.closed = False
    conn.broken = False
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(1,))
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=None)
    conn.cursor = Mock(return_value=cursor)
    return conn


async def _tick(pool: AsyncPostgreSQLPool) -> None:
    """Run one health-loop pass."""
    pool._closed = False
    with (
        patch("common.postgres_resilient.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()])),
        pytest.raises(asyncio.CancelledError),
    ):
        await pool._health_check_loop()


class TestIdleAndLifetime:
    @pytest.mark.asyncio
    @patch("common.postgres_resilient.monotonic")
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_idle_connections_shrink_to_min(self, mock_connect: Mock, clock: Mock) -> None:
        conns = [_fake_conn() for _ in range(4)]
        mock_connect.side_effect = conns
        clock.return_value = 0.0
        pool = AsyncPostgreSQLPool({}, min_connections=1, max_connections=4, idle_timeout=60, max_lifetime=None)
        await pool.initialize()
        pool._health_check_task.cancel()  # type: ignore[union-attr]
        for _ in range(3):
            c = await pool._create_connection()
            pool.active_connections += 1
            pool.connections.put_nowait(c)
        assert pool.connections.qsize() == 4
        clock.return_value = 61.0
        await _tick(pool)
        assert pool.connections.qsize() == 1  # never below min_connections
        assert pool.active_connections == 1
        assert sum(c.close.await_count for c in conns) == 3

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.monotonic")
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_recently_used_connection_is_not_closed(self, mock_connect: Mock, clock: Mock) -> None:
        mock_connect.side_effect = [_fake_conn(), _fake_conn()]
        clock.return_value = 0.0
        pool = AsyncPostgreSQLPool({}, min_connections=0, max_connections=2, idle_timeout=60, max_lifetime=None)
        await pool.initialize()
        pool._health_check_task.cancel()  # type: ignore[union-attr]
        async with pool.connection():
            pass
        clock.return_value = 30.0
        await _tick(pool)
        assert pool.connections.qsize() == 1

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.monotonic")
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_old_connection_is_recycled_and_min_is_restored(self, mock_connect: Mock, clock: Mock) -> None:
        old, new = _fake_conn(), _fake_conn()
        mock_connect.side_effect = [old, new]
        clock.return_value = 0.0
        pool = AsyncPostgreSQLPool({}, min_connections=1, max_connections=2, idle_timeout=None, max_lifetime=100)
        await pool.initialize()
        pool._health_check_task.cancel()  # type: ignore[union-attr]
        clock.return_value = 101.0
        await _tick(pool)
        old.close.assert_awaited()
        assert pool.connections.qsize() == 1
        assert pool.connections.get_nowait() is new

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_none_disables_both_rules(self, mock_connect: Mock) -> None:
        conn = _fake_conn()
        mock_connect.return_value = conn
        pool = AsyncPostgreSQLPool({}, min_connections=0, max_connections=2, idle_timeout=None, max_lifetime=None)
        await pool.initialize()
        pool._health_check_task.cancel()  # type: ignore[union-attr]
        with patch("common.postgres_resilient.monotonic", return_value=10**9):
            assert pool._retire_reason(conn, keep_floor_ok=True) is None

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pending_is_counted_while_waiting_and_wait_is_recorded(self, mock_connect: Mock) -> None:
        mock_connect.return_value = _fake_conn()
        pool = AsyncPostgreSQLPool({}, min_connections=0, max_connections=1, idle_timeout=None, max_lifetime=None)
        await pool.initialize()
        pool._health_check_task.cancel()  # type: ignore[union-attr]
        release = asyncio.Event()

        async def holder() -> None:
            async with pool.connection():
                await release.wait()

        first = asyncio.create_task(holder())
        await asyncio.sleep(0.05)
        second = asyncio.create_task(holder())
        await asyncio.sleep(0.05)
        idle, in_use, pending = pool.pool_stats()
        assert (idle, in_use, pending) == (0, 1, 1)
        release.set()
        await asyncio.gather(first, second)
        assert pool.pool_stats()[2] == 0


_HOST = os.environ.get("POSTGRES_HOST", "")
_PARAMS = {
    "host": _HOST,
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ.get("POSTGRES_USER", "u"),
    "password": os.environ.get("POSTGRES_PASSWORD", "pw"),
    "dbname": os.environ.get("POSTGRES_DB", "d"),
}


async def _backends(admin: psycopg.AsyncConnection, application_name: str) -> int:
    cur = await admin.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = %s", (application_name,))
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.integration
@pytest.mark.skipif(not _HOST, reason="POSTGRES_HOST not set")
class TestLivePostgresBackends:
    @pytest.mark.asyncio
    async def test_burst_backends_are_released_when_idle(self) -> None:
        app = "gm-adqd-pool-test"
        pool = AsyncPostgreSQLPool(
            {**_PARAMS, "application_name": app}, min_connections=1, max_connections=8, health_check_interval=1, idle_timeout=1.5, max_lifetime=None
        )
        await pool.initialize()
        release = asyncio.Event()

        async def hold() -> None:
            async with pool.connection() as c:
                await c.execute("SELECT 1")
                await release.wait()

        tasks = [asyncio.create_task(hold()) for _ in range(8)]
        await asyncio.sleep(0.5)
        async with await psycopg.AsyncConnection.connect(**_PARAMS, autocommit=True) as admin:
            peak = await _backends(admin, app)
            release.set()
            await asyncio.gather(*tasks)
            for _ in range(40):
                await asyncio.sleep(0.25)
                settled = await _backends(admin, app)
                if settled <= 1:
                    break
        assert peak == 8
        assert settled == 1  # shrunk to min_connections; before this change it stayed at 8 until restart
        await pool.close()
