"""Canonical identifier and company blocks: the closed vocabularies, validation, and alias extraction.

ADR 0011 in the ``design`` repository adds two additive blocks to a release: ``identifiers``
carries every catalogue identifier a provider published, and ``companies`` carries the
manufacturing and rights credits. Both loaders write the blocks and ``catalog-api`` reads them,
and the loaders additionally mint provider aliases from the three alias-bearing identifier
types, so the vocabularies, the block contract, and the one normalization of an alias value
live here rather than three times over.

The vocabularies and their JSON Schemas are vendored into ``common.identifier_vocabulary`` and
``common.company_role_vocabulary`` with digest checks. As in :mod:`common.identity` and
:mod:`common.media`, they are read with the standard library only, so the base
``groovemap-runtime`` install gains no schema library and a consumer pinned to an older
lockfile keeps resolving.

What keeps the validators honest is the conformance test rather than the shared code:
``tests/test_identifiers.py`` validates every vendored design fixture, and a mutation of each,
against the vendored JSON Schemas with ``jsonschema`` (a development dependency) and asserts
these validators return the same verdict every time. The Python contract therefore cannot drift
from the published schema without a failing test.

The alias normalization is read off the vocabulary's ``alias_namespaces`` rather than written
out here, so the three namespaces stay in one place: ``barcode`` compares as digits only,
``catalog_number`` compares upper-cased with whitespace collapsed, and ``matrix`` keeps its case
because the characters stamped into the disc are the evidence. A value that normalizes away to
nothing mints no alias, which is how a barcode field holding punctuation alone is dropped rather
than turned into an empty key.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Final

from common.identity import AliasRef


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


__all__ = [
    "IdentifierValidationError",
    "alias_identifier_types",
    "alias_refs_for_release",
    "company_role_categories",
    "identifier_types",
    "validate_companies_block",
    "validate_identifiers_block",
]

_IDENTIFIER_PACKAGE: Final = "common.identifier_vocabulary"
_IDENTIFIER_VOCABULARY_RESOURCE: Final = "identifier-types.json"
_IDENTIFIER_BLOCK_SCHEMA_RESOURCE: Final = "identifier-block.schema.json"

_COMPANY_PACKAGE: Final = "common.company_role_vocabulary"
_COMPANY_VOCABULARY_RESOURCE: Final = "company-roles.json"
_COMPANY_BLOCK_SCHEMA_RESOURCE: Final = "company-block.schema.json"

# The block schemas pin these three as `const`. They are spelled out rather than read back from
# the schema because a validator that derived its constants from the document it validates could
# not fail on a changed one; `tests/test_identifiers.py` asserts each still matches the schema.
_IDENTIFIERS_VERSION: Final = "1"
_COMPANIES_VERSION: Final = "1"
_COMPANY_SOURCE_PROVIDER: Final = "discogs"

# `entity_type` is a Discogs numeric id carried as a string. The schema spells the constraint
# `^[0-9]+$`; a full match is the same rule without Python's trailing-newline allowance for `$`.
_ENTITY_TYPE = re.compile(r"[0-9]+")
_WHITESPACE_RUN = re.compile(r"\s+")
_NON_DIGIT = re.compile(r"[^0-9]")

# The default entity kind an identifiers block mints aliases for. Blocks are attached to
# releases today; the argument exists so a future release-shaped entity does not need a second
# extractor.
_RELEASE: Final = "release"


class IdentifierValidationError(ValueError):
    """One field of an identifiers or companies block failed validation.

    Subclasses ``ValueError`` so a writer that already guards against bad input keeps working,
    and names the offending field with its path inside the block, so a rejection is actionable
    without re-deriving it from the message.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field = field_name
        self.message = message


def _read_vendored(package: str, resource: str) -> dict[str, Any]:
    document = (files(package) / resource).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(document)
    return parsed


@cache
def _identifier_vocabulary() -> dict[str, Any]:
    """Return the vendored identifier-type vocabulary, parsed once per process."""
    return _read_vendored(_IDENTIFIER_PACKAGE, _IDENTIFIER_VOCABULARY_RESOURCE)


@cache
def _identifier_block_schema() -> dict[str, Any]:
    """Return the vendored identifiers-block JSON Schema, parsed once per process."""
    return _read_vendored(_IDENTIFIER_PACKAGE, _IDENTIFIER_BLOCK_SCHEMA_RESOURCE)


