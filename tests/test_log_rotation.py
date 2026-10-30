"""Tests for bounded application-owned log files."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from common.config import setup_logging
from common.log_rotation import (
    DEFAULT_LOG_FILE_BACKUP_COUNT,
    DEFAULT_LOG_FILE_MAX_BYTES,
    build_rotating_file_handler,
)


if TYPE_CHECKING:
    import pytest


def test_rotates_and_bounds_total_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing past the cap retains only the active file and configured backups."""
    max_bytes = 256
    backup_count = 2
    monkeypatch.setenv("LOG_FILE_MAX_BYTES", str(max_bytes))
    monkeypatch.setenv("LOG_FILE_BACKUP_COUNT", str(backup_count))
    log_file = tmp_path / "service.log"
    handler = build_rotating_file_handler(log_file)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test-bounded-file-logging")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        for _ in range(100):
            logger.info("x" * 100)
        handler.flush()
    finally:
        handler.close()
        logger.handlers.clear()

    log_files = sorted(tmp_path.glob("service.log*"))
    assert log_file.with_suffix(".log.1") in log_files
    assert len(log_files) == backup_count + 1
    assert all(path.stat().st_size <= max_bytes for path in log_files)
    assert sum(path.stat().st_size for path in log_files) <= max_bytes * (backup_count + 1)


def test_invalid_rotation_overrides_remain_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad deployment values cannot accidentally restore unbounded append."""
    monkeypatch.setenv("LOG_FILE_MAX_BYTES", "0")
    monkeypatch.setenv("LOG_FILE_BACKUP_COUNT", "not-an-integer")

    handler = build_rotating_file_handler(tmp_path / "service.log")
    try:
        assert handler.maxBytes == DEFAULT_LOG_FILE_MAX_BYTES
        assert handler.backupCount == DEFAULT_LOG_FILE_BACKUP_COUNT
    finally:
        handler.close()


def test_setup_logging_uses_shared_rotating_handler(tmp_path: Path) -> None:
    """Every service using setup_logging receives the bounded file sink."""
    log_file = tmp_path / "service.log"
    handler = RotatingFileHandler(log_file, maxBytes=123, backupCount=4)

    with (
        patch("common.config.build_rotating_file_handler", return_value=handler) as build_handler,
        patch("common.config.logging.basicConfig") as basic_config,
    ):
        setup_logging("test-service", log_file=log_file)

    build_handler.assert_called_once_with(log_file)
    assert handler in basic_config.call_args.kwargs["handlers"]
    handler.close()


def _file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def test_unwritable_log_file_falls_back_to_console(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """An unusable path keeps console logging and warns exactly once."""
    monkeypatch.delenv("LOG_FILE", raising=False)
    bad = "/nonexistent-root-dir/x.log"

    setup_logging("fallback-test", log_file=Path(bad))
    logging.getLogger("probe").info("console-still-works")

    assert _file_handlers() == []
    out = capsys.readouterr().out
    assert "console-still-works" in out
    warnings_ = [line for line in out.splitlines() if '"level":"warning"' in line and bad in line]
    assert len(warnings_) == 1


def test_log_file_env_overrides_argument(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_FILE wins over the log_file argument."""
    custom = tmp_path / "custom.log"
    arg = tmp_path / "arg.log"
    monkeypatch.setenv("LOG_FILE", str(custom))

    setup_logging("env-test", log_file=arg)
    logging.getLogger("probe").info("hello-custom")
    for handler in _file_handlers():
        handler.flush()

    assert "hello-custom" in custom.read_text()
    assert not arg.exists()


def test_empty_log_file_env_disables_file_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LOG_FILE= disables file logging even when an argument is given."""
    arg = tmp_path / "arg.log"
    monkeypatch.setenv("LOG_FILE", "")

    setup_logging("empty-env-test", log_file=arg)

    assert _file_handlers() == []
    assert not arg.exists()
