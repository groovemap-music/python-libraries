"""Tests for bounded application-owned log files."""

import logging
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING
from unittest.mock import patch

from common.config import setup_logging
from common.log_rotation import (
    DEFAULT_LOG_FILE_BACKUP_COUNT,
    DEFAULT_LOG_FILE_MAX_BYTES,
    build_rotating_file_handler,
)


if TYPE_CHECKING:
    from pathlib import Path

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