@cache
def _company_vocabulary() -> dict[str, Any]:
    """Return the vendored company-role vocabulary, parsed once per process."""
    return _read_vendored(_COMPANY_PACKAGE, _COMPANY_VOCABULARY_RESOURCE)


@cache
def _company_block_schema() -> dict[str, Any]:
    """Return the vendored companies-block JSON Schema, parsed once per process."""
    return _read_vendored(_COMPANY_PACKAGE, _COMPANY_BLOCK_SCHEMA_RESOURCE)


@cache
def identifier_types() -> tuple[str, ...]:
    """Return the closed version 1 identifier-type vocabulary, in vocabulary order.

    Adding a type is additive within version 1; renaming or removing one is a new vocabulary
    version, because a stored ``type`` is historical data in every release-shaped store.
    """
    return tuple(entry["id"] for entry in _identifier_vocabulary()["identifier_types"])


@cache
def alias_identifier_types() -> tuple[str, ...]:
    """Return the identifier types that mint a provider alias, in vocabulary order.

    These are the three types the vocabulary declares an alias namespace for — ``barcode``,
    ``catalog_number``, and ``matrix_runout``, minting into the ``barcode``, ``catalog_number``,
    and ``matrix`` provider namespaces respectively. Every other type is evidence carried on the
    block and nothing more: a label code or a rights society identifies an organisation, not the
    release, and an ASIN names a retailer's listing rather than the edition.
    """
    return tuple(namespace["type"] for namespace in _identifier_vocabulary()["alias_namespaces"])


@cache
def company_role_categories() -> tuple[str, ...]:
    """Return the closed version 1 company role-category vocabulary, in vocabulary order.

    The categories are what the graph projects as ``CREDITED_TO`` edges; the raw provider role
    survives on the item beside the category, so a role the vocabulary does not model separately
    is preserved rather than dropped.
    """
    return tuple(category["id"] for category in _company_vocabulary()["role_categories"])


@cache
def _alias_namespaces() -> dict[str, tuple[str, str]]:
    """Map each alias-bearing identifier type to its provider namespace and normalization id."""
    return {namespace["type"]: (namespace["provider"], namespace["normalization"]) for namespace in _identifier_vocabulary()["alias_namespaces"]}


def _digits_only(value: str) -> str:
    """Keep the ASCII digits of the value in order and discard every other character."""
    return _NON_DIGIT.sub("", value)


def _collapse_space(value: str) -> str:
    """Trim the value and collapse every run of whitespace to one space, preserving case."""
    return _WHITESPACE_RUN.sub(" ", value).strip()


def _upper_collapse_space(value: str) -> str:
    """Trim the value, collapse every run of whitespace to one space, then upper-case it."""
    return _collapse_space(value).upper()


_NORMALIZERS: Final[dict[str, Callable[[str], str]]] = {
    "digits_only": _digits_only,
    "upper_collapse_space": _upper_collapse_space,
    "collapse_space": _collapse_space,
}


def _object_contract(node: Mapping[str, Any]) -> tuple[tuple[str, ...], frozenset[str]]:
    """Return one schema object's required names and the complete set it admits.

    Both block schemas close every object with ``additionalProperties: false``, so the declared
    properties are exactly the admitted names.
    """
    return tuple(node["required"]), frozenset(node["properties"])


@cache
def _identifier_contracts() -> dict[str, tuple[tuple[str, ...], frozenset[str]]]:
    schema = _identifier_block_schema()
    properties = schema["properties"]
    item = properties["items"]["items"]
    return {
        "block": _object_contract(schema),
        "item": _object_contract(item),
        "source": _object_contract(item["properties"]["source"]),
        "alias": _object_contract(properties["aliases"]["items"]),
        "unmapped": _object_contract(properties["unmapped"]),
    }


@cache
def _company_contracts() -> dict[str, tuple[tuple[str, ...], frozenset[str]]]:
    schema = _company_block_schema()
    properties = schema["properties"]
    item = properties["items"]["items"]
    return {
        "block": _object_contract(schema),
        "item": _object_contract(item),
        "source": _object_contract(item["properties"]["source"]),
        "unmapped": _object_contract(properties["unmapped"]),
    }


@cache
def _alias_providers() -> tuple[str, ...]:
    """Return the provider namespaces an alias entry may name, as the block schema closes them."""
    return tuple(_identifier_block_schema()["properties"]["aliases"]["items"]["properties"]["provider"]["enum"])


