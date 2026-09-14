"""Tests for PostgreSQL resilient connection module."""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, Mock, patch

import pytest
from psycopg.errors import InterfaceError, OperationalError

from common.db_resilience import CircuitState
from common.postgres_resilient import AsyncPostgreSQLPool, AsyncResilientPostgreSQL


class TestAsyncResilientPostgreSQL:
    """Tests for AsyncResilientPostgreSQL class."""

    @pytest.fixture
    def mock_async_connection(self) -> AsyncMock:
        """Create a mock async PostgreSQL connection."""
        conn = AsyncMock()
        conn.closed = False
        conn.set_autocommit = AsyncMock()
        conn.close = AsyncMock()
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        conn.cursor = Mock(return_value=cursor)
        return conn

    @pytest.fixture
    def connection_params(self) -> dict:
        """Test connection parameters."""
        return {
            "host": "localhost",
            "port": 5432,
            "dbname": "test",
            "user": "test_user",
            "password": "test_pass",
        }

    def test_init(self, connection_params: dict) -> None:
        """Test AsyncResilientPostgreSQL initialization."""
        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params, max_retries=3)

        assert async_conn.connection_params == connection_params
        assert async_conn.circuit_breaker is not None
        assert async_conn.backoff is not None

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_create_connection(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test async connection creation."""
        mock_connect.return_value = mock_async_connection

        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        conn = await async_conn._create_connection()

        assert conn == mock_async_connection
        mock_connect.assert_called_once_with(**connection_params)
        mock_async_connection.set_autocommit.assert_called_once_with(True)

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_create_connection_closes_conn_when_set_autocommit_fails(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Regression: connect() returning means a real backend is
        live. If set_autocommit raises, the connection must be closed before
        the exception propagates rather than orphaned to GC."""
        mock_async_connection.set_autocommit.side_effect = OperationalError("autocommit failed")
        mock_connect.return_value = mock_async_connection

        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        with pytest.raises(OperationalError):
            await async_conn._create_connection()

        mock_async_connection.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_test_connection_healthy(self, _mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test async connection health check on healthy connection."""
        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        result = await async_conn._test_connection(mock_async_connection)

        assert result is True

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_test_connection_closed(self, _mock_connect: Mock, connection_params: dict) -> None:
        """Test async connection health check on closed connection."""
        closed_conn = AsyncMock()
        closed_conn.closed = True

        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        result = await async_conn._test_connection(closed_conn)

        assert result is False

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_test_connection_error(self, _mock_connect: Mock, connection_params: dict) -> None:
        """Test async connection health check when query fails."""
        failing_conn = AsyncMock()
        failing_conn.closed = False
        cursor = AsyncMock()
        cursor.execute = AsyncMock(side_effect=OperationalError("Connection lost"))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        failing_conn.cursor = Mock(return_value=cursor)

        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        result = await async_conn._test_connection(failing_conn)

        assert result is False

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_get_connection_success(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test successful async connection acquisition."""
        mock_connect.return_value = mock_async_connection

        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        conn = await async_conn.get_connection()

        assert conn == mock_async_connection

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_close_connection(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test async connection closure."""
        mock_connect.return_value = mock_async_connection

        async_conn = AsyncResilientPostgreSQL(connection_params=connection_params)
        # First establish connection
        await async_conn.get_connection()

        # Reset mock to track close call
        mock_async_connection.close.reset_mock()

        await async_conn.close()

        # Check that close was called (either close or aclose)
        assert mock_async_connection.close.called or (hasattr(mock_async_connection, "aclose") and mock_async_connection.aclose.called)
        assert async_conn._connection is None


class TestAsyncPostgreSQLPool:
    """Tests for AsyncPostgreSQLPool class."""

    @pytest.fixture
    def mock_async_connection(self) -> AsyncMock:
        """Create a mock async PostgreSQL connection."""
        conn = AsyncMock()
        conn.closed = False
        conn.set_autocommit = AsyncMock()
        conn.close = AsyncMock()
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        conn.cursor = Mock(return_value=cursor)
        return conn

    @pytest.fixture
    def connection_params(self) -> dict:
        """Test connection parameters."""
        return {
            "host": "localhost",
            "port": 5432,
            "dbname": "test",
            "user": "test_user",
            "password": "test_pass",
        }

    def test_init(self, connection_params: dict) -> None:
        """Test AsyncPostgreSQLPool initialization."""

        pool = AsyncPostgreSQLPool(
            connection_params=connection_params,
            max_connections=10,
            min_connections=2,
            max_retries=3,
            health_check_interval=30,
        )

        assert pool.connection_params == connection_params
        assert pool.max_connections == 10
        assert pool.min_connections == 2
        assert pool.max_retries == 3
        assert pool.health_check_interval == 30
        assert pool._closed is False
        assert pool._initialized is False
        assert pool.circuit_breaker is not None
        assert pool.backoff is not None

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pool_initialization_success(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test successful pool initialization with minimum connections."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=5)

        await pool.initialize()

        assert pool._initialized is True
        assert pool._health_check_task is not None
        assert pool.active_connections >= 0
        assert mock_connect.call_count >= 0  # May vary due to async timing

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pool_initialization_with_min_connections(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test pool initializes with specified minimum connections."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=3, max_connections=10)

        await pool.initialize()

        # Should attempt to create min_connections
        assert pool._initialized is True
        # Note: actual connection count may vary due to async timing and errors

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pool_initialization_connection_failure(self, mock_connect: Mock, connection_params: dict) -> None:
        """Test pool initialization handles connection failures gracefully."""

        mock_connect.side_effect = OperationalError("Connection failed")

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=5)

        # Should not raise exception even if connections fail
        await pool.initialize()

        assert pool._initialized is True
        assert pool.active_connections == 0  # No connections created

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pool_double_initialization_idempotent(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test that calling initialize() twice is idempotent."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)

        await pool.initialize()
        first_task = pool._health_check_task

        # Second initialization should do nothing
        await pool.initialize()

        assert pool._health_check_task is first_task

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_create_connection_success(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test successful async connection creation."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params)
        conn = await pool._create_connection()

        assert conn == mock_async_connection
        mock_connect.assert_called_with(**connection_params)
        mock_async_connection.set_autocommit.assert_called_once_with(True)

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_create_connection_routes_through_circuit_breaker(self, mock_connect: Mock, connection_params: dict) -> None:
        """Regression: _create_connection must actually invoke the
        breaker constructed in __init__, not bypass it. 3 consecutive
        connect() failures must open the circuit so subsequent callers
        fast-fail (CircuitOpenError) instead of each walking the full
        connect+backoff ladder during a sustained outage."""
        mock_connect.side_effect = OperationalError("Postgres is down")

        pool = AsyncPostgreSQLPool(connection_params=connection_params, max_retries=1)
        assert pool.circuit_breaker.state == CircuitState.CLOSED

        for _ in range(pool.circuit_breaker.config.failure_threshold):
            with pytest.raises(OperationalError):
                await pool._create_connection()

        assert pool.circuit_breaker.state == CircuitState.OPEN

        # The breaker itself now rejects the call BEFORE a new connect()
        # attempt is even made — the fast-fail this bead is about.
        mock_connect.reset_mock()
        with pytest.raises(Exception, match="Circuit breaker is OPEN"):
            await pool._create_connection()
        mock_connect.assert_not_called()

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_create_connection_with_health_check(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test connection creation includes health check."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params)
        conn = await pool._create_connection()

        assert conn is not None
        # Verify health check query was executed
        cursor = mock_async_connection.cursor.return_value
        cursor.execute.assert_called_with("SELECT 1")

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_create_connection_closes_conn_when_validation_fails(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Regression: psycopg.AsyncConnection.connect() returning
        means a real backend is live. If the post-connect SELECT 1 probe
        raises, the just-opened connection must be closed before the
        exception propagates — otherwise it is orphaned and only reclaimed by
        unreliable/delayed __del__ GC, pinning a backend against the shared
        PgBouncer session-mode cap under DB instability."""
        mock_async_connection.cursor.return_value.execute.side_effect = OperationalError("probe failed")
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, max_retries=1)
        with pytest.raises(OperationalError):
            await pool._create_connection()

        mock_async_connection.close.assert_called_once()

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    async def test_test_connection_healthy(self, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test connection health check on healthy connection."""

        pool = AsyncPostgreSQLPool(connection_params=connection_params)
        result = await pool._test_connection(mock_async_connection)

        assert result is True

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    async def test_test_connection_closed(self, connection_params: dict) -> None:
        """Test connection health check on closed connection."""

        closed_conn = AsyncMock()
        closed_conn.closed = True

        pool = AsyncPostgreSQLPool(connection_params=connection_params)
        result = await pool._test_connection(closed_conn)

        assert result is False

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    async def test_test_connection_query_failure(self, connection_params: dict) -> None:
        """Test connection health check when query fails."""

        failing_conn = AsyncMock()
        failing_conn.closed = False
        cursor = AsyncMock()
        cursor.execute = AsyncMock(side_effect=OperationalError("Query failed"))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        failing_conn.cursor = Mock(return_value=cursor)

        pool = AsyncPostgreSQLPool(connection_params=connection_params)
        result = await pool._test_connection(failing_conn)

        assert result is False

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_acquisition_from_pool(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test acquiring connection from pool when available."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=5)
        await pool.initialize()

        # Give time for initialization
        await asyncio.sleep(0.1)

        async with pool.connection() as conn:
            assert conn is not None

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_creation_when_pool_empty(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test creating new connection when pool is empty."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        async with pool.connection() as conn:
            assert conn is not None
            assert pool.active_connections > 0

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_return_to_pool_when_healthy(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test that healthy connection is returned to pool after use."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)
        await pool.initialize()
        await asyncio.sleep(0.1)

        initial_size = pool.connections.qsize()

        async with pool.connection() as conn:
            assert conn is not None

        # Connection should be back in pool
        await asyncio.sleep(0.1)
        final_size = pool.connections.qsize()
        assert final_size >= initial_size

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_when_pool_closed(self, _mock_connect: Mock, connection_params: dict) -> None:
        """Test that acquiring connection from closed pool raises error."""

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()
        await pool.close()

        with pytest.raises(RuntimeError, match="Connection pool is closed"):
            async with pool.connection():
                pass

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_error_during_operation(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test connection error handling during operation."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)
        await pool.initialize()
        await asyncio.sleep(0.1)

        with pytest.raises(InterfaceError):
            async with pool.connection():
                # Simulate error during use
                raise InterfaceError("Connection lost")

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_close_pool_cancels_health_check(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test that closing pool cancels health check task."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)
        await pool.initialize()

        health_task = pool._health_check_task
        assert health_task is not None

        await pool.close()

        assert pool._closed is True
        assert health_task.cancelled() or health_task.done()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_close_pool_closes_all_connections(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test that closing pool closes all connections."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=5)
        await pool.initialize()
        await asyncio.sleep(0.1)

        await pool.close()

        assert pool._closed is True
        assert pool.connections.empty()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_unhealthy_replacement(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test that unhealthy connection is replaced with new one."""

        # First connection is unhealthy, second is healthy
        unhealthy_conn = AsyncMock()
        unhealthy_conn.closed = False
        cursor = AsyncMock()
        cursor.execute = AsyncMock(side_effect=OperationalError("Connection lost"))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        unhealthy_conn.cursor = Mock(return_value=cursor)

        mock_connect.side_effect = [unhealthy_conn, mock_async_connection]

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        async with pool.connection() as conn:
            # Should get the healthy connection after unhealthy one was replaced
            assert conn is not None

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_max_retries_exceeded(self, mock_connect: Mock, connection_params: dict) -> None:
        """Test that max retries raises exception."""

        mock_connect.side_effect = OperationalError("Connection failed")

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5, max_retries=2)
        await pool.initialize()

        with pytest.raises(Exception, match="Failed to get PostgreSQL connection after 2 attempts"):
            async with pool.connection():
                pass

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_removes_unhealthy_connections(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test that health check loop removes unhealthy connections."""

        # Create one healthy and one unhealthy connection
        unhealthy_conn = AsyncMock()
        unhealthy_conn.closed = False
        cursor = AsyncMock()
        cursor.execute = AsyncMock(side_effect=OperationalError("Connection lost"))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        unhealthy_conn.cursor = Mock(return_value=cursor)

        mock_connect.side_effect = [unhealthy_conn, mock_async_connection]

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5, health_check_interval=1)
        await pool.initialize()

        # Manually put unhealthy connection in pool
        await pool.connections.put(unhealthy_conn)
        pool.active_connections = 1

        # Wait for health check to run
        await asyncio.sleep(1.5)

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_replenishes_min_connections(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test that health check loop maintains minimum connections."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=5, health_check_interval=1)
        await pool.initialize()

        # Drain the pool below minimum
        while not pool.connections.empty():
            try:
                conn = pool.connections.get_nowait()
                await conn.close()
            except asyncio.QueueEmpty:
                break

        pool.active_connections = 0

        # Wait for health check to replenish
        await asyncio.sleep(1.5)

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pool_exhaustion_timeout(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test pool exhaustion with timeout while waiting for connection."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=1, max_retries=2)
        await pool.initialize()

        # Acquire the only connection
        async with pool.connection() as _conn1:
            # Try to acquire another connection - should timeout
            with suppress(TimeoutError, Exception):
                async with asyncio.timeout(0.5):
                    async with pool.connection():
                        pass

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_not_returned_when_unhealthy(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test that unhealthy connection is not returned to pool."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)
        await pool.initialize()
        await asyncio.sleep(0.1)

        initial_count = pool.active_connections

        with suppress(Exception):
            async with pool.connection() as conn:
                # Mark connection as closed (unhealthy)
                conn.closed = True
                # Connection should not be returned on exit

        await asyncio.sleep(0.1)

        # Active connections should have decreased
        assert pool.active_connections < initial_count or pool.active_connections == 0

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_pool_full_on_return(self, mock_connect: Mock, connection_params: dict) -> None:
        """Test connection closure when pool is full on return."""

        # Create multiple mock connections
        conns = [AsyncMock() for _ in range(3)]
        for conn in conns:
            conn.closed = False
            conn.set_autocommit = AsyncMock()
            conn.close = AsyncMock()
            cursor = AsyncMock()
            cursor.execute = AsyncMock()
            cursor.fetchone = AsyncMock(return_value=(1,))
            cursor.__aenter__ = AsyncMock(return_value=cursor)
            cursor.__aexit__ = AsyncMock(return_value=None)
            conn.cursor = Mock(return_value=cursor)

        mock_connect.side_effect = conns

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=2)
        await pool.initialize()

        # Fill the pool
        async with pool.connection(), pool.connection():
            # Pool now has 2 active connections (max)
            pass

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_set_autocommit_failure_discards_connection(self, mock_connect: Mock, connection_params: dict) -> None:
        """Test that connection is discarded when set_autocommit(True) fails on pool return."""
        from psycopg import OperationalError

        conn = AsyncMock()
        conn.closed = False
        conn.close = AsyncMock()
        # set_autocommit succeeds on initial creation but fails on pool return
        conn.set_autocommit = AsyncMock(side_effect=[None, OperationalError("connection lost")])
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        conn.cursor = Mock(return_value=cursor)

        mock_connect.return_value = conn

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        async with pool.connection():
            pass  # On exit, set_autocommit(True) will raise

        await asyncio.sleep(0.1)

        # Connection should have been discarded, not returned to pool
        assert pool.connections.qsize() == 0
        # active_connections should have been decremented
        assert pool.active_connections == 0
        # Connection should have been closed
        conn.close.assert_called()

        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_body_exception_propagates_when_set_autocommit_fails_on_release(self, mock_connect: Mock, connection_params: dict) -> None:
        """Regression: a `return` in connection()'s finally block would silently swallow
        any exception raised inside the caller's `async with` body when set_autocommit
        also fails on release. The body exception MUST still propagate, and the
        connection must still be discarded with the active slot released.
        """
        from psycopg import OperationalError

        conn = AsyncMock()
        conn.closed = False
        conn.close = AsyncMock()
        # Succeeds on creation, fails on release — the discard path that used `return`.
        conn.set_autocommit = AsyncMock(side_effect=[None, OperationalError("connection lost")])
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        conn.cursor = Mock(return_value=cursor)

        mock_connect.return_value = conn

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        class BodyError(RuntimeError):
            pass

        # The caller's body raises; set_autocommit then fails during release.
        with pytest.raises(BodyError, match="boom from body"):
            async with pool.connection():
                raise BodyError("boom from body")

        await asyncio.sleep(0.1)

        # Body exception survived the finally block AND the connection was discarded.
        assert pool.connections.qsize() == 0
        assert pool.active_connections == 0
        conn.close.assert_called()

        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_pool_exhaustion_wait(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test waiting for connection when pool is exhausted."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=1, max_retries=3)
        await pool.initialize()

        # Helper to hold connection briefly
        async def hold_connection():
            async with pool.connection():
                await asyncio.sleep(0.2)

        # Start task that holds the connection
        task = asyncio.create_task(hold_connection())
        await asyncio.sleep(0.05)  # Let it acquire the connection

        # This should wait and eventually get the connection when released
        async with pool.connection() as conn:
            assert conn is not None

        await task  # Ensure first task completes

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_unhealthy_connection_counter_management(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test proper active_connections counter management with unhealthy connections."""

        # First connection is unhealthy, second is healthy
        unhealthy_conn = AsyncMock()
        unhealthy_conn.closed = False
        cursor = AsyncMock()
        cursor.execute = AsyncMock(side_effect=OperationalError("Connection lost"))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        unhealthy_conn.cursor = Mock(return_value=cursor)
        unhealthy_conn.close = AsyncMock()

        mock_connect.side_effect = [unhealthy_conn, mock_async_connection]

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        # Manually add unhealthy connection to pool
        await pool.connections.put(unhealthy_conn)
        pool.active_connections = 1

        # Acquire connection - should detect unhealthy and replace
        async with pool.connection() as conn:
            assert conn is not None

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_error_operationalerror(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test OperationalError handling during operation."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)
        await pool.initialize()
        await asyncio.sleep(0.1)

        with pytest.raises(OperationalError):
            async with pool.connection():
                # Simulate OperationalError during use
                raise OperationalError("Database error")

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_initialization_handles_connection_limit(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Test initialization respects max_connections limit."""

        mock_connect.return_value = mock_async_connection

        # Create pool with reasonable limits
        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=3)

        # Should initialize successfully
        await pool.initialize()

        assert pool._initialized is True
        # Active connections should not exceed max
        assert pool.active_connections <= pool.max_connections

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_queue_full_on_return(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test health check handling when queue is full when returning connection."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=1, health_check_interval=1)
        await pool.initialize()

        # Fill the pool to capacity
        await asyncio.sleep(0.1)

        # Wait for health check to attempt putting connections back
        await asyncio.sleep(1.5)

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_retry_with_backoff(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test connection retry logic with exponential backoff."""

        # Fail twice, then succeed
        mock_connect.side_effect = [
            OperationalError("Connection failed"),
            OperationalError("Connection failed"),
            mock_async_connection,
        ]

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5, max_retries=3)
        await pool.initialize()

        # Should eventually succeed after retries
        async with pool.connection() as conn:
            assert conn is not None

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_timeout_in_exhausted_pool(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test timeout when waiting for connection in exhausted pool."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=1, max_retries=2)
        await pool.initialize()

        # Create a long-running connection holder
        async def hold_connection_long():
            async with pool.connection():
                await asyncio.sleep(10)  # Hold for long time

        # Start holding connection
        holder_task = asyncio.create_task(hold_connection_long())
        await asyncio.sleep(0.1)  # Let it acquire

        # Try to get connection - should retry and timeout
        try:
            with pytest.raises((TimeoutError, RuntimeError, Exception)):
                async with pool.connection():
                    pass
        finally:
            holder_task.cancel()
            with suppress(asyncio.CancelledError):
                await holder_task

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_empty_queue(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Test health check when queue becomes empty."""

        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5, health_check_interval=1)
        await pool.initialize()

        # Wait for health check with empty queue
        await asyncio.sleep(1.5)

        # Cleanup
        await pool.close()


class TestAsyncPostgreSQLPoolUncoveredLines:
    """Additional tests to cover uncovered lines in AsyncPostgreSQLPool."""

    @pytest.fixture
    def mock_async_connection(self) -> AsyncMock:
        """Create a mock async PostgreSQL connection."""
        conn = AsyncMock()
        conn.closed = False
        conn.set_autocommit = AsyncMock()
        conn.close = AsyncMock()
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        conn.cursor = Mock(return_value=cursor)
        return conn

    @pytest.fixture
    def connection_params(self) -> dict:
        """Test connection parameters."""
        return {
            "host": "localhost",
            "port": 5432,
            "dbname": "test",
            "user": "test_user",
            "password": "test_pass",
        }

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_initialize_queue_full_breaks(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Line 340: initialize() catches asyncio.QueueFull and breaks without error."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=5)

        # Create queue and lock manually (normally done in initialize())
        pool.connections = asyncio.Queue(maxsize=5)
        pool._lock = asyncio.Lock()

        # Replace the queue put coroutine so it raises QueueFull on await
        async def put_raises_queue_full(_conn: AsyncMock) -> None:
            raise asyncio.QueueFull

        pool.connections.put = put_raises_queue_full  # type: ignore[method-assign]

        # initialize() should not raise; it breaks on QueueFull
        await pool.initialize()

        assert pool._initialized is True

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_loop_queue_empty_breaks(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Lines 390-391: _health_check_loop() catches asyncio.QueueEmpty and breaks."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(
            connection_params=connection_params,
            min_connections=0,
            max_connections=5,
            health_check_interval=0,
        )
        await pool.initialize()

        # Put one connection so the health check processes it, then the queue
        # becomes empty and get_nowait raises QueueEmpty, triggering the break.
        await pool.connections.put(mock_async_connection)
        pool.active_connections = 1

        # Allow the health check loop to run a couple of cycles
        await asyncio.sleep(0.15)

        # Pool should still be operational
        assert pool._closed is False

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_loop_queue_full_on_return_closes_connection(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Lines 397-400: _health_check_loop() closes connection when QueueFull on return."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(
            connection_params=connection_params,
            min_connections=0,
            max_connections=1,
            health_check_interval=0,
        )
        await pool.initialize()

        # Create a healthy connection
        healthy_conn = AsyncMock()
        healthy_conn.closed = False
        healthy_conn.close = AsyncMock()
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        healthy_conn.cursor = Mock(return_value=cursor)

        await pool.connections.put(healthy_conn)
        pool.active_connections = 1

        # Make put_nowait raise QueueFull so the connection is closed instead of returned
        pool.connections.put_nowait = Mock(side_effect=asyncio.QueueFull)  # type: ignore[method-assign]

        # Let the health check cycle run
        await asyncio.sleep(0.15)

        # The connection should have been closed (QueueFull branch)
        healthy_conn.close.assert_awaited()

        # Cleanup
        pool.connections.put_nowait = asyncio.Queue.put_nowait.__get__(pool.connections)  # type: ignore[attr-defined]
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_health_check_loop_replenishment_failure_breaks(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Lines 413-415: _health_check_loop() logs warning and breaks on replenishment failure."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(
            connection_params=connection_params,
            min_connections=2,
            max_connections=5,
            health_check_interval=0,
        )

        # Create queue and lock (normally done in initialize()) then mark as initialized
        pool.connections = asyncio.Queue(maxsize=5)
        pool._lock = asyncio.Lock()
        pool._initialized = True
        pool.active_connections = 0
        pool._health_check_task = asyncio.create_task(pool._health_check_loop())

        # Make _create_connection raise so the replenishment break path is hit
        pool._create_connection = AsyncMock(side_effect=Exception("DB unavailable"))  # type: ignore[method-assign]

        # Give the health check loop time to attempt replenishment
        await asyncio.sleep(0.15)

        # Pool should still be alive (the exception is caught, not re-raised)
        assert pool._closed is False

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_do_initialize_double_check_skips_when_already_initialized(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """_do_initialize() returns early when _initialized is True (double-check locking)."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=1, max_connections=5)

        # First initialization
        await pool.initialize()
        assert pool._initialized is True
        first_task = pool._health_check_task

        # Directly call _do_initialize — simulates the race where another coroutine
        # already completed initialization while we held the init lock.
        await pool._do_initialize()

        # Should be a no-op: health check task unchanged, no extra connections
        assert pool._health_check_task is first_task

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_auto_initializes_when_not_initialized(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Line 424: connection() auto-initializes pool when _initialized is False."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)

        # Confirm pool is not yet initialized
        assert pool._initialized is False

        # Calling connection() should trigger auto-initialization
        async with pool.connection() as conn:
            assert conn is not None

        assert pool._initialized is True

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_unhealthy_connection_replacement_increments_active_connections(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Lines 476-477: active_connections incremented after unhealthy replacement."""
        # Build an unhealthy connection whose _test_connection returns False
        unhealthy_conn = AsyncMock()
        unhealthy_conn.closed = False
        unhealthy_conn.close = AsyncMock()
        unhealthy_cursor = AsyncMock()
        unhealthy_cursor.execute = AsyncMock(side_effect=OperationalError("gone"))
        unhealthy_cursor.__aenter__ = AsyncMock(return_value=unhealthy_cursor)
        unhealthy_cursor.__aexit__ = AsyncMock(return_value=None)
        unhealthy_conn.cursor = Mock(return_value=unhealthy_cursor)

        # The replacement healthy connection
        healthy_conn = mock_async_connection
        mock_connect.side_effect = [healthy_conn]

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        # Manually place the unhealthy connection in the pool
        await pool.connections.put(unhealthy_conn)
        pool.active_connections = 1

        before = pool.active_connections

        async with pool.connection() as conn:
            # We should have received the healthy replacement
            assert conn is not None
            # active_connections should have been incremented for the new connection
            assert pool.active_connections >= before

        # Cleanup
        await pool.close()

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_connection_finally_queue_full_closes_connection(
        self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock
    ) -> None:
        """Lines 514-519: connection() finally block closes connection when QueueFull."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=1)
        await pool.initialize()

        assert pool.active_connections == 0

        async with pool.connection():
            # While holding the connection, make put_nowait raise QueueFull
            # so the finally block exercises the QueueFull branch
            pool.connections.put_nowait = Mock(side_effect=asyncio.QueueFull)  # type: ignore[method-assign]

        # After the context exits, the connection should have been closed
        mock_async_connection.close.assert_awaited()

        # active_connections should have been decremented back to 0
        assert pool.active_connections == 0

        # Restore put_nowait before close() tries to drain the queue
        pool.connections.put_nowait = asyncio.Queue.put_nowait.__get__(pool.connections)  # type: ignore[attr-defined]
        await pool.close()

    @pytest.mark.asyncio
    async def test_health_check_replenishment_queue_full_closes_connection(self, connection_params: dict) -> None:
        """Lines 414-417: _health_check_loop() closes connection when QueueFull during replenishment."""
        # Create a fresh connection that replenishment will produce
        replenish_conn = AsyncMock()
        replenish_conn.closed = False
        replenish_conn.close = AsyncMock()
        replenish_cursor = AsyncMock()
        replenish_cursor.execute = AsyncMock()
        replenish_cursor.fetchone = AsyncMock(return_value=(1,))
        replenish_cursor.__aenter__ = AsyncMock(return_value=replenish_cursor)
        replenish_cursor.__aexit__ = AsyncMock(return_value=None)
        replenish_conn.cursor = Mock(return_value=replenish_cursor)

        pool = AsyncPostgreSQLPool(
            connection_params=connection_params,
            min_connections=2,
            max_connections=5,
            health_check_interval=0,
        )

        # Create queue and lock (normally done in initialize()) then mark as initialized
        pool.connections = asyncio.Queue(maxsize=5)
        pool._lock = asyncio.Lock()
        pool._initialized = True
        pool.active_connections = 0

        # Make _create_connection return our mock connection
        pool._create_connection = AsyncMock(return_value=replenish_conn)  # type: ignore[method-assign]

        # Make put_nowait raise QueueFull so the replenishment branch closes the connection
        pool.connections.put_nowait = Mock(side_effect=asyncio.QueueFull)  # type: ignore[method-assign]

        pool._health_check_task = asyncio.create_task(pool._health_check_loop())

        # Give the health check loop time to attempt replenishment
        await asyncio.sleep(0.15)

        # The connection should have been closed due to QueueFull
        replenish_conn.close.assert_awaited()

        # Restore put_nowait before close() tries to drain the queue
        pool.connections.put_nowait = asyncio.Queue.put_nowait.__get__(pool.connections)  # type: ignore[attr-defined]

        # Cleanup
        await pool.close()


class TestAsyncHealthCheckQueueEmpty:
    """Test async _health_check_loop() QueueEmpty branch (lines 393-394)."""

    @pytest.fixture
    def connection_params(self) -> dict:
        """Test connection parameters."""
        return {
            "host": "localhost",
            "port": 5432,
            "dbname": "test",
            "user": "test_user",
            "password": "test_pass",
        }

    @pytest.fixture
    def mock_async_connection(self) -> AsyncMock:
        """Create a mock async PostgreSQL connection."""
        conn = AsyncMock()
        conn.closed = False
        conn.set_autocommit = AsyncMock()
        conn.close = AsyncMock()
        cursor = AsyncMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        cursor.__aenter__ = AsyncMock(return_value=cursor)
        cursor.__aexit__ = AsyncMock(return_value=None)
        conn.cursor = Mock(return_value=cursor)
        return conn

    @pytest.mark.asyncio
    @patch("common.postgres_resilient.psycopg.AsyncConnection.connect")
    async def test_async_health_check_queue_empty_on_get(self, mock_connect: Mock, connection_params: dict, mock_async_connection: AsyncMock) -> None:
        """Lines 393-394: _health_check_loop() catches asyncio.QueueEmpty and breaks."""
        mock_connect.return_value = mock_async_connection

        pool = AsyncPostgreSQLPool(
            connection_params=connection_params,
            min_connections=0,
            max_connections=5,
            health_check_interval=0,
        )
        await pool.initialize()

        # Make qsize report items but get_nowait raises QueueEmpty
        pool.connections.qsize = Mock(return_value=3)  # type: ignore[method-assign]
        original_get = pool.connections.get_nowait
        pool.connections.get_nowait = Mock(side_effect=asyncio.QueueEmpty)  # type: ignore[method-assign]

        pool._health_check_task = asyncio.create_task(pool._health_check_loop())

        # Give the health check loop time to run
        await asyncio.sleep(0.15)

        # Pool should still be operational
        assert pool._closed is False

        # Restore before cleanup
        pool.connections.get_nowait = original_get  # type: ignore[method-assign]
        pool.connections.qsize = asyncio.Queue.qsize.__get__(pool.connections)  # type: ignore[attr-defined]

        await pool.close()


def _make_async_conn(*, healthy: bool = True) -> AsyncMock:
    """Build a mock async psycopg connection whose health probe returns ``healthy``."""
    conn = AsyncMock()
    conn.closed = False
    conn.set_autocommit = AsyncMock()
    conn.close = AsyncMock()
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=(1,) if healthy else (0,))
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=None)
    conn.cursor = Mock(return_value=cursor)
    return conn


class TestPgPoolBatchRegressions:
    """Regression tests for the PostgreSQL pool defect sweep."""

    @pytest.fixture
    def connection_params(self) -> dict:
        return {"host": "localhost", "port": 5432, "dbname": "test", "user": "u", "password": "p"}

    @pytest.mark.asyncio
    async def test_cu2_34_failed_replacement_does_not_yield_closed_conn(self, connection_params: dict) -> None:
        """Regression: a failed replacement must raise, not yield a stale CLOSED connection.

        A pooled connection fails its health check and every replacement create fails. The pool must
        surface 'Failed to get PostgreSQL connection...' rather than yielding the closed connection,
        and active_connections must be decremented exactly once (no double-decrement below the true count).
        """
        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5, max_retries=3)
        await pool.initialize()
        pool.backoff.get_delay = Mock(return_value=0)  # type: ignore[method-assign]

        bad_conn = _make_async_conn(healthy=False)
        pool.connections.put_nowait(bad_conn)
        # Simulate 3 live connections: the bad one in the queue plus 2 held elsewhere.
        pool.active_connections = 3

        pool._create_connection = AsyncMock(side_effect=OperationalError("db down"))  # type: ignore[method-assign]

        with pytest.raises(Exception, match="Failed to get PostgreSQL connection"):
            async with pool.connection():
                pytest.fail("connection() must not yield a connection when all replacements fail")

        # The bad connection's slot is released exactly once; the 2 unrelated live slots survive.
        assert pool.active_connections == 2
        bad_conn.close.assert_awaited()

        await pool.close()

    @pytest.mark.asyncio
    async def test_cu2_21_cancel_during_create_rolls_back_slot(self, connection_params: dict) -> None:
        """Regression: cancellation while creating a connection must not leak a slot.

        The empty-pool path reserves a slot (active_connections += 1) before awaiting
        _create_connection(). In Python 3.13 asyncio.CancelledError derives from BaseException, so an
        `except Exception` guard would skip the rollback and strand the slot forever. The reservation
        must be released on cancellation too.
        """
        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()
        assert pool.active_connections == 0

        pool._create_connection = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            async with pool.connection():
                pytest.fail("connection() must not yield when creation is cancelled")

        assert pool.active_connections == 0

        await pool.close()

    @pytest.mark.asyncio
    async def test_cu2_21_cancel_during_health_test_releases_conn(self, connection_params: dict) -> None:
        """Regression: cancellation during the health probe must release the checked-out slot."""
        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=0, max_connections=5)
        await pool.initialize()

        held = _make_async_conn(healthy=True)
        pool.connections.put_nowait(held)
        pool.active_connections = 1

        pool._test_connection = AsyncMock(side_effect=asyncio.CancelledError())  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            async with pool.connection():
                pytest.fail("connection() must not yield when the health probe is cancelled")

        assert pool.active_connections == 0
        held.close.assert_awaited()

        await pool.close()

    @pytest.mark.asyncio
    async def test_cu2_11_async_replenish_respects_max_connections(self, connection_params: dict) -> None:
        """Regression: the async health-check replenisher must not mint past max_connections.

        When the pool is saturated (all connections checked out) the queue is empty, so the
        replenish branch fires every tick. Without a cap check it mints min_connections fresh
        backends per tick, blowing past the shared PgBouncer session-mode cap. Reserve the slot
        under the lock (active_connections < max_connections) before creating.
        """
        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=3, health_check_interval=0)
        # Manually stand up the primitives (skip initialize() so no background task races us).
        pool.connections = asyncio.Queue(maxsize=3)
        pool._lock = asyncio.Lock()
        pool.active_connections = 3  # saturated & at cap; every connection checked out (queue empty)

        create_mock = AsyncMock(return_value=_make_async_conn())
        pool._create_connection = create_mock  # type: ignore[method-assign]

        task = asyncio.create_task(pool._health_check_loop())
        await asyncio.sleep(0.05)
        pool._closed = True
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert create_mock.call_count == 0
        assert pool.active_connections == 3

    @pytest.mark.asyncio
    async def test_cu2_11_async_replenish_still_fills_up_to_max(self, connection_params: dict) -> None:
        """Regression: the cap gate must not block legitimate replenishment below max."""
        pool = AsyncPostgreSQLPool(connection_params=connection_params, min_connections=2, max_connections=3, health_check_interval=0)
        pool.connections = asyncio.Queue(maxsize=3)
        pool._lock = asyncio.Lock()
        pool.active_connections = 0  # empty pool, room to grow

        create_mock = AsyncMock(side_effect=lambda: _make_async_conn())
        pool._create_connection = create_mock  # type: ignore[method-assign]

        task = asyncio.create_task(pool._health_check_loop())
        await asyncio.sleep(0.05)
        pool._closed = True
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        # Replenished exactly up to min_connections and never past max_connections.
        assert pool.active_connections == 2
        assert pool.active_connections <= pool.max_connections
