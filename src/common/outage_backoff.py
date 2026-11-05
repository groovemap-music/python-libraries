"""Backoff for per-message consumers that requeue on a database outage.

Per-message consumers handle "the database is down" by ``nack(requeue=True)``. With no pause, a
message comes straight back and fails again hundreds of times a second for as long as the outage
lasts: RabbitMQ 4.3.5 and 4.4.0 do not spend ``x-delivery-limit`` on a nack, so the broker never
stops it. This holds the delivery for a growing delay before each nack.

What ends a message that keeps failing is ``common.delivery.DEFAULT_MAX_REQUEUES``, read from the
broker's ``x-acquired-count``. At the 60 s cap here, that budget lasts about 8.3 hours.
"""

import asyncio

import structlog


logger = structlog.get_logger(__name__)

# Delay applied before the FIRST requeue of an outage, in seconds.
DEFAULT_INITIAL_DELAY = 2.0
# Ceiling for the per-redelivery delay, in seconds.
DEFAULT_MAX_DELAY = 60.0


class OutageBackoff:
    """Growing delay applied before requeueing during a backend outage.

    One instance per consumer process. It is deliberately NOT per-message: the
    thing being throttled is the outage, and every in-flight delivery is hitting
    the same dead backend.
    """

    def __init__(
        self,
        name: str,
        initial_delay: float = DEFAULT_INITIAL_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        multiplier: float = 2.0,
    ) -> None:
        self.name = name
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive transient failures since the last success."""
        return self._consecutive_failures

    def reset(self) -> None:
        """Record a success — the backend is answering again."""
        self._consecutive_failures = 0

    def next_delay(self) -> float:
        """Count one transient failure and return the delay to apply."""
        self._consecutive_failures += 1
        return min(
            self.initial_delay * (self.multiplier ** (self._consecutive_failures - 1)),
            self.max_delay,
        )

    async def wait(self) -> float:
        """Count a transient failure and sleep for the resulting delay.

        Call this immediately BEFORE ``message.nack(requeue=True)`` so the redelivery that
        follows costs wall-clock time. A nack spends none of the queue's ``x-delivery-limit``.
        """
        delay = self.next_delay()
        logger.warning(
            "⏳ Backing off before requeue — backend unavailable",
            consumer=self.name,
            delay_seconds=round(delay, 1),
            consecutive_failures=self._consecutive_failures,
        )
        await asyncio.sleep(delay)
        return delay