@cache
def _source_fields() -> tuple[str, ...]:
    """Return the provider fields an identifier item may be lifted from, per the block schema."""
    return tuple(_identifier_block_schema()["properties"]["items"]["items"]["properties"]["source"]["properties"]["field"]["enum"])


@cache
def _identifier_source_providers() -> tuple[str, ...]:
    """Return the identifier source providers admitted by the published block schema."""
    return tuple(_identifier_block_schema()["properties"]["items"]["items"]["properties"]["source"]["properties"]["provider"]["enum"])


def _require_object(field_name: str, value: Any, contract: tuple[tuple[str, ...], frozenset[str]]) -> Mapping[str, Any]:
    """Enforce one closed schema object: its type, its required set, and its admitted names."""
    if not isinstance(value, dict):
        raise IdentifierValidationError(field_name, "must be an object")
    required, allowed = contract
    for name in required:
        if name not in value:
            raise IdentifierValidationError(f"{field_name}.{name}" if field_name else name, "is required")
    for name in sorted(value):
        if name not in allowed:
            raise IdentifierValidationError(f"{field_name}.{name}" if field_name else name, "is not part of the block")
    document: Mapping[str, Any] = value
    return document


def _require_const(field_name: str, value: Any, expected: str) -> None:
    if value != expected or not isinstance(value, str):
        raise IdentifierValidationError(field_name, f"must be {expected!r}")


def _require_text(field_name: str, value: Any) -> None:
    """Require a non-empty string, which is the schema's ``type: string, minLength: 1``."""
    if not isinstance(value, str) or not value:
        raise IdentifierValidationError(field_name, "must be a non-empty string")


def _require_nullable_text(field_name: str, value: Any) -> None:
    if value is not None:
        _require_text(field_name, value)


def _require_enum(field_name: str, value: Any, permitted: Sequence[str]) -> None:
    if value not in permitted or not isinstance(value, str):
        raise IdentifierValidationError(field_name, f"must be one of {', '.join(permitted)}")


def _require_nullable_positive_integer(field_name: str, value: Any) -> None:
    """Require the schema's ``type: [integer, null], minimum: 1``.

    JSON Schema calls a number with no fractional part an integer and a boolean neither, so
    ``1.0`` is accepted here and ``True`` is not, which is what the conformance test measures
    this validator against.
    """
    if value is None:
        return
    if isinstance(value, bool):
        raise IdentifierValidationError(field_name, "must be an integer or null")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise IdentifierValidationError(field_name, "must be an integer or null")
    if value < 1:
        raise IdentifierValidationError(field_name, "must be at least 1")


