"""Native identity: the closed vocabularies, id minting, and the bulk alias resolve.

ADR 0009 in the ``design`` repository demotes every provider identifier to evidence: GrooveMap
mints its own UUID version 7 for each native entity, and ``provider_aliases`` maps a provider's
``(provider, entity_kind, external_id)`` onto that native id. Both SQL loaders and
``catalog-api`` need the same lookup-or-create, so it lives here once rather than twice.

The vocabulary is read from ``common.identity_vocabulary`` package data with the standard
library only, the way :mod:`common.media` reads the media taxonomy, so the base
``groovemap-runtime`` install stays dependency-light.

Psycopg is not imported at runtime. :func:`resolve_aliases` and :func:`attach_aliases` act on a
connection the caller already holds, so the only thing this module needs from the ``postgres``
extra is the type of that argument — imported under ``TYPE_CHECKING`` and therefore absent at
run time. ``import common.identity`` succeeds with no psycopg installed, and the vocabulary and
:func:`new_id` work there; a caller without the extra simply has no connection to pass.

Both resolve functions run inside the **caller's** transaction and open no SAVEPOINT: the
Discogs loader holds one transaction per hundred-row batch and the MusicBrainz loader one per
message, and a nested rollback point would change their failure semantics. They take arrays
rather than looping (no ``executemany``), so a batch of any size costs three round trips, and
they are correct under an unspecified number of concurrent writers because the partial unique
index on ``(provider, entity_kind, external_id) WHERE valid_to IS NULL`` makes
``INSERT ... ON CONFLICT DO NOTHING`` plus a re-select converge on one native id.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Final

from common.query_debug import execute_sql


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from psycopg import AsyncConnection, AsyncCursor


__all__ = [
    "AliasRef",
    "alias_sources",
    "attach_aliases",
    "catalog_kinds",
    "entity_kinds",
    "is_valid_alias_source",
    "is_valid_entity_kind",
    "is_valid_provider",
    "new_id",
    "providers",
    "resolve_aliases",
]

_VOCABULARY_PACKAGE: Final = "common.identity_vocabulary"
_VOCABULARY_RESOURCE: Final = "identity-vocabulary.json"

# The entity kinds minted through `catalog_items` are exactly those the vocabulary declares that
# table for. Reading it off `native_table` keeps the set in the vocabulary rather than in a
# literal here, so a kind that moves tables moves without a code change.
_CATALOG_TABLE: Final = "catalog_items"

# One statement per round trip. The join against `unnest` is what makes the batch a single
# lookup: the three arrays are the batch's keys, and the partial unique index answers it.
_SELECT_ALIASES: Final = """
SELECT k.provider, k.entity_kind, k.external_id, alias.native_id
FROM unnest(%s::text[], %s::text[], %s::text[]) AS k(provider, entity_kind, external_id)
JOIN provider_aliases AS alias
  ON alias.provider = k.provider
 AND alias.entity_kind = k.entity_kind
 AND alias.external_id = k.external_id
