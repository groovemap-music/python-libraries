"""Deployment and request context contracts for structured logging."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from contextlib import redirect_stdout
from threading import Thread
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from common.config import setup_logging
from common.health_server import HealthServer


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def restore_logging_state() -> Iterator[None]:
    """Keep the process-global logging configuration isolated to this module."""
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    old_structlog_config = structlog.get_config().copy()
    old_context = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    yield
    for handler in root.handlers:
        if handler not in old_handlers:
            handler.close()
    root.handlers = old_handlers
    root.setLevel(old_level)
    structlog.configure(**old_structlog_config)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**old_context)


def configure_to_buffer(service: str = "catalog") -> io.StringIO:
    """Configure the real console formatter against an inspectable stream."""
    output = io.StringIO()
    with redirect_stdout(output):
        setup_logging(service)
    return output


def records(output: io.StringIO) -> list[dict[str, Any]]:
    """Decode all JSON records written to a configured stream."""
    return [json.loads(line) for line in output.getvalue().splitlines()]


def event_record(output: io.StringIO, event: str) -> dict[str, Any]:
    """Return the single record matching an event string."""
    return next(record for record in records(output) if record["event"] == event)


def test_deployment_context_is_authoritative_in_caller_and_fresh_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both logging APIs retain process identity without copying request context."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    output = configure_to_buffer("explore")
    structlog.contextvars.bind_contextvars(correlation_id="caller-only")

    logging.getLogger("stdlib.caller").info("caller", extra={"environment": "spoofed", "service": "spoofed"})
    structlog.get_logger("structlog.caller").info("structured", environment="spoofed", service="spoofed")

    def emit_from_fresh_thread() -> None:
        logging.getLogger("stdlib.thread").info("background")

    thread = Thread(target=emit_from_fresh_thread)
    thread.start()
    thread.join()

    caller = event_record(output, "caller")
    structured = event_record(output, "structured")
    background = event_record(output, "background")
    for record in (caller, structured, background):
        assert record["service"] == "explore"
        assert record["environment"] == "production"
    assert caller["correlation_id"] == "caller-only"
    assert structured["correlation_id"] == "caller-only"
    assert "correlation_id" not in background


def test_unset_environment_defaults_in_console_and_rotating_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both real sinks apply the documented development default in a thread."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    log_file = tmp_path / "runtime.log"
    output = io.StringIO()
    with redirect_stdout(output):
        setup_logging("insights", log_file=log_file)

    thread = Thread(target=logging.getLogger("sink.thread").info, args=("sink-event",))
    thread.start()
    thread.join()
    for handler in logging.getLogger().handlers:
        handler.flush()

    console_record = event_record(output, "sink-event")
    file_record = next(json.loads(line) for line in log_file.read_text().splitlines() if json.loads(line)["event"] == "sink-event")
    assert console_record["service"] == file_record["service"] == "insights"
    assert console_record["environment"] == file_record["environment"] == "development"


def test_health_server_background_writer_has_deployment_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the actual HealthServer startup record from its background thread."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    output = configure_to_buffer("explore")
    structlog.contextvars.bind_contextvars(correlation_id="request-must-not-leak")
    server = HealthServer(0, lambda: {"status": "healthy"})
    try:
        server.start_background()
        assert server.thread is not None
        server.thread.join(timeout=0.05)
    finally:
        server.stop()

    record = next(item for item in records(output) if "Health server listening" in item["event"])
    assert record["service"] == "explore"
    assert record["environment"] == "production"
    assert "correlation_id" not in record


def test_error_and_escaped_payload_keep_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployment injection preserves JSON escaping, errors, and standard metadata."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    output = configure_to_buffer("catalog")
    try:
        raise ValueError('bad "value"\nnext line')
    except ValueError:
        logging.getLogger("stdlib.error").exception('failed "check"\nnext line')

    record = event_record(output, 'failed "check"\nnext line')
    assert record["service"] == "catalog"
    assert record["environment"] == "production"
    assert record["logger"] == "stdlib.error"
    assert record["level"] == "error"
    assert isinstance(record["exception"], str)
    assert 'ValueError: bad "value"\nnext line' in record["exception"]
    assert isinstance(record["timestamp"], str)
    assert isinstance(record["lineno"], int)


def test_async_correlation_context_remains_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process identity is stable while concurrent task correlation stays independent."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    output = configure_to_buffer("catalog")

    async def emit(correlation_id: str) -> None:
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        await asyncio.sleep(0)
        structlog.get_logger("async.worker").info(f"task-{correlation_id}")

    async def run() -> None:
        await asyncio.gather(emit("one"), emit("two"))

    asyncio.run(run())

    one = event_record(output, "task-one")
    two = event_record(output, "task-two")
    assert one["correlation_id"] == "one"
    assert two["correlation_id"] == "two"
    assert one["service"] == two["service"] == "catalog"
    assert one["environment"] == two["environment"] == "production"
    assert "correlation_id" not in structlog.contextvars.get_contextvars()
