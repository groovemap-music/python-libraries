"""Checkout-probe TTL for AsyncPostgreSQLPool.

Unit tests count probes on fakes. The integration tests (``POSTGRES_HOST`` set, ``integration`` marker) run against a
LIVE Postgres: a mock cannot show that a backend killed server-side is recovered from.
"""

import os
from unittest.mock import AsyncMock, Mock, patch

import psycopg
import pytest
from psycopg.errors import OperationalError

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
    conn._cursor = cursor
    return conn


class TestCheckoutProbeTtl:
    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_probe_skipped_inside_ttl(self, mock_connect: Mock) -> None:
        conn = _fake_conn()
        mock_connect.return_value = conn
        pool = AsyncPostgreSQLPool({}, min_connections=1, max_connections=2, checkout_probe_ttl=30)
        await pool.initialize()
        conn._cursor.execute.reset_mock()
        for _ in range(5):
            async with pool.connection():
                pass
        assert conn._cursor.execute.await_count == 0
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_ttl_zero_probes_every_checkout(self, mock_connect: Mock) -> None:
        conn = _fake_conn()
        mock_connect.return_value = conn
        pool = AsyncPostgreSQLPool({}, min_connections=1, max_connections=2, checkout_probe_ttl=0)
        await pool.initialize()
        conn._cursor.execute.reset_mock()
        for _ in range(3):
            async with pool.connection():
                pass
        assert conn._cursor.execute.await_count == 3
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.monotonic")
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_probe_resumes_after_ttl(self, mock_connect: Mock, mock_clock: Mock) -> None:
        conn = _fake_conn()
        mock_connect.return_value = conn
        mock_clock.return_value = 1000.0
        pool = AsyncPostgreSQLPool({}, min_connections=1, max_connections=2, checkout_probe_ttl=30)
        await pool.initialize()
        conn._cursor.execute.reset_mock()
        mock_clock.return_value = 1031.0
        async with pool.connection():
            pass
        assert conn._cursor.execute.await_count == 1
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_locally_closed_or_broken_connection_is_replaced(self, mock_connect: Mock) -> None:
        dead, fresh = _fake_conn(), _fake_conn()
        mock_connect.side_effect = [dead, fresh]
        pool = AsyncPostgreSQLPool({}, min_connections=1, max_connections=2, checkout_probe_ttl=30)
        await pool.initialize()
        dead.broken = True
        dead._cursor.execute.side_effect = OperationalError("dead")
        async with pool.connection() as got:
            assert got is fresh
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_error_in_use_forces_reprobe_of_idle_siblings(self, mock_connect: Mock) -> None:
        first, second = _fake_conn(), _fake_conn()
        mock_connect.side_effect = [first, second]
        pool = AsyncPostgreSQLPool({}, min_connections=2, max_connections=2, checkout_probe_ttl=30)
        await pool.initialize()
        second._cursor.execute.reset_mock()
        with pytest.raises(OperationalError):
            async with pool.connection():
                raise OperationalError("server closed the connection")
        async with pool.connection() as got:
            assert got is second
        assert second._cursor.execute.await_count == 1
        await pool.close()


_HOST = os.environ.get("POSTGRES_HOST", "")
_PARAMS = {
    "host": _HOST,
    "port": int(os.environ.get("POSTGRES_PORT", "5432")),
    "user": os.environ.get("POSTGRES_USER", "u"),
    "password": os.environ.get("POSTGRES_PASSWORD", "pw"),
    "dbname": os.environ.get("POSTGRES_DB", "d"),
}


@pytest.mark.integration
@pytest.mark.skipif(not _HOST, reason="POSTGRES_HOST not set")
class TestCheckoutProbeTtlLivePostgres:
    @pytest.mark.asyncio
    async def test_backend_killed_inside_ttl_surfaces_once_then_recovers(self) -> None:
        pool = AsyncPostgreSQLPool(_PARAMS, min_connections=2, max_connections=2, checkout_probe_ttl=30)
        await pool.initialize()
        async with pool.connection() as c, c.cursor() as cur:
            await cur.execute("SELECT 1")
        # Kill every pooled backend from outside, inside the TTL window.
        async with await psycopg.AsyncConnection.connect(**_PARAMS, autocommit=True) as admin:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = %s",
                (_PARAMS["dbname"],),
            )
        failures = 0
        for _ in range(3):
            try:
                async with pool.connection() as c, c.cursor() as cur:
                    await cur.execute("SELECT 42")
                    assert (await cur.fetchone()) == (42,)
            except psycopg.OperationalError, psycopg.InterfaceError:
                failures += 1
        assert failures == 1  # exactly one caller sees the dead connection; the pool then re-probes and heals
        await pool.close()

    @pytest.mark.asyncio
    async def test_backend_killed_after_ttl_is_probed_and_replaced_transparently(self) -> None:
        pool = AsyncPostgreSQLPool(_PARAMS, min_connections=1, max_connections=1, checkout_probe_ttl=0.05)
        await pool.initialize()
        async with await psycopg.AsyncConnection.connect(**_PARAMS, autocommit=True) as admin:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = %s",
                (_PARAMS["dbname"],),
            )
        import asyncio

        await asyncio.sleep(0.1)
        async with pool.connection() as c, c.cursor() as cur:
            await cur.execute("SELECT 42")
            assert (await cur.fetchone()) == (42,)
        await pool.close()
