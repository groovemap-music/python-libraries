"""Contracts for Valkey-only store connection settings."""

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from common import config


if TYPE_CHECKING:
    from pathlib import Path


_SUFFIXES = ("HOST", "PORT", "PASSWORD", "PASSWORD_FILE", "DB", "KEY_PREFIX")
_LEGACY = {
    "REDIS_HOST": "legacy",
    "REDIS_PORT": "6390",
    "REDIS_PASSWORD": "legacy-secret",
    "REDIS_PASSWORD_FILE": "/missing/legacy/secret",
    "REDIS_DB": "8",
}


@pytest.fixture(autouse=True)
def isolated_store_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Machine settings must not leak into any case."""
    for prefix in ("VALKEY", "REDIS"):
        for suffix in _SUFFIXES:
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)


@pytest.mark.parametrize(
    ("settings", "authority"),
    [
        ({}, "localhost:6379"),
        ({"VALKEY_HOST": "store"}, "store:6379"),
        ({"VALKEY_PORT": "6380"}, "localhost:6380"),
        ({"VALKEY_HOST": "store", "VALKEY_PORT": "6380"}, "store:6380"),
        ({"VALKEY_PASSWORD": "a:/@% b"}, ":a%3A%2F%40%25%20b@localhost:6379"),
        ({"VALKEY_PASSWORD": ""}, "localhost:6379"),
    ],
)
def test_connection_matrix(monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], authority: str) -> None:
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    assert config._build_valkey_url() == f"valkey://{authority}/0"
    assert config.get_valkey_url() == config._build_valkey_url()


def test_password_file_wins_and_is_quoted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("  file:/@% secret\n")
    monkeypatch.setenv("VALKEY_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("VALKEY_PASSWORD", "plain")
    assert config._build_valkey_url() == "valkey://:file%3A%2F%40%25%20secret@localhost:6379/0"


def test_unreadable_password_file_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_PASSWORD_FILE", "/missing/store/secret")
    monkeypatch.setenv("VALKEY_PASSWORD", "fallback")
    with pytest.raises(ValueError, match="Cannot read secret file for VALKEY_PASSWORD"):
        config._build_valkey_url()


def test_empty_password_file_omits_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "password"
    secret.write_text("\n")
    monkeypatch.setenv("VALKEY_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("VALKEY_PASSWORD", "ignored")
    assert config._build_valkey_url() == "valkey://localhost:6379/0"


# Negative regression coverage: the transitional REDIS_* aliases are gone.


def test_legacy_settings_alone_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy-only environment yields defaults; even an unreadable legacy file is not read."""
    for name, value in _LEGACY.items():
        monkeypatch.setenv(name, value)
    assert config._build_valkey_url() == "valkey://localhost:6379/0"


@pytest.mark.parametrize("legacy", sorted(_LEGACY))
def test_each_legacy_setting_never_fills_an_unset_valkey_setting(monkeypatch: pytest.MonkeyPatch, legacy: str) -> None:
    monkeypatch.setenv(legacy, _LEGACY[legacy])
    assert config._build_valkey_url() == "valkey://localhost:6379/0"


def test_legacy_settings_never_override_valkey_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _LEGACY.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("VALKEY_HOST", "store")
    monkeypatch.setenv("VALKEY_PORT", "6380")
    monkeypatch.setenv("VALKEY_PASSWORD", "new")
    assert config._build_valkey_url() == "valkey://:new@store:6380/0"


def test_redis_variables_are_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _LEGACY.items():
        monkeypatch.setenv(name, value)
    real_getenv = config.getenv
    seen: list[str] = []

    def recording_getenv(name: str, *args: str) -> str | None:
        seen.append(name)
        return real_getenv(name, *args)

    with patch.object(config, "getenv", recording_getenv):
        config._build_valkey_url()
    assert seen
    assert not [name for name in seen if name.startswith("REDIS")]


def test_legacy_settings_log_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _LEGACY.items():
        monkeypatch.setenv(name, value)
    with patch.object(config.logger, "warning") as warning:
        config._build_valkey_url()
    warning.assert_not_called()


