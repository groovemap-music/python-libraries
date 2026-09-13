"""Bounded file-handler construction for application-owned logs."""

from __future__ import annotations

from logging.handlers import RotatingFileHandler
from os import getenv
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


DEFAULT_LOG_FILE_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_LOG_FILE_BACKUP_COUNT = 5


def _positive_env_int(name: str, default: int) -> int:
    """Return a positive integer environment value or a safe default."""
    try:
        value = int(getenv(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def build_rotating_file_handler(log_file: Path) -> RotatingFileHandler:
    """Create a size-bounded UTF-8 file handler using deployment overrides.

    ``LOG_FILE_MAX_BYTES`` controls the active file's rollover threshold and
    ``LOG_FILE_BACKUP_COUNT`` controls how many old files are retained. Invalid
    or non-positive values fall back to bounded defaults rather than silently
    disabling rotation.
    """
    return RotatingFileHandler(
        log_file,
        maxBytes=_positive_env_int("LOG_FILE_MAX_BYTES", DEFAULT_LOG_FILE_MAX_BYTES),
        backupCount=_positive_env_int("LOG_FILE_BACKUP_COUNT", DEFAULT_LOG_FILE_BACKUP_COUNT),
        encoding="utf-8",
    )
