"""Tests for the native identity vocabulary, id minting, and the bulk alias resolve.

Every test here is offline. The two resolve functions act on a caller-supplied psycopg
``AsyncConnection``, so a fake connection whose cursor replays queued result sets exercises the
whole algorithm — including the concurrent-writer race — without a database.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import uuid
from typing import Any

import pytest

from common.identity import (
    AliasRef,
    alias_sources,
    attach_aliases,
    catalog_kinds,
    entity_kinds,
    is_valid_alias_source,
    is_valid_entity_kind,
    is_valid_provider,
    new_id,
    providers,
    resolve_aliases,
)


DISCOGS_RELEASE = AliasRef("discogs", "release", "249504")
DISCOGS_ARTIST = AliasRef("discogs", "artist", "1289")
MUSICBRAINZ_RELEASE = AliasRef("musicbrainz", "release", "f4f1f1f1-0000-4000-8000-000000000001")
BARCODE_OWNED_COPY = AliasRef("barcode", "owned_copy", "0602557")

NATIVE_A = uuid.UUID("0192f0b2-0000-7000-8000-00000000c001")
NATIVE_B = uuid.UUID("0192f0b2-0000-7000-8000-00000000c002")
NATIVE_C = uuid.UUID("0192f0b2-0000-7000-8000-00000000c003")

Rows = list[tuple[Any, ...]]


def echo_inserted(params: Any) -> Rows:
    """Report every candidate row back, as the database does when this writer wins every key."""
    providers_, kinds, external_ids, native_ids = params[2], params[3], params[4], params[5]
    return list(zip(providers_, kinds, external_ids, native_ids, strict=True))


def alias_row(ref: AliasRef, native_id: uuid.UUID) -> tuple[Any, ...]:
    return (ref.provider, ref.entity_kind, ref.external_id, native_id)


class FakeCursor:
    """An async cursor that records statements and replays queued result sets.

    A queued entry is either a literal list of rows or a callable that derives the rows from the
    statement's parameters, which is how a winning ``INSERT ... RETURNING`` is modelled without
    hard-coding the ids the helper minted.
    """

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._queued: list[Any] = []
        self._rows: Rows = []

    def queue(self, *result_sets: Any) -> None:
        self._queued.extend(result_sets)

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))
        result = self._queued.pop(0) if self._queued else []
        self._rows = result(params) if callable(result) else result

    async def fetchall(self) -> Rows:
        return self._rows

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    @property
    def statements(self) -> list[str]:
        return [re.sub(r"\s+", " ", query).strip() for query, _ in self.executed]

    def parameters_of(self, index: int) -> Any:
        return self.executed[index][1]


class FakeConnection:
    """Just enough of a psycopg ``AsyncConnection`` for the resolve functions."""

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor


@pytest.fixture
def cursor() -> FakeCursor:
    return FakeCursor()


@pytest.fixture
def conn(cursor: FakeCursor) -> FakeConnection:
    return FakeConnection(cursor)


def minted_ids(cursor: FakeCursor) -> list[uuid.UUID]:
    """Return the ids the mint statement generated, in the order it sent them."""
    ids: list[uuid.UUID] = cursor.parameters_of(1)[0]
    return ids


class TestVocabulary:
    def test_entity_kinds_are_the_vendored_closed_set(self) -> None:
        assert entity_kinds() == ("release", "master", "artist", "label", "artifact", "owned_copy", "collection_snapshot", "observation")

    def test_catalog_kinds_are_the_four_minted_through_catalog_items(self) -> None:
        assert catalog_kinds() == ("release", "master", "artist", "label")
        assert set(catalog_kinds()) < set(entity_kinds())

    def test_providers_cover_catalogs_and_bare_identifier_namespaces(self) -> None:
        assert providers() == ("discogs", "musicbrainz", "wikidata", "barcode", "catalog_number", "isrc", "matrix")

    def test_alias_sources_separate_assertion_from_inference(self) -> None:
        assert alias_sources() == ("catalog", "user", "inference")

    def test_membership_predicates_answer_for_known_and_unknown_values(self) -> None:
        assert is_valid_provider("discogs")
        assert not is_valid_provider("spotify")
        assert is_valid_entity_kind("owned_copy")
        assert not is_valid_entity_kind("playlist")
        assert is_valid_alias_source("inference")
        assert not is_valid_alias_source("guess")


class TestNewId:
    def test_new_id_mints_a_version_7_uuid(self) -> None:
        identifier = new_id()

        assert isinstance(identifier, uuid.UUID)
        assert identifier.version == 7

    def test_minted_ids_are_distinct_and_time_ordered(self) -> None:
        minted = [new_id() for _ in range(16)]

        assert len(set(minted)) == 16
        assert [identifier.int for identifier in minted] == sorted(identifier.int for identifier in minted)


class TestAliasRef:
    def test_a_ref_is_hashable_and_compares_by_value(self) -> None:
        assert AliasRef("discogs", "release", "249504") == DISCOGS_RELEASE
        assert len({AliasRef("discogs", "release", "249504"), DISCOGS_RELEASE}) == 1

    @pytest.mark.parametrize(
        ("provider", "entity_kind", "external_id", "expected"),
        [
            ("spotify", "release", "1", "unknown provider"),
            ("discogs", "playlist", "1", "unknown entity kind"),
            ("discogs", "release", "", "external_id"),
        ],
    )
    def test_a_value_outside_the_closed_sets_raises_at_construction(self, provider: str, entity_kind: str, external_id: str, expected: str) -> None:
        with pytest.raises(ValueError, match=expected):
            AliasRef(provider, entity_kind, external_id)


class TestResolveAliases:
    @pytest.mark.asyncio
    async def test_an_empty_batch_runs_no_statement(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        assert await resolve_aliases(conn, []) == {}
        assert cursor.executed == []

    @pytest.mark.asyncio
    async def test_an_unknown_source_raises_before_any_statement(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        with pytest.raises(ValueError, match="unknown alias source"):
            await resolve_aliases(conn, [DISCOGS_RELEASE], source="vibes")

        assert cursor.executed == []

    @pytest.mark.asyncio
    async def test_a_batch_that_is_all_hits_costs_one_round_trip(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([alias_row(DISCOGS_RELEASE, NATIVE_A), alias_row(DISCOGS_ARTIST, NATIVE_B)])

        resolved = await resolve_aliases(conn, [DISCOGS_RELEASE, DISCOGS_ARTIST])

        assert resolved == {DISCOGS_RELEASE: NATIVE_A, DISCOGS_ARTIST: NATIVE_B}
        assert len(cursor.executed) == 1

    @pytest.mark.asyncio
    async def test_a_batch_that_is_all_misses_mints_and_inserts(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([], [], echo_inserted)

        resolved = await resolve_aliases(conn, [DISCOGS_ARTIST, DISCOGS_RELEASE])

        assert set(resolved) == {DISCOGS_ARTIST, DISCOGS_RELEASE}
        assert all(native.version == 7 for native in resolved.values())
        assert sorted(resolved.values()) == sorted(minted_ids(cursor))
        assert len(cursor.executed) == 3

    @pytest.mark.asyncio
    async def test_a_mixed_batch_mints_only_the_misses(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([alias_row(DISCOGS_RELEASE, NATIVE_A)], [], echo_inserted)

        resolved = await resolve_aliases(conn, [DISCOGS_RELEASE, MUSICBRAINZ_RELEASE])

        assert resolved[DISCOGS_RELEASE] == NATIVE_A
        assert resolved[MUSICBRAINZ_RELEASE] == minted_ids(cursor)[0]
        assert cursor.parameters_of(1)[1] == ["release"]
        assert cursor.parameters_of(2)[2] == ["musicbrainz"]
        assert len(cursor.executed) == 3

    @pytest.mark.asyncio
    async def test_a_miss_on_a_kind_that_is_not_minted_here_is_returned_absent(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([])

        resolved = await resolve_aliases(conn, [BARCODE_OWNED_COPY])

        assert resolved == {}
        assert len(cursor.executed) == 1

    @pytest.mark.asyncio
    async def test_losing_the_race_reselects_the_winner_and_deletes_the_orphan(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue(
            [],  # nothing resolved yet
            [],  # the mint returns nothing
            [],  # the alias insert conflicted away: this writer lost
            [alias_row(DISCOGS_RELEASE, NATIVE_C)],  # the winner's id
            [],  # the orphan delete
        )

        resolved = await resolve_aliases(conn, [DISCOGS_RELEASE])

        assert resolved == {DISCOGS_RELEASE: NATIVE_C}
        assert len(cursor.executed) == 5
        assert "DELETE FROM catalog_items" in cursor.executed[4][0]
        assert cursor.parameters_of(4) == (minted_ids(cursor),)

    @pytest.mark.asyncio
    async def test_a_ref_whose_winner_vanished_is_returned_absent_and_still_cleans_up(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([], [], [], [], [])

        resolved = await resolve_aliases(conn, [DISCOGS_RELEASE])

        assert resolved == {}
        assert "DELETE FROM catalog_items" in cursor.executed[4][0]

    @pytest.mark.asyncio
    async def test_duplicate_refs_collapse_and_the_batch_is_ordered_deterministically(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([alias_row(DISCOGS_RELEASE, NATIVE_A), alias_row(DISCOGS_ARTIST, NATIVE_B)], [], echo_inserted)

        await resolve_aliases(conn, [DISCOGS_RELEASE, MUSICBRAINZ_RELEASE, DISCOGS_ARTIST, DISCOGS_RELEASE])

        assert cursor.parameters_of(0) == (
            ["discogs", "discogs", "musicbrainz"],
            ["artist", "release", "release"],
            [DISCOGS_ARTIST.external_id, DISCOGS_RELEASE.external_id, MUSICBRAINZ_RELEASE.external_id],
        )

    @pytest.mark.asyncio
    async def test_the_source_and_confidence_are_written_onto_the_alias_row(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([], [], echo_inserted)

        await resolve_aliases(conn, [DISCOGS_RELEASE], source="inference", confidence=0.6)

        assert cursor.parameters_of(2)[:2] == ("inference", 0.6)


class TestStatementShapes:
    @pytest.mark.asyncio
    async def test_every_statement_is_array_parameterized_and_targets_the_partial_index(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([], [], [], [], [])

        await resolve_aliases(conn, [DISCOGS_RELEASE])

        select, mint, insert, reselect, delete = cursor.statements
        assert "unnest(%s::text[], %s::text[], %s::text[])" in select
        assert "WHERE alias.valid_to IS NULL" in select
        assert select == reselect
        assert mint.startswith("INSERT INTO catalog_items (id, kind)")
        assert "unnest(%s::uuid[], %s::text[])" in mint
        assert "unnest(%s::text[], %s::text[], %s::text[], %s::uuid[])" in insert
        assert "ON CONFLICT (provider, entity_kind, external_id) WHERE valid_to IS NULL DO NOTHING" in insert
        assert "RETURNING provider, entity_kind, external_id, native_id" in insert
        assert delete == "DELETE FROM catalog_items WHERE id = ANY(%s::uuid[])"

    @pytest.mark.asyncio
    async def test_no_statement_opens_a_savepoint_or_ends_the_callers_transaction(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([], [], [], [], [], echo_inserted)

        await resolve_aliases(conn, [DISCOGS_RELEASE])
        await attach_aliases(conn, {DISCOGS_ARTIST: NATIVE_A})

        forbidden = ("SAVEPOINT", "COMMIT", "ROLLBACK", "BEGIN")
        assert not [statement for statement in cursor.statements if any(word in statement.upper() for word in forbidden)]


class TestAttachAliases:
    @pytest.mark.asyncio
    async def test_an_empty_mapping_runs_no_statement(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        assert await attach_aliases(conn, {}) == {}
        assert cursor.executed == []

    @pytest.mark.asyncio
    async def test_an_unknown_source_raises_before_any_statement(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        with pytest.raises(ValueError, match="unknown alias source"):
            await attach_aliases(conn, {DISCOGS_RELEASE: NATIVE_A}, source="vibes")

        assert cursor.executed == []

    @pytest.mark.asyncio
    async def test_attaching_to_a_known_id_writes_one_alias(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue(echo_inserted)

        resolved = await attach_aliases(conn, {MUSICBRAINZ_RELEASE: NATIVE_A})

        assert resolved == {MUSICBRAINZ_RELEASE: NATIVE_A}
        assert len(cursor.executed) == 1
        assert cursor.parameters_of(0) == ("catalog", 1.0, ["musicbrainz"], ["release"], [MUSICBRAINZ_RELEASE.external_id], [NATIVE_A])

    @pytest.mark.asyncio
    async def test_an_existing_alias_wins_and_is_never_overwritten(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue(
            [],  # the insert conflicted away
            [alias_row(MUSICBRAINZ_RELEASE, NATIVE_B)],  # the id already on record
        )

        resolved = await attach_aliases(conn, {MUSICBRAINZ_RELEASE: NATIVE_A})

        assert resolved == {MUSICBRAINZ_RELEASE: NATIVE_B}
        assert len(cursor.executed) == 2
        assert "INSERT INTO provider_aliases" in cursor.executed[0][0]
        assert cursor.executed[1][0].lstrip().startswith("SELECT")

    @pytest.mark.asyncio
    async def test_a_mapping_that_partly_conflicts_reselects_only_the_conflicts(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue(
            [alias_row(DISCOGS_RELEASE, NATIVE_A)],
            [alias_row(MUSICBRAINZ_RELEASE, NATIVE_C)],
        )

        resolved = await attach_aliases(conn, {DISCOGS_RELEASE: NATIVE_A, MUSICBRAINZ_RELEASE: NATIVE_B})

        assert resolved == {DISCOGS_RELEASE: NATIVE_A, MUSICBRAINZ_RELEASE: NATIVE_C}
        assert cursor.parameters_of(1) == (["musicbrainz"], ["release"], [MUSICBRAINZ_RELEASE.external_id])

    @pytest.mark.asyncio
    async def test_a_conflict_whose_alias_vanished_is_returned_absent(self, conn: FakeConnection, cursor: FakeCursor) -> None:
        cursor.queue([], [])

        assert await attach_aliases(conn, {MUSICBRAINZ_RELEASE: NATIVE_A}) == {}


def test_the_module_imports_without_the_postgres_extra() -> None:
    """Psycopg is a type-checking import only, so the base install can read the vocabulary."""
    program = textwrap.dedent(
        """
        import sys

        class BlockPsycopg:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "psycopg":
                    raise ImportError(f"no module named {name}")
                return None

        sys.meta_path.insert(0, BlockPsycopg())
        import common.identity as identity

        assert "psycopg" not in sys.modules
        assert identity.new_id().version == 7
        assert identity.catalog_kinds() == ("release", "master", "artist", "label")
        assert identity.AliasRef("discogs", "release", "1").external_id == "1"
        """
    )
    completed = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=False)  # noqa: S603

    assert completed.returncode == 0, completed.stderr