@pytest.mark.parametrize(
    "removed", ["_build_redis_url", "_warn_redis_setting", "_deprecated_store_settings", "_store_setting", "_store_password", "_build_store_url"]
)
def test_transitional_helpers_are_removed(removed: str) -> None:
    assert not hasattr(config, removed)


# VALKEY_DB, VALKEY_KEY_PREFIX and the key helpers.


@pytest.mark.parametrize(("raw", "expected"), [(None, 0), ("", 0), ("  ", 0), ("0", 0), ("9", 9), (" 15 ", 15), ("255", 255)])
def test_valkey_db_parsing(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    if raw is not None:
        monkeypatch.setenv("VALKEY_DB", raw)
    assert config.valkey_db() == expected


@pytest.mark.parametrize("raw", ["abc", "1.5", "-1", "256", "99999"])
def test_valkey_db_rejects_invalid_or_out_of_range(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("VALKEY_DB", raw)
    with pytest.raises(ValueError, match="VALKEY_DB"):
        config.valkey_db()
    with pytest.raises(ValueError, match="VALKEY_DB"):
        config.get_valkey_url()


def test_url_includes_database_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VALKEY_HOST", "store")
    monkeypatch.setenv("VALKEY_PASSWORD", "pw")
    monkeypatch.setenv("VALKEY_DB", "9")
    assert config.get_valkey_url() == "valkey://:pw@store:6379/9"
    assert config._build_valkey_url() == "valkey://:pw@store:6379/9"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "groovemap:"),
        ("", ""),
        ("gm", "gm:"),
        ("gm:", "gm:"),
        ("env.prod-1_a:gm:", "env.prod-1_a:gm:"),
    ],
)
def test_key_prefix_parsing(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str) -> None:
    if raw is not None:
        monkeypatch.setenv("VALKEY_KEY_PREFIX", raw)
    assert config.valkey_key_prefix() == expected


@pytest.mark.parametrize("raw", ["*", "gm*", "a?b", "a[b]", "a\\b", "a b", "a/b", ":", "::", "gm\n", "gr\u00fcn"])
def test_key_prefix_rejects_glob_and_unsafe_characters(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("VALKEY_KEY_PREFIX", raw)
    with pytest.raises(ValueError, match="VALKEY_KEY_PREFIX"):
        config.valkey_key_prefix()


def test_valkey_key_prefixes_and_joins_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.valkey_key("recommend:enhanced:42") == "groovemap:recommend:enhanced:42"
    assert config.valkey_key("revoked", "jti", 7) == "groovemap:revoked:jti:7"
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "shared")
    assert config.valkey_key("insights", "g3") == "shared:insights:g3"
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "")
    assert config.valkey_key("insights:g3") == "insights:g3"


@pytest.mark.parametrize("parts", [(), ("",), ("a", "")])
def test_valkey_key_rejects_missing_parts(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="valkey_key"):
        config.valkey_key(*parts)


def test_valkey_pattern_keeps_wildcards_inside_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.valkey_pattern("insights:g*") == "groovemap:insights:g*"
    assert config.valkey_pattern("*") == "groovemap:*"
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "other")
    assert config.valkey_pattern("recommend:explore:7:*") == "other:recommend:explore:7:*"
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "")
    assert config.valkey_pattern("insights:g*") == "insights:g*"


def test_valkey_pattern_rejects_empty_pattern() -> None:
    with pytest.raises(ValueError, match="valkey_pattern"):
        config.valkey_pattern("")


def test_key_and_pattern_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key built by valkey_key is matched by the valkey_pattern for its family."""
    from fnmatch import fnmatchcase

    for prefix in (None, "", "team-a"):
        if prefix is None:
            monkeypatch.delenv("VALKEY_KEY_PREFIX", raising=False)
        else:
            monkeypatch.setenv("VALKEY_KEY_PREFIX", prefix)
        assert fnmatchcase(config.valkey_key("insights:g2:top"), config.valkey_pattern("insights:g*"))
    monkeypatch.setenv("VALKEY_KEY_PREFIX", "team-a")
    assert not fnmatchcase("team-b:insights:g2:top", config.valkey_pattern("insights:g*"))
