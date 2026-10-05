"""Compatibility contracts for Valkey-first store connection settings."""

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from common import config


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_store_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Neither machine settings nor warning state may leak between cases."""
    for prefix in ("VALKEY", "REDIS"):
        for suffix in ("HOST", "PORT", "PASSWORD", "PASSWORD_FILE", "DB"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    with patch.object(config, "_deprecated_store_settings", set()):
        yield


@pytest.mark.parametrize("scheme", ["valkey", "redis"])
@pytest.mark.parametrize(
    ("settings", "authority"),
    [
        ({}, "localhost:6379"),
        ({"VALKEY_HOST": "store", "VALKEY_PORT": "6380"}, "store:6380"),
        ({"REDIS_HOST": "legacy", "REDIS_PORT": "6381"}, "legacy:6381"),
        ({"VALKEY_HOST": "store", "REDIS_PORT": "6381"}, "store:6381"),
        ({"REDIS_HOST": "legacy", "VALKEY_PORT": "6380"}, "legacy:6380"),
        (
            {"VALKEY_HOST": "store", "VALKEY_PORT": "6380", "REDIS_HOST": "old", "REDIS_PORT": "6390"},
            "store:6380",
        ),
        ({"VALKEY_PASSWORD": "a:/@% b"}, ":a%3A%2F%40%25%20b@localhost:6379"),
        ({"REDIS_PASSWORD": "a:/@% b"}, ":a%3A%2F%40%25%20b@localhost:6379"),
        ({"VALKEY_PASSWORD": "new", "REDIS_PASSWORD": "old"}, ":new@localhost:6379"),
        ({"VALKEY_PASSWORD": "", "REDIS_PASSWORD": "old"}, "localhost:6379"),
        ({"REDIS_PASSWORD": ""}, "localhost:6379"),
        ({"VALKEY_DB": "9", "REDIS_DB": "8"}, "localhost:6379"),
    ],
)
def test_connection_matrix(monkeypatch: pytest.MonkeyPatch, scheme: str, settings: dict[str, str], authority: str) -> None:
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    builder = config._build_valkey_url if scheme == "valkey" else config._build_redis_url
    assert builder() == f"{scheme}://{authority}/0"


@pytest.mark.parametrize("prefix", ["VALKEY", "REDIS"])
@pytest.mark.parametrize("scheme", ["valkey", "redis"])
def test_password_file_wins_and_is_quoted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prefix: str, scheme: str) -> None:
    secret = tmp_path / "password"
    secret.write_text("  file:/@% secret\n")
    monkeypatch.setenv(f"{prefix}_PASSWORD_FILE", str(secret))
    monkeypatch.setenv(f"{prefix}_PASSWORD", "plain")
    builder = config._build_valkey_url if scheme == "valkey" else config._build_redis_url
    assert builder() == f"{scheme}://:file%3A%2F%40%25%20secret@localhost:6379/0"


@pytest.mark.parametrize("value", ["", "new"])
def test_valkey_plain_password_overrides_legacy_file(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("VALKEY_PASSWORD", value)
    monkeypatch.setenv("REDIS_PASSWORD_FILE", "/missing/legacy/secret")
    expected = f":{value}@" if value else ""
    assert config._build_valkey_url() == f"valkey://{expected}localhost:6379/0"


def test_valkey_file_overrides_legacy_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("new")
    monkeypatch.setenv("VALKEY_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("REDIS_PASSWORD_FILE", "/missing/legacy/secret")
    assert config._build_valkey_url() == "valkey://:new@localhost:6379/0"


@pytest.mark.parametrize("prefix", ["VALKEY", "REDIS"])
def test_unreadable_password_file_does_not_fall_back(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setenv(f"{prefix}_PASSWORD_FILE", "/missing/store/secret")
    monkeypatch.setenv(f"{prefix}_PASSWORD", "fallback")
    with pytest.raises(ValueError, match=f"Cannot read secret file for {prefix}_PASSWORD"):
        config._build_valkey_url()


@pytest.mark.parametrize("prefix", ["VALKEY", "REDIS"])
def test_empty_password_file_omits_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prefix: str) -> None:
    secret = tmp_path / "password"
    secret.write_text("\n")
    monkeypatch.setenv(f"{prefix}_PASSWORD_FILE", str(secret))
    monkeypatch.setenv(f"{prefix}_PASSWORD", "ignored")
    assert config._build_valkey_url() == "valkey://localhost:6379/0"


def test_deprecation_warning_once_per_used_variable_across_both_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix, value in (("HOST", "legacy"), ("PORT", "6380"), ("PASSWORD", "sensitive-value")):
        monkeypatch.setenv(f"REDIS_{suffix}", value)
    with patch.object(config.logger, "warning") as warning:
        config._build_valkey_url()
        config._build_redis_url()
        config._build_valkey_url()
    assert warning.call_count == 3
    assert {call.kwargs["variable"] for call in warning.call_args_list} == {"REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"}
    assert "sensitive-value" not in str(warning.call_args_list)


def test_file_warning_names_only_used_legacy_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("secret")
    monkeypatch.setenv("REDIS_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("REDIS_PASSWORD", "unused")
    with patch.object(config.logger, "warning") as warning:
        config._build_valkey_url()
        config._build_redis_url()
    warning.assert_called_once_with("Deprecated Redis environment variable; use VALKEY_* instead", variable="REDIS_PASSWORD_FILE")


def test_unused_legacy_settings_do_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    for prefix in ("VALKEY", "REDIS"):
        for suffix in ("HOST", "PORT", "PASSWORD"):
            monkeypatch.setenv(f"{prefix}_{suffix}", "new" if prefix == "VALKEY" else "unused")
    with patch.object(config.logger, "warning") as warning:
        config._build_valkey_url()
        config._build_redis_url()
    warning.assert_not_called()
