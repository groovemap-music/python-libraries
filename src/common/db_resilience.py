"""Database resilience utilities for handling connection failures and recovery."""

import asyncio
import contextlib
import enum
import inspect
import logging
import random
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from threading import Lock
from typing import TYPE_CHECKING, Any, TypeVar, cast

from common import runtime_metrics


if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

T = TypeVar("T")


class DatabaseUnavailableError(Exception):
    """The database could not be reached — a TRANSIENT, infrastructure-level fault.

    Raised by the resilience layer itself, never by a query. Callers use it to
    tell "the database is down" apart from "this record is bad": the resilience
    wrappers used to raise a bare ``Exception`` here, so consumers classifying on
    exception type saw an outage as a deterministic, poison payload and
    dead-lettered perfectly valid records.
    """


class ConnectionEstablishmentError(DatabaseUnavailableError):
    """Every attempt to establish (or health-check) a connection failed."""


class CircuitOpenError(DatabaseUnavailableError):
    """The circuit breaker is open, so the call was rejected without an attempt."""


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failures exceeded threshold, rejecting calls
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    failure_threshold: int = 5  # Number of failures before opening
    recovery_timeout: int = 60  # Seconds before trying half-open
    expected_exception: type[Exception] | tuple[type[Exception], ...] = Exception
    name: str = "CircuitBreaker"
    # Closed-set label for the backing system, reported as the `system` attribute of the
    # circuit-breaker state gauge. `name` is a human-facing log prefix and may be anything;
    # this must stay low-cardinality. Defaults to the lowercased name.
    system: str | None = None


class CircuitBreaker:
    """Circuit breaker pattern implementation for database connections."""

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.failure_count = 0
        self.last_failure_time: datetime | None = None
        self.state = CircuitState.CLOSED
        self._lock = Lock()
        self._async_lock: asyncio.Lock | None = None
        self.system = config.system or config.name.lower()
        runtime_metrics.register_circuit_breaker(self)

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute function with circuit breaker protection."""
        is_trial = False
        with self._lock:
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    logger.info(f"🔄 {self.config.name}: Circuit breaker entering HALF_OPEN state")
                    is_trial = True
                else:
                    raise CircuitOpenError(f"{self.config.name}: Circuit breaker is OPEN")
            elif self.state == CircuitState.HALF_OPEN:
                is_trial = True

        if is_trial:
            # Execute trial call outside the lock, then update state under lock
            try:
                result = func(*args, **kwargs)
                with self._lock:
                    self.failure_count = 0
                    if self.state != CircuitState.CLOSED:
                        logger.info(f"✅ {self.config.name}: Circuit breaker reset to CLOSED")
                        self.state = CircuitState.CLOSED
                return result
            except self.config.expected_exception:
                with self._lock:
                    self.failure_count += 1
                    self.last_failure_time = datetime.now(UTC)
                    if self.failure_count >= self.config.failure_threshold and self.state != CircuitState.OPEN:
                        logger.error(f"❌ {self.config.name}: Circuit breaker OPEN after {self.failure_count} failures")
                        self.state = CircuitState.OPEN
                raise

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.config.expected_exception:
            self._on_failure()
            raise

    def _get_async_lock(self) -> asyncio.Lock:
        """Return the async lock, creating it lazily in the running event loop."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    async def call_async(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute async function with circuit breaker protection."""
        is_trial = False
        async with self._get_async_lock():
            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self.state = CircuitState.HALF_OPEN
                    logger.info(f"🔄 {self.config.name}: Circuit breaker entering HALF_OPEN state")
                    is_trial = True
                else:
                    raise CircuitOpenError(f"{self.config.name}: Circuit breaker is OPEN")
            elif self.state == CircuitState.HALF_OPEN:
                is_trial = True

        if is_trial:
            # Execute trial call outside the lock, then update state under lock
            try:
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                async with self._get_async_lock():
                    self.failure_count = 0
                    if self.state != CircuitState.CLOSED:
                        logger.info(f"✅ {self.config.name}: Circuit breaker reset to CLOSED")
                        self.state = CircuitState.CLOSED
                return result
            except self.config.expected_exception:
                async with self._get_async_lock():
                    self.failure_count += 1
                    self.last_failure_time = datetime.now(UTC)
                    if self.failure_count >= self.config.failure_threshold and self.state != CircuitState.OPEN:
                        logger.error(f"❌ {self.config.name}: Circuit breaker OPEN after {self.failure_count} failures")
                        self.state = CircuitState.OPEN
                raise

        try:
            if inspect.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            await self._on_success_async()
            return result
        except self.config.expected_exception:
            await self._on_failure_async()
            raise

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to try reset."""
        return self.last_failure_time is not None and datetime.now(UTC) - self.last_failure_time > timedelta(seconds=self.config.recovery_timeout)

    def _on_success(self) -> None:
        """Handle successful call."""
        with self._lock:
            self.failure_count = 0
            if self.state != CircuitState.CLOSED:
                logger.info(f"✅ {self.config.name}: Circuit breaker reset to CLOSED")
                self.state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        """Handle failed call."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = datetime.now(UTC)

            if self.failure_count >= self.config.failure_threshold and self.state != CircuitState.OPEN:
                logger.error(f"❌ {self.config.name}: Circuit breaker OPEN after {self.failure_count} failures")
                self.state = CircuitState.OPEN

    async def _on_success_async(self) -> None:
        """Handle successful async call."""
        async with self._get_async_lock():
            self.failure_count = 0
            if self.state != CircuitState.CLOSED:
                logger.info(f"✅ {self.config.name}: Circuit breaker reset to CLOSED")
                self.state = CircuitState.CLOSED

    async def _on_failure_async(self) -> None:
        """Handle failed async call."""
        async with self._get_async_lock():
            self.failure_count += 1
            self.last_failure_time = datetime.now(UTC)

            if self.failure_count >= self.config.failure_threshold and self.state != CircuitState.OPEN:
                logger.error(f"❌ {self.config.name}: Circuit breaker OPEN after {self.failure_count} failures")
                self.state = CircuitState.OPEN