WHERE alias.valid_to IS NULL
"""

_MINT_CATALOG_ITEMS: Final = """
INSERT INTO catalog_items (id, kind)
SELECT minted.id, minted.kind
FROM unnest(%s::uuid[], %s::text[]) AS minted(id, kind)
"""

# ON CONFLICT names the partial unique index by repeating its predicate; without the
# `WHERE valid_to IS NULL` there is no unique index for the inference to find and the statement
# fails outright. The RETURNING rows are the refs this writer won, so the ones it lost need no
# second bookkeeping pass to identify.
_INSERT_ALIASES: Final = """
INSERT INTO provider_aliases (provider, entity_kind, external_id, native_id, source, confidence)
SELECT candidate.provider, candidate.entity_kind, candidate.external_id, candidate.native_id, %s, %s
FROM unnest(%s::text[], %s::text[], %s::text[], %s::uuid[]) AS candidate(provider, entity_kind, external_id, native_id)
ON CONFLICT (provider, entity_kind, external_id) WHERE valid_to IS NULL DO NOTHING
RETURNING provider, entity_kind, external_id, native_id
"""

_DELETE_ORPHANED_CATALOG_ITEMS: Final = """
DELETE FROM catalog_items WHERE id = ANY(%s::uuid[])
"""


@cache
def _vocabulary() -> dict[str, Any]:
    """Return the vendored identity vocabulary, parsed once per process."""
    document = (files(_VOCABULARY_PACKAGE) / _VOCABULARY_RESOURCE).read_text(encoding="utf-8")
    vocabulary: dict[str, Any] = json.loads(document)
    return vocabulary


@cache
def entity_kinds() -> tuple[str, ...]:
    """Return every entity kind an alias may name, in vocabulary order."""
    return tuple(kind["id"] for kind in _vocabulary()["entity_kinds"])


@cache
def catalog_kinds() -> tuple[str, ...]:
    """Return the entity kinds minted through ``catalog_items``, in vocabulary order.

    These are the only kinds :func:`resolve_aliases` mints. A miss on any other kind is returned
    absent, because the entity it names originates somewhere else: ``catalog-api`` mints owned
    copies, collection snapshots, and observations itself.
    """
    return tuple(kind["id"] for kind in _vocabulary()["entity_kinds"] if kind["native_table"] == _CATALOG_TABLE)


@cache
def providers() -> tuple[str, ...]:
    """Return every provider namespace an alias may come from, in vocabulary order.

    Barcodes, catalogue numbers, ISRCs, and matrix inscriptions are providers here for the same
    reason Discogs is: they are external namespaces that identify an entity without belonging to
    GrooveMap, so a new identifier source is a row rather than a column.
    """
    return tuple(provider["id"] for provider in _vocabulary()["providers"])


@cache
def alias_sources() -> tuple[str, ...]:
    """Return the closed set of alias sources, in vocabulary order.

    The set separates an alias a person asserted from one a matching heuristic proposed, so
    evidence is never read as an assertion.
    """
    return tuple(source["id"] for source in _vocabulary()["sources"])


def is_valid_provider(provider: str) -> bool:
    """Return whether ``provider`` names a provider the vocabulary carries."""
    return provider in providers()


def is_valid_entity_kind(entity_kind: str) -> bool:
    """Return whether ``entity_kind`` names an entity kind the vocabulary carries."""
    return entity_kind in entity_kinds()


def is_valid_alias_source(source: str) -> bool:
    """Return whether ``source`` names an alias source the vocabulary carries."""
    return source in alias_sources()


def new_id() -> uuid.UUID:
    """Mint a native identifier: a UUID version 7, as ADR 0009 specifies.

    Time-ordered, so index locality and insert ordering behave like a sequence without a central
    allocator, and opaque, so it carries no provider meaning. This is the same format the
    database's ``uuidv7()`` column default produces; the deployment pins PostgreSQL 18 and
    Python 3.14 precisely so both sides can generate it from a standard facility.
    """
    return uuid.uuid7()


@dataclass(frozen=True, slots=True)
class AliasRef:
    """One provider identifier, as the alias table keys it.

    The three fields are the lookup key of the partial unique index, so a ref is hashable and is
    used directly as a dictionary key in the results of :func:`resolve_aliases` and
    :func:`attach_aliases`.

    ``external_id`` is a string because the column is ``TEXT``: a provider's numeric id is
    stringified by the caller, so a Discogs ``123`` and the barcode ``"123"`` never collide on a
    type coercion that happened somewhere else.

    Raises:
        ValueError: If ``provider`` or ``entity_kind`` is outside the closed vocabulary, or
            ``external_id`` is empty. Constructing the ref is the validation point, so no
            malformed ref can reach a statement.
    """

    provider: str
    entity_kind: str
    external_id: str

    def __post_init__(self) -> None:
        if not is_valid_provider(self.provider):
            raise ValueError(f"unknown provider {self.provider!r}; the vocabulary carries {', '.join(providers())}")
        if not is_valid_entity_kind(self.entity_kind):
            raise ValueError(f"unknown entity kind {self.entity_kind!r}; the vocabulary carries {', '.join(entity_kinds())}")
        if not self.external_id:
            raise ValueError("external_id must be a non-empty string")


def _sort_key(ref: AliasRef) -> tuple[str, str, str]:
    return (ref.provider, ref.entity_kind, ref.external_id)


def _ordered(refs: Iterable[AliasRef]) -> list[AliasRef]:
    """De-duplicate refs and put them in one deterministic order.

    Two writers that submit the same batch in different orders then send the same array values,
    which keeps a statement's plan and a test's expectation stable.
    """
    return sorted(dict.fromkeys(refs), key=_sort_key)


def _require_known_source(source: str) -> None:
    if not is_valid_alias_source(source):
        raise ValueError(f"unknown alias source {source!r}; the vocabulary carries {', '.join(alias_sources())}")


async def _select_aliases(cursor: AsyncCursor[Any], refs: list[AliasRef]) -> dict[AliasRef, uuid.UUID]:
    """Return the native id of every ref that currently has a valid alias."""
    await execute_sql(
        cursor,
        _SELECT_ALIASES,
        ([ref.provider for ref in refs], [ref.entity_kind for ref in refs], [ref.external_id for ref in refs]),
    )
    return {AliasRef(provider, entity_kind, external_id): native_id for provider, entity_kind, external_id, native_id in await cursor.fetchall()}


async def _insert_aliases(
    cursor: AsyncCursor[Any],
    refs: list[AliasRef],
    native_ids: dict[AliasRef, uuid.UUID],
    source: str,
    confidence: float,
) -> dict[AliasRef, uuid.UUID]:
    """Insert one alias per ref, and return only the refs this writer actually wrote."""
    await execute_sql(
        cursor,
        _INSERT_ALIASES,
        (
            source,
            confidence,
            [ref.provider for ref in refs],
            [ref.entity_kind for ref in refs],
            [ref.external_id for ref in refs],
            [native_ids[ref] for ref in refs],
        ),
    )
    return {AliasRef(provider, entity_kind, external_id): native_id for provider, entity_kind, external_id, native_id in await cursor.fetchall()}


async def resolve_aliases(
    conn: AsyncConnection[Any],
    refs: Iterable[AliasRef],
    *,
    source: str = "catalog",
    confidence: float = 1.0,
) -> dict[AliasRef, uuid.UUID]:
    """Resolve provider identifiers to native ids, minting catalog items for the misses.

    Three round trips regardless of batch size:

    1. One ``SELECT`` joining the batch's keys against the currently valid alias rows.
    2. For every miss whose kind is in :func:`catalog_kinds`, one ``INSERT`` into
       ``catalog_items`` and one ``INSERT ... ON CONFLICT DO NOTHING`` into ``provider_aliases``.
       The conflict target repeats the partial index's predicate, so a concurrent writer that
       already claimed the key makes this writer's row vanish instead of raising.
    3. If anything was lost to a concurrent writer, one re-``SELECT`` that reads the id the
       winner minted, and one ``DELETE`` of the catalog items this writer minted for those refs,
       so a race leaves no orphan behind.

    Native ids are minted with :func:`new_id` rather than read back from ``RETURNING``. The
    ``RETURNING`` clause of a multi-row insert has no defined row order, so mapping its ids back
    onto the refs that caused them would rest on an undocumented behaviour; ADR 0009 sanctions
    ``uuid.uuid7()`` in a service as the same facility the ``uuidv7()`` column default uses.

    Runs inside the caller's transaction and opens no SAVEPOINT, so a failure surfaces to the
    caller's own rollback.

    Args:
        conn: A psycopg ``AsyncConnection`` already inside a transaction.
        refs: The provider identifiers to resolve. Duplicates collapse, and the batch is put in
            one deterministic order before any statement runs.
        source: The alias source recorded on rows this call writes. Must be in
            :func:`alias_sources`.
        confidence: The confidence recorded on rows this call writes.

    Returns:
        A dict from ref to native id, holding only the refs that resolved. A miss whose kind is
        not a catalog kind is absent, and so is a ref that lost a race and whose winner's alias
        was closed in between.

    Raises:
        ValueError: If ``source`` is outside the closed set. Raised before any statement runs.
    """
    _require_known_source(source)
    ordered = _ordered(refs)
    if not ordered:
        return {}

    minted_kinds = catalog_kinds()
    async with conn.cursor() as cursor:
        resolved = await _select_aliases(cursor, ordered)
        mintable = [ref for ref in ordered if ref not in resolved and ref.entity_kind in minted_kinds]
        if not mintable:
            return resolved

        minted = {ref: new_id() for ref in mintable}
        await execute_sql(cursor, _MINT_CATALOG_ITEMS, ([minted[ref] for ref in mintable], [ref.entity_kind for ref in mintable]))
        won = await _insert_aliases(cursor, mintable, minted, source, confidence)
        resolved.update(won)

        lost = [ref for ref in mintable if ref not in won]
        if lost:
            resolved.update(await _select_aliases(cursor, lost))
            await execute_sql(cursor, _DELETE_ORPHANED_CATALOG_ITEMS, ([minted[ref] for ref in lost],))

    return resolved


async def attach_aliases(
    conn: AsyncConnection[Any],
    mapping: Mapping[AliasRef, uuid.UUID],
    *,
    source: str = "catalog",
    confidence: float = 1.0,
) -> dict[AliasRef, uuid.UUID]:
    """Attach provider identifiers to native ids that are already known.

    This is how a second catalog joins an item the first one minted: a MusicBrainz release that
    carries a ``discogs_release_id`` attaches its own alias to the native id the Discogs loader
    already minted, instead of minting a second item for the same record.

    The existing alias always wins. When a valid alias for a ref is already present, the insert
    conflicts away and the re-select returns the id that alias points at, which may differ from
    the id the caller supplied. Nothing is ever overwritten, so an attach can be retried and can
    race another writer without rewriting identity.

    Two round trips at most: one ``INSERT ... ON CONFLICT DO NOTHING``, and one ``SELECT`` for
    the refs that conflicted. No catalog item is minted and none is deleted, because every
    native id here was minted by the caller. Runs inside the caller's transaction with no
    SAVEPOINT.

    Args:
        conn: A psycopg ``AsyncConnection`` already inside a transaction.
        mapping: The native id each provider identifier should attach to.
        source: The alias source recorded on rows this call writes. Must be in
            :func:`alias_sources`.
        confidence: The confidence recorded on rows this call writes.

    Returns:
        A dict from ref to the native id the ref actually resolves to after the insert: the
        supplied id when this call wrote the alias, and the existing alias's id when one was
        already present. A ref whose alias was closed between the insert and the re-select is
        absent.

    Raises:
        ValueError: If ``source`` is outside the closed set. Raised before any statement runs.
    """
    _require_known_source(source)
    ordered = _ordered(mapping)
    if not ordered:
        return {}

    async with conn.cursor() as cursor:
        resolved = await _insert_aliases(cursor, ordered, dict(mapping), source, confidence)
        conflicted = [ref for ref in ordered if ref not in resolved]
        if conflicted:
            resolved.update(await _select_aliases(cursor, conflicted))

    return resolved
