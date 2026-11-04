"""The health probe must not run under the handle lock, and a busy driver is not a dead one.

Unit tests use a gated fake probe. The integration test (``NEO4J_URI`` set, ``integration`` marker) saturates a LIVE Neo4j
Bolt pool: a fake cannot reproduce the driver's real acquisition wait.
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock, Mock

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
    async def test_unanswered_probe_is_a_failure_and_a_hung_peer_is_replaced(self) -> None:
        fresh = object()
        existing = object()

        async def hung(conn: object) -> bool:
            if conn is existing:
                await asyncio.sleep(60)
            return True

        manager: AsyncResilientConnection = AsyncResilientConnection(
            lambda: fresh,
            hung,
            health_check_ttl=0.0,
            probe_timeout=0.05,
            unhealthy_threshold=3,
            close_grace_period=0,
            max_retries=1,
        )
        manager._connection = existing
        manager._last_healthy_at = time.monotonic() - 1.0

        started = time.perf_counter()
        assert await manager.get_connection() is existing
        assert await manager.get_connection() is existing
        assert manager._failed_probes == 2
        assert await manager.get_connection() is fresh  # third consecutive unanswered probe: replaced
        assert time.perf_counter() - started < 2.0  # bounded: about threshold x probe_timeout
        await manager.close()

    @pytest.mark.asyncio
    async def test_saturated_connection_is_kept_when_the_wrapper_explains_the_timeout(self) -> None:
        async def never_answers(_conn: object) -> bool:
            await asyncio.sleep(60)
            return True

        class Saturated(AsyncResilientConnection):  # type: ignore[type-arg]
            def _probe_timeout_is_busy(self, connection: object) -> bool:  # noqa: ARG002
                return True

        manager = Saturated(lambda: object(), never_answers, health_check_ttl=0.0, probe_timeout=0.05, unhealthy_threshold=2)
        existing = await _seed(manager)
        for _ in range(6):  # far more rounds than the threshold
            assert await manager.get_connection() is existing
        assert manager._failed_probes == 0
        await manager.close()

    @pytest.mark.asyncio
    async def test_handle_replaced_during_probe_is_never_returned(self) -> None:
        gate = asyncio.Event()

        async def probe(_conn: object) -> bool:
            await gate.wait()
            return True

        manager = _manager(probe, probe_timeout=30.0)
        old = await _seed(manager)
        caller = asyncio.create_task(manager.get_connection())
        await asyncio.sleep(0.05)
        replacement = object()
        manager._connection = replacement  # reconnect published a new handle while the probe was in flight
        gate.set()
        assert await caller is replacement
        await manager.close()
        assert old is not replacement

    @pytest.mark.asyncio
    async def test_close_during_probe_cancels_it_and_hands_out_no_old_handle(self) -> None:
        async def probe(_conn: object) -> bool:
            await asyncio.sleep(60)
            return True

        fresh = object()
        manager: AsyncResilientConnection = AsyncResilientConnection(lambda: fresh, lambda _c: True, health_check_ttl=0.01, probe_timeout=30.0)
        old = await _seed(manager)
        manager.connection_test = probe  # type: ignore[assignment]
        caller = asyncio.create_task(manager.get_connection())
        await asyncio.sleep(0.05)
        probe_task = manager._probe_task
        assert probe_task is not None
        manager.connection_test = lambda _c: True  # type: ignore[assignment]
        await manager.close()
        got = await asyncio.wait_for(caller, timeout=2.0)
        assert got is not old
        assert probe_task.done()
        assert manager._probe_task is None
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() if "_probe_verdict" in repr(t)]

    @pytest.mark.asyncio
    async def test_closed_connection_is_never_reported_healthy_inside_the_ttl(self) -> None:
        class Conn:
            closed = False

        fresh = Conn()
        manager: AsyncResilientConnection = AsyncResilientConnection(lambda: fresh, lambda c: not c.closed, health_check_ttl=60.0)
        old = Conn()
        manager._connection = old
        manager._last_healthy_at = time.monotonic()
        old.closed = True
        assert await manager.get_connection() is fresh
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


class TestBorrowerErrorsAndCloseOrdering:
    @pytest.mark.asyncio
    async def test_borrower_connection_error_drops_the_healthy_verdict(self) -> None:
        from neo4j.exceptions import ServiceUnavailable

        driver = AsyncResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"))
        raw = Mock()
        raw.session = Mock(side_effect=ServiceUnavailable("gone"))
        driver._connection = raw
        driver._last_healthy_at = time.monotonic()
        with pytest.raises(ServiceUnavailable):
            async with driver.session():
                pass
        assert driver._last_healthy_at == 0.0
        assert driver._open_sessions == 0

    def test_sync_borrower_connection_error_drops_the_healthy_verdict(self) -> None:
        from neo4j.exceptions import SessionExpired

        from common.neo4j_resilient import ResilientNeo4jDriver

        driver = ResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"))
        raw = Mock()
        raw.session = Mock(side_effect=SessionExpired("expired"))
        driver._connection = raw
        driver._last_healthy_at = time.monotonic()
        with pytest.raises(SessionExpired):
            driver.session()
        assert driver._last_healthy_at == 0.0

    @pytest.mark.asyncio
    async def test_probe_is_awaited_to_completion_before_the_connection_is_closed(self) -> None:
        events: list[str] = []

        class Conn:
            async def close(self) -> None:
                events.append("closed")

        old, fresh = Conn(), Conn()

        async def probe(conn: object) -> bool:
            if conn is fresh:
                return True
            try:
                await asyncio.sleep(60)
            finally:
                events.append("probe-finished")
            return True

        manager: AsyncResilientConnection = AsyncResilientConnection(lambda: fresh, probe, health_check_ttl=0.01, probe_timeout=30.0)
        manager._connection = old
        manager._last_healthy_at = time.monotonic() - 1.0
        caller = asyncio.create_task(manager.get_connection())
        await asyncio.sleep(0.05)
        await manager.close()
        assert events == ["probe-finished", "closed"]  # probe finished before the old connection was closed
        assert await asyncio.wait_for(caller, timeout=2.0) is fresh  # the waiter was not itself cancelled: it reconnects
        await manager.close()

    @pytest.mark.asyncio
    async def test_cancelling_a_waiter_does_not_cancel_the_shared_probe(self) -> None:
        gate = asyncio.Event()

        async def probe(_conn: object) -> bool:
            await gate.wait()
            return True

        manager = _manager(probe, probe_timeout=30.0)
        existing = await _seed(manager)
        waiter = asyncio.create_task(manager.get_connection())
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert manager._probe_task is not None
        assert not manager._probe_task.cancelled()
        gate.set()
        assert await manager.get_connection() is existing
        await manager.close()


class TestReviewFollowUps:
    @pytest.mark.asyncio
    async def test_borrower_errors_do_not_reset_the_consecutive_failure_count(self) -> None:
        fresh = object()
        existing = object()
        manager: AsyncResilientConnection = AsyncResilientConnection(
            lambda: fresh, lambda c: c is fresh, health_check_ttl=0.0, unhealthy_threshold=3, close_grace_period=0, max_retries=1
        )
        manager._connection = existing
        manager._last_healthy_at = time.monotonic() - 1.0
        got = []
        for _ in range(3):
            got.append(await manager.get_connection())  # failing probe each time
            manager.note_borrower_error()  # a borrower also reports ServiceUnavailable
        assert got[:2] == [existing, existing]
        assert got[2] is fresh  # replaced on the third failure: borrower errors did not reset the count
        await manager.close()

    def test_neo4j_saturation_needs_fresh_activity_and_a_sane_pool_size(self) -> None:
        driver = AsyncResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"), max_connection_pool_size=2)
        driver._open_sessions = 2
        assert driver._probe_timeout_is_busy(object()) is True
        driver._last_session_activity_at = time.monotonic() - driver.busy_progress_window - 1
        assert driver._probe_timeout_is_busy(object()) is False  # saturated but frozen: a hung peer, not busy work
        driver._last_session_activity_at = time.monotonic()
        driver._open_sessions = 1
        assert driver._probe_timeout_is_busy(object()) is False
        driver.driver_kwargs["max_connection_pool_size"] = -1
        driver._open_sessions = 5
        assert driver._probe_timeout_is_busy(object()) is False  # a nonsensical pool size is never "saturated"

    def test_open_session_counter_is_per_driver(self) -> None:
        a = AsyncResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"), max_connection_pool_size=1)
        b = AsyncResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"), max_connection_pool_size=1)
        a._open_sessions = 1
        assert b._open_sessions == 0
        assert b._probe_timeout_is_busy(object()) is False

    def test_postgres_busy_only_while_a_statement_is_executing(self) -> None:
        import psycopg

        from common.postgres_resilient import AsyncResilientPostgreSQL

        status = psycopg.pq.TransactionStatus
        pg = AsyncResilientPostgreSQL({"host": "localhost"})  # type: ignore[arg-type]
        for state, busy in ((status.ACTIVE, True), (status.IDLE, False), (status.INTRANS, False)):
            assert pg._probe_timeout_is_busy(Mock(info=Mock(transaction_status=state))) is busy


class TestSyncDriverProbeTtl:
    def test_default_is_opt_in(self) -> None:
        assert ResilientConnection(lambda: object(), lambda _c: True).health_check_ttl == 0

    def test_rabbitmq_is_probed_on_every_call(self) -> None:
        from common.rabbitmq_resilient import ResilientRabbitMQConnection

        assert ResilientRabbitMQConnection("amqp://guest:guest@localhost:5672/").health_check_ttl == 0

    def test_neo4j_opts_in(self) -> None:
        from common.neo4j_resilient import ResilientNeo4jDriver

        assert ResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x")).health_check_ttl > 0

    def test_closed_connection_is_not_trusted_inside_the_ttl(self) -> None:
        class Conn:
            is_open = True

            def close(self) -> None:
                self.is_open = False

        conns = [Conn(), Conn()]
        manager: ResilientConnection = ResilientConnection(lambda: conns.pop(0), lambda c: c.is_open, health_check_ttl=60.0)
        first = manager.get_connection()
        first.close()  # peer/driver closed it inside the TTL
        assert manager.get_connection() is not first

    def test_close_overrides_reset_the_healthy_verdict(self) -> None:
        from unittest.mock import patch as _patch

        from common.neo4j_resilient import ResilientNeo4jDriver
        from common.rabbitmq_resilient import ResilientRabbitMQConnection

        with _patch("common.rabbitmq_resilient.BlockingConnection"):
            rabbit = ResilientRabbitMQConnection("amqp://guest:guest@localhost:5672/")
            rabbit._connection = Mock(is_open=False)
            rabbit._last_healthy_at = time.monotonic()
            rabbit.close()
            assert rabbit._last_healthy_at == 0.0
            assert rabbit._connection is None  # even an already-closed handle is cleared
        neo = ResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"))
        neo._connection = Mock()
        neo._last_healthy_at = time.monotonic()
        neo.close()
        assert neo._last_healthy_at == 0.0

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
_CONTAINER = os.environ.get("NEO4J_CONTAINER", "")


def _live_driver(**kwargs: object) -> AsyncResilientNeo4jDriver:
    driver = AsyncResilientNeo4jDriver(
        _URI,
        (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
        max_connection_pool_size=2,
        connection_acquisition_timeout=8.0,
        **kwargs,
    )
    driver.health_check_ttl = 0.2
    driver.probe_timeout = 1.0
    return driver


@pytest.mark.integration
@pytest.mark.skipif(not _URI, reason="NEO4J_URI not set")
@pytest.mark.asyncio
async def test_saturated_live_neo4j_is_kept_and_does_not_stall_callers() -> None:
    driver = _live_driver()
    raw = await driver.get_connection()
    release = asyncio.Event()

    async def hold() -> None:
        async with driver.session(database="neo4j") as session:
            await session.run("RETURN 1")
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(2)]  # fills the 2-connection Bolt pool
    await asyncio.sleep(0.5)

    async def timed() -> float:
        started = time.perf_counter()
        assert await driver.get_connection() is raw
        return time.perf_counter() - started

    for _ in range(driver.unhealthy_threshold + 2):  # more probe rounds than would tear a hung peer down
        await asyncio.sleep(0.3)
        assert max(await asyncio.gather(*[timed() for _ in range(10)])) < 4.0
    assert driver._connection is raw  # saturated, not torn down
    release.set()
    await asyncio.gather(*holders)
    await driver.close()


@pytest.mark.integration
@pytest.mark.skipif(not (_URI and _CONTAINER), reason="NEO4J_URI and NEO4J_CONTAINER not set")
@pytest.mark.asyncio
async def test_hung_live_neo4j_is_replaced_within_a_bound() -> None:
    import subprocess

    driver = _live_driver()
    driver._reconnect_failure_is_fresh = lambda: False  # type: ignore[method-assign]
    driver.max_retries = 1
    raw = await driver.get_connection()
    subprocess.run(["docker", "pause", _CONTAINER], check=True, capture_output=True)  # noqa: S603, S607
    try:
        await asyncio.sleep(0.3)
        started = time.perf_counter()
        outcomes = []
        for _ in range(driver.unhealthy_threshold + 1):
            try:
                outcomes.append(await driver.get_connection() is raw)
            except Exception:  # the replacement cannot connect to a frozen server either
                outcomes.append("raised")
                break
            await asyncio.sleep(0.25)
        elapsed = time.perf_counter() - started
        assert "raised" in outcomes or outcomes[-1] is False  # the hung handle stopped being handed out
        assert driver._connection is not raw
        assert elapsed < driver.unhealthy_threshold * (driver.probe_timeout + 1.0) + 8.0
    finally:
        subprocess.run(["docker", "unpause", _CONTAINER], check=True, capture_output=True)  # noqa: S603, S607
    await asyncio.sleep(1.0)
    driver._last_failure_at = None
    healthy = await driver.get_connection()
    assert healthy is not raw  # recovered onto a fresh driver once the peer answers again
    await driver.close()


_PG_HOST = os.environ.get("POSTGRES_HOST", "")


@pytest.mark.integration
@pytest.mark.skipif(not _PG_HOST, reason="POSTGRES_HOST not set")
@pytest.mark.asyncio
async def test_live_postgres_connection_running_a_long_statement_is_not_replaced() -> None:
    from common.postgres_resilient import AsyncResilientPostgreSQL

    params = {
        "host": _PG_HOST,
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "user": os.environ.get("POSTGRES_USER", "u"),
        "password": os.environ.get("POSTGRES_PASSWORD", "pw"),
        "dbname": os.environ.get("POSTGRES_DB", "d"),
    }
    pg = AsyncResilientPostgreSQL(params)
    pg.health_check_ttl = 0.1
    pg.probe_timeout = 0.5
    conn = await pg.get_connection()

    async def long_statement() -> None:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_sleep(6)")

    work = asyncio.create_task(long_statement())
    await asyncio.sleep(0.4)
    for _ in range(pg.unhealthy_threshold + 2):  # more timed-out probes than would replace a hung peer
        await asyncio.sleep(0.2)
        assert await pg.get_connection() is conn
    assert pg._failed_probes == 0
    await work  # the in-flight statement ran to completion on the original connection
    await pg.close()


class TestNeo4jProgressEvidence:
    """Only a session that completed counts as progress; failed ones must not extend the busy window."""

    @pytest.mark.asyncio
    async def test_failed_sessions_do_not_refresh_progress(self) -> None:
        from neo4j.exceptions import ConnectionAcquisitionTimeoutError, ServiceUnavailable, SessionExpired

        driver = AsyncResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"), max_connection_pool_size=2)
        stale = time.monotonic() - 1000.0
        driver._connection = Mock()
        driver._last_healthy_at = time.monotonic()
        driver.health_check_ttl = 3600.0
        for exc in (ServiceUnavailable("down"), SessionExpired("gone"), ConnectionAcquisitionTimeoutError("pool")):
            driver._last_session_activity_at = stale
            driver._connection.session = Mock(side_effect=exc)
            with pytest.raises(type(exc)):
                async with driver.session(database="neo4j"):
                    pytest.fail("session body must not run")  # pragma: no cover
            assert driver._last_session_activity_at == stale, type(exc).__name__
            assert driver._open_sessions == 0

    @pytest.mark.asyncio
    async def test_completed_session_refreshes_progress(self) -> None:
        driver = AsyncResilientNeo4jDriver("bolt://localhost:7687", ("neo4j", "x"), max_connection_pool_size=2)
        fake_session = AsyncMock()
        fake = Mock()
        fake.session = Mock(return_value=fake_session)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)
        driver._connection = fake
        driver._last_healthy_at = time.monotonic()
        driver.health_check_ttl = 3600.0
        driver._last_session_activity_at = time.monotonic() - 1000.0
        async with driver.session(database="neo4j"):
            assert driver._open_sessions == 1
        assert time.monotonic() - driver._last_session_activity_at < 5.0


@pytest.mark.integration
@pytest.mark.skipif(not _URI, reason="NEO4J_URI not set")
@pytest.mark.asyncio
async def test_live_neo4j_failed_session_is_not_progress_but_completed_one_is() -> None:
    from neo4j.exceptions import ClientError

    driver = _live_driver()
    await driver.get_connection()
    driver._last_session_activity_at = time.monotonic() - 1000.0
    stale = driver._last_session_activity_at
    with pytest.raises(ClientError):  # the server answers a malformed query with an error
        async with driver.session(database="neo4j") as session:
            await session.run("THIS IS NOT CYPHER")
    assert driver._last_session_activity_at == stale
    with pytest.raises(ValueError, match="boom"):  # a borrower failing for its own reasons is not progress either
        async with driver.session(database="neo4j"):
            raise ValueError("boom")
    assert driver._last_session_activity_at == stale
    async with driver.session(database="neo4j") as session:
        result = await session.run("RETURN 1 AS n")
        assert (await result.single())["n"] == 1
    assert time.monotonic() - driver._last_session_activity_at < 5.0
    await driver.close()