class ExponentialBackoff:
    """Exponential backoff retry strategy."""

    def __init__(self, initial_delay: float = 1.0, max_delay: float = 60.0, exponential_base: float = 2.0, jitter: bool = True):
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter

    def get_delay(self, retry_count: int) -> float:
        """Calculate delay for given retry count."""
        delay = min(self.initial_delay * (self.exponential_base**retry_count), self.max_delay)

        if self.jitter:
            # Add random jitter (0-25% of delay)
            jitter_value = random.uniform(0, 0.25)  # noqa: S311  # nosec B311
            delay = delay * (1 + jitter_value)

        return delay


class _ProbeVerdict(enum.Enum):
    HEALTHY = "healthy"
    DEAD = "dead"
    STALE = "stale"  # the handle was replaced, detached or closed while probing: the verdict is about nothing current


def _reports_closed(connection: Any) -> bool:
    """True when the connection object itself says it is closed (pika ``is_open``, psycopg/aio ``closed``)."""
    return (
        getattr(connection, "closed", None) is True or getattr(connection, "is_closed", None) is True or getattr(connection, "is_open", None) is False
    )


class ResilientConnection[T]:
    """Base class for resilient database connections with circuit breaker and retry logic."""

    def __init__(
        self,
        connection_factory: Callable[[], T],
        connection_test: Callable[[T], bool],
        circuit_breaker: CircuitBreaker | None = None,
        backoff: ExponentialBackoff | None = None,
        max_retries: int = 3,
        name: str = "Connection",
        system: str | None = None,
        health_check_ttl: float = 0.0,
    ):
        self.connection_factory = connection_factory
        self.connection_test = connection_test
        self.circuit_breaker = circuit_breaker or CircuitBreaker(CircuitBreakerConfig(name=name))
        self.backoff = backoff or ExponentialBackoff()
        self.max_retries = max_retries
        self.name = name
        # Closed-set metric attribute for this wrapper's backing system; falls back to the
        # display name so an unlabelled subclass still reports something stable.
        self.system = system or name.lower()
        self.health_check_ttl = health_check_ttl
        self._last_healthy_at: float = 0.0
        self._connection: T | None = None
        self._lock = Lock()

    def _close_connection(self, connection: Any) -> None:
        """Best-effort close of a connection, matching close()'s dispatch."""
        if connection is None:
            return
        try:
            if hasattr(connection, "close"):
                connection.close()
        except Exception as e:
            logger.warning(f"⚠️ {self.name}: Error closing connection: {e}")

    def _create_tested_connection(self) -> T:
        """Create a healthy connection, closing a rejected candidate."""
        connection = self.connection_factory()
        if self.connection_test(connection):
            return connection
        self._close_connection(connection)
        raise ConnectionEstablishmentError("Connection test failed")

    def get_connection(self) -> T:
        """Get a healthy connection, creating or reconnecting if needed."""
        with self._lock:
            if self._connection:
                # A passing probe is trusted for health_check_ttl: probing on every call put one
                # round trip (and, on a saturated driver, a long wait under this lock) in front of each session.
                if (
                    self.health_check_ttl > 0
                    and self._last_healthy_at
                    and time.monotonic() - self._last_healthy_at < self.health_check_ttl
                    and not _reports_closed(self._connection)
                ):
                    return self._connection
                if self._test_connection(self._connection):
                    self._last_healthy_at = time.monotonic()
                    return self._connection

            if self._connection is not None:
                self._close_connection(self._connection)
                self._connection = None
            self._last_healthy_at = 0.0

            retry_count = 0
            last_error = None

            while retry_count < self.max_retries:
                try:
                    logger.info(f"🔄 {self.name}: Creating new connection (attempt {retry_count + 1}/{self.max_retries})")

                    self._connection = self.circuit_breaker.call(self._create_tested_connection)
                    self._last_healthy_at = time.monotonic()
                    runtime_metrics.record_reconnect(self.system)
                    logger.info(f"✅ {self.name}: Connection established successfully")
                    return self._connection

                except Exception as e:
                    last_error = e
                    retry_count += 1

                    if retry_count < self.max_retries:
                        delay = self.backoff.get_delay(retry_count - 1)
                        logger.warning(f"⚠️ {self.name}: Connection attempt {retry_count} failed: {e}. Retrying in {delay:.1f} seconds...")
                        time.sleep(delay)
                    else:
                        logger.error(f"❌ {self.name}: All connection attempts failed")

            raise ConnectionEstablishmentError(f"{self.name}: Failed to establish connection after {self.max_retries} attempts") from last_error

    def _test_connection(self, connection: T) -> bool:
        """Test if connection is healthy."""
        try:
            return self.connection_test(connection)
        except Exception as e:
            logger.warning(f"⚠️ {self.name}: Connection test failed: {e}")
            return False

    def _reset_health_state(self) -> None:
        """Forget any healthy verdict. Every close()/clear path, including subclass overrides, must call this."""
        self._last_healthy_at = 0.0

    def note_borrower_error(self) -> None:
        """A borrower hit a connection-level error: drop the healthy verdict so the next call probes."""
        self._reset_health_state()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            if self._connection is not None:
                self._close_connection(self._connection)
                self._connection = None
            self._reset_health_state()


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Retrieve a finished task's exception so asyncio does not warn about it."""
    if not task.cancelled():
        task.exception()