def _require_nullable_entity_type(field_name: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _ENTITY_TYPE.fullmatch(value) is None:
        raise IdentifierValidationError(field_name, "must be a string of digits or null")


def _require_array(field_name: str, value: Any, *, unique: bool = False) -> list[Any]:
    """Require a JSON array, optionally with the schema's ``uniqueItems: true``.

    Uniqueness is JSON equality rather than Python identity, so two equal objects written with
    their keys in different orders are one item, exactly as the schema reads them.
    """
    if not isinstance(value, list):
        raise IdentifierValidationError(field_name, "must be an array")
    if unique:
        seen: set[str] = set()
        for entry in value:
            canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            if canonical in seen:
                raise IdentifierValidationError(field_name, "must not repeat an entry")
            seen.add(canonical)
    return value


def validate_identifiers_block(block: Mapping[str, Any]) -> None:
    """Validate one canonical identifiers block against ADR 0011's published contract.

    Args:
        block: The decoded block, as a producer attaches it to a releases event and as every
            release-shaped store carries it.

    Raises:
        IdentifierValidationError: On the first field that fails, naming its path in the block.
    """
    contracts = _identifier_contracts()
    document = _require_object("", block, contracts["block"])

    _require_const("identifiers_version", document["identifiers_version"], _IDENTIFIERS_VERSION)

    for index, item in enumerate(_require_array("items", document["items"])):
        path = f"items[{index}]"
        entry = _require_object(path, item, contracts["item"])
        _require_enum(f"{path}.type", entry["type"], identifier_types())
        _require_text(f"{path}.value", entry["value"])
        _require_nullable_text(f"{path}.description", entry["description"])

        source_path = f"{path}.source"
        source = _require_object(source_path, entry["source"], contracts["source"])
        _require_enum(f"{source_path}.provider", source["provider"], _identifier_source_providers())
        _require_nullable_text(f"{source_path}.type", source["type"])
        _require_enum(f"{source_path}.field", source["field"], _source_fields())

    for index, value in enumerate(_require_array("types", document["types"], unique=True)):
        _require_enum(f"types[{index}]", value, identifier_types())

    for index, alias in enumerate(_require_array("aliases", document["aliases"], unique=True)):
        path = f"aliases[{index}]"
        entry = _require_object(path, alias, contracts["alias"])
        _require_enum(f"{path}.provider", entry["provider"], _alias_providers())
        _require_text(f"{path}.external_id", entry["external_id"])

    unmapped = _require_object("unmapped", document["unmapped"], contracts["unmapped"])
    for index, value in enumerate(_require_array("unmapped.types", unmapped["types"], unique=True)):
        _require_text(f"unmapped.types[{index}]", value)


def validate_companies_block(block: Mapping[str, Any]) -> None:
    """Validate one canonical companies block against ADR 0011's published contract.

    Args:
        block: The decoded block, as a producer attaches it to a releases event and as the graph
            projects it into ``CREDITED_TO`` edges.

    Raises:
        IdentifierValidationError: On the first field that fails, naming its path in the block.
    """
    contracts = _company_contracts()
    document = _require_object("", block, contracts["block"])

    _require_const("companies_version", document["companies_version"], _COMPANIES_VERSION)

    for index, item in enumerate(_require_array("items", document["items"])):
        path = f"items[{index}]"
        entry = _require_object(path, item, contracts["item"])
        _require_text(f"{path}.name", entry["name"])
        _require_nullable_positive_integer(f"{path}.discogs_id", entry["discogs_id"])
        _require_text(f"{path}.role", entry["role"])
        _require_enum(f"{path}.role_category", entry["role_category"], company_role_categories())
        _require_nullable_text(f"{path}.catno", entry["catno"])

        source_path = f"{path}.source"
        source = _require_object(source_path, entry["source"], contracts["source"])
        _require_const(f"{source_path}.provider", source["provider"], _COMPANY_SOURCE_PROVIDER)
        _require_nullable_entity_type(f"{source_path}.entity_type", source["entity_type"])

    for index, value in enumerate(_require_array("role_categories", document["role_categories"], unique=True)):
        _require_enum(f"role_categories[{index}]", value, company_role_categories())

    unmapped = _require_object("unmapped", document["unmapped"], contracts["unmapped"])
    for index, value in enumerate(_require_array("unmapped.roles", unmapped["roles"], unique=True)):
        _require_text(f"unmapped.roles[{index}]", value)


def alias_refs_for_release(block: Mapping[str, Any], entity_kind: str = _RELEASE) -> list[AliasRef]:
    """Return the provider aliases an identifiers block mints, ready for ``attach_aliases``.

    One ref per alias-bearing identifier: the provider is the namespace the type mints into, and
    the ``external_id`` is the item's value under that namespace's normalization. Two items that
    normalize to the same value are one ref, which is how the same barcode printed with grouping
    spaces and the same catalogue number written in two cases stay two items on the block and
    become one alias each. Refs keep the block's item order, and a value that normalizes away to
    nothing is skipped rather than turned into an empty alias key.

    The block is validated first, so a caller cannot mint aliases out of a malformed block.

    Args:
        block: A canonical identifiers block.
        entity_kind: The entity kind the aliases key against. Defaults to ``"release"``, the
            only kind these blocks are attached to today.

    Returns:
        The de-duplicated refs, in block order. Pass them to
        :func:`common.identity.attach_aliases` with the native id the release resolved to.

    Raises:
        IdentifierValidationError: If the block is not a valid identifiers block.
        ValueError: If ``entity_kind`` is outside the identity vocabulary's closed set.
    """
    validate_identifiers_block(block)

    namespaces = _alias_namespaces()
    refs: dict[AliasRef, None] = {}
    for item in block["items"]:
        namespace = namespaces.get(item["type"])
        if namespace is None:
            continue
        provider, normalization = namespace
        external_id = _NORMALIZERS[normalization](item["value"])
        if not external_id:
            continue
        refs[AliasRef(provider, entity_kind, external_id)] = None
    return list(refs)