class AsyncResilientConnection[T]:
    """Async version of resilient database connection."""

    def __init__(
        self,
        connection_factory: Callable[[], T] | Callable[[], Any],
        connection_test: Callable[[T], bool] | Callable[[T], Any],
        circuit_breaker: CircuitBreaker | None = None,
        backoff: ExponentialBackoff | None = None,
        max_retries: int = 3,
        name: str = "AsyncConnection",
        health_check_ttl: float = 30.0,
        unhealthy_threshold: int = 3,
        close_grace_period: float = 30.0,
        reconnect_cooldown: float = 5.0,
        system: str | None = None,
        probe_timeout: float = 5.0,
    ):
        self.connection_factory = connection_factory
        self.connection_test = connection_test
        self.circuit_breaker = circuit_breaker or CircuitBreaker(CircuitBreakerConfig(name=name))
        self.backoff = backoff or ExponentialBackoff()
        self.max_retries = max_retries
        self.name = name
        self.system = system or name.lower()
        self.health_check_ttl = health_check_ttl
        self.probe_timeout = probe_timeout
        self._probe_task: asyncio.Task[_ProbeVerdict] | None = None
        self.unhealthy_threshold = unhealthy_threshold
        self.close_grace_period = close_grace_period
        self.reconnect_cooldown = reconnect_cooldown
        self._connection: T | None = None
        self._lock: asyncio.Lock | None = None
        self._last_healthy_at: float = 0.0
        self._failed_probes: int = 0
        self._reconnect_task: asyncio.Task[T] | None = None
        self._last_failure_at: float | None = None
        self._last_failure_error: Exception | None = None
        self._draining: list[Any] = []
        self._close_tasks: set[asyncio.Task[None]] = set()

    async def _aclose_connection(self, connection: Any) -> None:
        """Best-effort close of a connection, dispatching aclose()/close() like close()."""
        if connection is None:
            return
        try:
            if hasattr(connection, "aclose"):
                await connection.aclose()
            elif hasattr(connection, "close"):
                if inspect.iscoroutinefunction(connection.close):
                    await connection.close()
                else:
                    connection.close()
        except Exception as e:
            logger.warning(f"⚠️ {self.name}: Error closing connection: {e}")

    def _get_lock(self) -> asyncio.Lock:
        """Return the handle lock, creating it lazily on the running event loop."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _probe_connection(self, connection: T) -> bool:
        """Run the health check, never letting it raise."""
        try:
            return bool(await self._test_connection(connection))
        except Exception as e:
            logger.warning(f"⚠️ {self.name}: Connection test failed: {e}")
            return False

    def _recently_healthy(self, connection: T) -> bool:
        return bool(self._last_healthy_at) and time.monotonic() - self._last_healthy_at < self.health_check_ttl and not _reports_closed(connection)

    def _probe_timeout_is_busy(self, connection: T) -> bool:  # noqa: ARG002
        """Whether an unanswered probe is explained by the connection being saturated with work.

        Default: no, an unanswered probe is an unresponsive peer. A wrapper that can tell a saturated driver
        from a hung one (it knows how many borrowers are in flight) overrides this.
        """
        return False

    async def _probe_verdict(self, connection: T) -> _ProbeVerdict:
        """Probe an existing connection and decide whether it may still be handed out.

        Runs as a single shared task OUTSIDE the handle lock, so a slow probe never blocks other callers.

        * a successful probe is trusted for ``health_check_ttl`` seconds, so borrowing does not pay a health
          round trip per call;
        * a probe unanswered within ``probe_timeout`` is benign ONLY when ``_probe_timeout_is_busy`` explains
          it (every slot in use): that connection is working, and replacing it would abort the in-flight work
          of every borrower. Otherwise it is an unresponsive peer and counts as a failed probe; and
        * ``unhealthy_threshold`` CONSECUTIVE failed probes (timeouts included) are required before the
          connection is declared dead, so a hung peer is replaced after roughly
          ``unhealthy_threshold * probe_timeout`` seconds of callers probing it, never kept indefinitely.
        """
        timed_out = False
        try:
            ok = await asyncio.wait_for(self._probe_connection(connection), timeout=self.probe_timeout)
        except TimeoutError:
            timed_out = True
            ok = False
            if self._probe_timeout_is_busy(connection):
                logger.warning(f"⚠️ {self.name}: Health probe unanswered after {self.probe_timeout:.0f}s but the connection is saturated — keeping it")
                return _ProbeVerdict.HEALTHY if self._connection is connection else _ProbeVerdict.STALE
        if self._connection is not connection:
            return _ProbeVerdict.STALE
        if ok:
            self._failed_probes = 0
            self._last_healthy_at = time.monotonic()
            return _ProbeVerdict.HEALTHY

        self._failed_probes += 1
        if timed_out:
            logger.warning(f"⚠️ {self.name}: Health probe unanswered after {self.probe_timeout:.0f}s and the connection is not saturated")
        if self._failed_probes < self.unhealthy_threshold:
            logger.warning(
                f"⚠️ {self.name}: Health check failed ({self._failed_probes}/{self.unhealthy_threshold}) — "
                "keeping the existing connection; in-flight borrowers must not be aborted on one probe"
            )
            return _ProbeVerdict.HEALTHY

        logger.error(f"❌ {self.name}: Health check failed {self._failed_probes} consecutive times — replacing the connection")
        return _ProbeVerdict.DEAD

    def _schedule_deferred_close(self, connection: Any) -> None:
        """Close a replaced connection only after in-flight borrowers can drain.

        The manager's lock serializes access to the HANDLE, never use of the
        object: a caller keeps using a session borrowed from it long after
        get_connection() returned, and the neo4j driver documents close() as
        NOT concurrency-safe with live sessions. So the replaced connection is
        detached immediately and closed after ``close_grace_period``.
        """
        if connection is None:
            return
        self._draining.append(connection)
        task = asyncio.create_task(self._close_after_grace(connection))
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    def _reset_health_state(self) -> None:
        """Forget any healthy verdict and failure count. Called by every close/detach path."""
        self._failed_probes = 0
        self._last_healthy_at = 0.0

    def _detach_connection(self, connection: T) -> None:
        """Stop handing out an unhealthy connection while existing borrowers drain."""
        self._connection = None
        self._reset_health_state()
        self._schedule_deferred_close(connection)

    def _reconnect_failure_is_fresh(self) -> bool:
        return self._last_failure_at is not None and time.monotonic() - self._last_failure_at < self.reconnect_cooldown

    async def _close_after_grace(self, connection: Any) -> None:
        """Wait out the grace period, then close a detached connection."""
        if self.close_grace_period > 0:
            await asyncio.sleep(self.close_grace_period)
        try:
            await self._aclose_connection(connection)
        finally:
            with contextlib.suppress(ValueError):
                self._draining.remove(connection)

    async def _cancel_probe(self) -> None:
        """Cancel the in-flight probe and wait for it to finish, so it cannot touch a connection being closed.

        Shared by every close() path through ``_drain_deferred_closes``.
        """
        task, self._probe_task = self._probe_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def note_borrower_error(self) -> None:
        """A borrower hit a connection-level error: drop the healthy verdict so the next checkout probes."""
        self._reset_health_state()

    async def _drain_deferred_closes(self) -> None:
        """Close every detached connection immediately (explicit shutdown)."""
        await self._cancel_probe()
        self._reset_health_state()
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None
        self._last_failure_at = None
        self._last_failure_error = None
        for task in list(self._close_tasks):
            task.cancel()
        self._close_tasks.clear()
        draining, self._draining = self._draining, []
        for connection in draining:
            await self._aclose_connection(connection)

    async def get_connection(self) -> T:
        """Get a healthy connection, creating or reconnecting if needed.

        The manager's lock guards ONLY the connection handle. The reconnect
        cycle — up to ``max_retries`` driver-creation attempts, each of which can
        block for the driver's whole acquisition timeout, plus the backoff
        sleeps between them — runs OUTSIDE the lock as a single shared task.
        Before the fix, the first caller held the process
        singleton lock for the entire failed cycle, every other coroutine in the
        process queued behind it, and each waiter then re-ran the full cycle
        itself, so the k-th caller failed only after roughly k cycles. Now
        concurrent callers join the one in-flight reconnect, and callers arriving
        inside ``reconnect_cooldown`` of a failed cycle fail fast instead of
        repeating it.
        """
        lock = self._get_lock()

        async with lock:
            connection = self._connection
            probe: asyncio.Task[_ProbeVerdict] | None = None
            if connection is not None:
                if self._recently_healthy(connection):
                    return connection
                if self._probe_task is not None and not self._probe_task.done():
                    # A probe is already in flight (possibly a driver saturated by busy work). Do not queue
                    # behind it: the connection was healthy at its last verdict.
                    return connection
                probe = self._probe_task = asyncio.create_task(self._probe_verdict(connection))
                probe.add_done_callback(_consume_task_exception)
            else:
                reconnect = self._start_reconnect_locked()

        if probe is not None and connection is not None:
            # Outside the lock: a slow probe delays only this caller, and only up to ``probe_timeout``.
            try:
                verdict = await asyncio.shield(probe)
            except asyncio.CancelledError:
                if not probe.cancelled():
                    raise  # this caller was cancelled, not the probe
                verdict = _ProbeVerdict.STALE  # close() cancelled the probe
            # Re-read the handle under the lock: it may have been replaced, detached or closed while we waited,
            # and the old object must never be handed out on the strength of a verdict about it.
            async with lock:
                current = self._connection
                if current is not None and current is not connection:
                    return current
                if current is connection and verdict is _ProbeVerdict.HEALTHY and not _reports_closed(connection):
                    return connection
                if current is connection:
                    self._detach_connection(connection)
                reconnect = self._start_reconnect_locked()

        # A cancelled waiter must not cancel the reconnect shared by other callers.
        return await asyncio.shield(reconnect)

    def _start_reconnect_locked(self) -> asyncio.Task[T]:
        """Join the in-flight reconnect or start one. Caller holds the handle lock."""
        if self._reconnect_failure_is_fresh():
            raise ConnectionEstablishmentError(
                f"{self.name}: Connection unavailable — a reconnect cycle failed less than {self.reconnect_cooldown:.0f}s ago"
            ) from self._last_failure_error

        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())
            # Consume a failed shared task even when all of its waiters are cancelled.
            self._reconnect_task.add_done_callback(_consume_task_exception)
        return self._reconnect_task

    async def _reconnect(self) -> T:
        """Run the retry/backoff cycle, publishing the result under the lock."""
        retry_count = 0
        last_error: Exception | None = None

        while retry_count < self.max_retries:
            try:
                logger.info(f"🔄 {self.name}: Creating new connection (attempt {retry_count + 1}/{self.max_retries})")

                async def create_connection() -> T:
                    if inspect.iscoroutinefunction(self.connection_factory):
                        conn = await self.connection_factory()
                    else:
                        conn = self.connection_factory()
                    if inspect.iscoroutinefunction(self.connection_test):
                        test_ok = await self.connection_test(conn)
                    else:
                        test_ok = self.connection_test(conn)
                    if not test_ok:
                        # Close the just-built connection before discarding it, so a failed
                        # health test does not leak a fresh pool/socket per attempt.
                        await self._aclose_connection(conn)
                        raise ConnectionEstablishmentError("Connection test failed")
                    return cast("T", conn)

                conn = await self.circuit_breaker.call_async(create_connection)

                async with self._get_lock():
                    self._connection = cast("T", conn)
                    self._last_healthy_at = time.monotonic()
                    self._failed_probes = 0
                    self._last_failure_at = None
                    self._last_failure_error = None
                runtime_metrics.record_reconnect(self.system)
                logger.info(f"✅ {self.name}: Connection established successfully")
                return cast("T", conn)

            except Exception as e:
                last_error = e
                retry_count += 1

                if retry_count < self.max_retries:
                    delay = self.backoff.get_delay(retry_count - 1)
                    logger.warning(f"⚠️ {self.name}: Connection attempt {retry_count} failed: {e}. Retrying in {delay:.1f} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"❌ {self.name}: All connection attempts failed")

        self._last_failure_at = time.monotonic()
        self._last_failure_error = last_error
        raise ConnectionEstablishmentError(f"{self.name}: Failed to establish connection after {self.max_retries} attempts") from last_error

    async def _test_connection(self, connection: T) -> bool:
        """Test if connection is healthy."""
        try:
            if inspect.iscoroutinefunction(self.connection_test):
                result = await self.connection_test(connection)
                return bool(result)
            else:
                return bool(self.connection_test(connection))
        except Exception as e:
            logger.warning(f"⚠️ {self.name}: Connection test failed: {e}")
            return False

    async def close(self) -> None:
        """Close the connection."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            await self._drain_deferred_closes()
            if self._connection is not None:
                await self._aclose_connection(self._connection)
                self._connection = None
            self._last_healthy_at = 0.0
            self._failed_probes = 0


@contextmanager
def resilient_connection[T](connection_manager: ResilientConnection[T]) -> Any:
    """Context manager for resilient connections."""
    conn = connection_manager.get_connection()
    yield conn


@asynccontextmanager
async def async_resilient_connection[T](connection_manager: AsyncResilientConnection[T]) -> Any:
    """Async context manager for resilient connections."""
    conn = await connection_manager.get_connection()
    yield conn
