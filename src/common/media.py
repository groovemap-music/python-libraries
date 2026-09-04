"""Canonical media block mappers for the vendored GrooveMap media taxonomy.

ADR 0007 in the ``design`` repository makes one JSON vocabulary authoritative for every
GrooveMap service and requires every mapper — the two Rust producers and this one — to
produce byte-identical blocks for the same input. This module is the Python mapper. It is a
line-for-line port of the reference mapper the design repository proves its conformance
fixtures against, so the behaviour that looks arbitrary here (last-value-wins for a format
entry's container, first-value-wins for a description's, quantities parsed with JavaScript
``parseInt`` semantics) is deliberate and fixture-locked.

The vocabulary is read from ``common.media_taxonomy`` package data with the standard library
only; nothing here imports a third-party package, so the base ``groovemap-runtime`` install
stays dependency-light.

Ordering is deterministic: ``items`` follow source order, ``source.descriptions`` are kept as
received, and every other list is sorted and de-duplicated. Every field is always present,
holding ``None`` or an empty list when unknown.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from functools import cache
from importlib.resources import files
from typing import Any, Final


__all__ = [
    "families_of",
    "family_ids",
    "legacy_format_names_to_media",
    "map_discogs_formats",
    "map_musicbrainz_release",
    "medium_ids",
    "medium_label",
]

_TAXONOMY_PACKAGE: Final = "common.media_taxonomy"
_TAXONOMY_RESOURCE: Final = "media-taxonomy.json"

# A Discogs description routes to exactly one target. These three tables name the targets that
# are medium attributes, item lists, and release lists; the remaining scalar targets are
# first-value-wins on the block.
_ITEM_ATTRIBUTES: Final[frozenset[str]] = frozenset({"channels", "codec", "size_inches", "speed_rpm"})
_ITEM_LISTS: Final[dict[str, str]] = {"appearance": "appearance", "variant": "variants"}
_RELEASE_LISTS: Final[dict[str, str]] = {"edition": "edition", "flag": "flags", "trait": "traits"}
_BLOCK_SCALARS: Final[frozenset[str]] = frozenset({"container", "packaging", "release_kind"})

# JavaScript's Number.parseInt reads an optional sign and the longest run of leading ASCII
# digits, ignoring the rest; anything else is NaN. \d is avoided because it also matches
# non-ASCII digits in Python.
_LEADING_INTEGER: Final = re.compile(r"\s*([+-]?[0-9]+)")

_MISSING: Final = object()


@cache
def _taxonomy() -> dict[str, Any]:
    """Return the vendored vocabulary, parsed once per process."""
    document = (files(_TAXONOMY_PACKAGE) / _TAXONOMY_RESOURCE).read_text(encoding="utf-8")
    taxonomy: dict[str, Any] = json.loads(document)
    return taxonomy


@cache
def _families_by_id() -> dict[str, dict[str, Any]]:
    return {family["id"]: family for family in _taxonomy()["families"]}


@cache
def _media_by_id() -> dict[str, dict[str, Any]]:
    return {medium["id"]: medium for medium in _taxonomy()["media"]}


def _sorted_unique(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


def _javascript_string(value: object) -> str:
    """Render a value the way JavaScript's ``String()`` would.

    Family resolution keys its map on the string form of a numeric attribute, so a Python
    ``float`` that happens to be integral has to render as ``12`` rather than ``12.0`` for the
    lookup to agree with the reference mapper.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return str(value)


def _parse_quantity(value: object) -> int:
    """Parse a Discogs ``qty`` the way the reference mapper does, defaulting to ``1``."""
    match = _LEADING_INTEGER.match(_javascript_string("1" if value is None else value))
    if match is None:
        return 1
    quantity = int(match.group(1))
    return quantity if quantity >= 1 else 1


def _as_integer(value: object) -> int | None:
    """Return an integral number as an ``int``, and anything else as ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _empty_block() -> dict[str, Any]:
    return {
        "taxonomy_version": _taxonomy()["taxonomy_version"],
        "items": [],
        "families": [],
        "release_kind": None,
        "traits": [],
        "edition": [],
        "packaging": None,
        "container": None,
        "flags": [],
        "unmapped": {"formats": [], "descriptions": []},
    }


def _new_item(provider: str, name: str | None, descriptions: list[str], text: str | None) -> dict[str, Any]:
    return {
        "family": None,
        "medium": None,
        "qty": 1,
        "size_inches": None,
        "speed_rpm": None,
        "channels": None,
        "codec": None,
        "variants": [],
        "appearance": [],
        "position": None,
        "track_count": None,
        "source": {"provider": provider, "name": name, "descriptions": descriptions, "text": text},
    }


def _apply_format_entry(block: dict[str, Any], entry: Mapping[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    """Apply one vocabulary format entry, returning the item it produced or ``None``.

    A format name that carries only a release fact (Discogs ``Box Set`` sets the container,
    ``All Media`` adds a flag) contributes to the block and produces no item.
    """
    if entry.get("container"):
        block["container"] = entry["container"]
    if entry.get("flag"):
        block["flags"].append(entry["flag"])
    if not entry.get("family") and not entry.get("medium"):
        return None
    if entry.get("medium"):
        item["medium"] = entry["medium"]
    if entry.get("family"):
        item["family"] = entry["family"]
    if entry.get("variant"):
        item["variants"].append(entry["variant"])
    return item


def _finish_item(item: dict[str, Any]) -> dict[str, Any]:
    """Resolve an item's family and medium, apply medium defaults, and order its lists."""
    media = _media_by_id()
    if item["medium"] and not item["family"]:
        item["family"] = media[item["medium"]]["family"]
    if not item["medium"]:
        resolve = _families_by_id()[item["family"]].get("resolve")
        resolved = resolve["map"].get(_javascript_string(item[resolve["attribute"]])) if resolve else None
        item["medium"] = resolved if resolved is not None else f"{item['family']}_unspecified"
    for attribute, value in (media[item["medium"]].get("defaults") or {}).items():
        if item[attribute] is None:
            item[attribute] = value
    item["variants"] = _sorted_unique(item["variants"])
    item["appearance"] = _sorted_unique(item["appearance"])
    return item


def _finish_block(block: dict[str, Any]) -> dict[str, Any]:
    block["families"] = _sorted_unique(item["family"] for item in block["items"])
    for key in ("traits", "edition", "flags"):
        block[key] = _sorted_unique(block[key])
    block["unmapped"]["formats"] = _sorted_unique(block["unmapped"]["formats"])
    block["unmapped"]["descriptions"] = _sorted_unique(block["unmapped"]["descriptions"])
    return block


def flatten_descriptions(descriptions: object) -> list[str]:
    """Flatten either Discogs description shape into a list of strings.

    The normalized event shape nests the list under a ``description`` key and collapses a
    single entry to a bare string; the Discogs API shape is already a flat list. Both are
    accepted, and anything else flattens to an empty list.
    """
    if descriptions is None:
        return []
    if isinstance(descriptions, str):
        return [descriptions]
    if isinstance(descriptions, list):
        return [value for value in descriptions if isinstance(value, str)]
    if isinstance(descriptions, Mapping) and "description" in descriptions:
        return flatten_descriptions(descriptions["description"])
    return []


def map_discogs_formats(formats: object) -> dict[str, Any]:
    """Map a Discogs ``formats`` list onto the canonical media block.

    Both provider shapes are accepted: the normalized releases-event shape, whose descriptions
    arrive as ``{"description": [...]}`` (or a bare string for a single entry), and the Discogs
    API shape, whose ``descriptions`` is already a flat list.

    Args:
        formats: The raw ``formats`` list. A non-list, or an element that is not a mapping, is
            skipped rather than raising.

    Returns:
        A JSON-ready media block: plain dicts and lists, every field present.
    """
    taxonomy = _taxonomy()
    known_formats: Mapping[str, Any] = taxonomy["discogs"]["formats"]
    known_descriptions: Mapping[str, Any] = taxonomy["discogs"]["descriptions"]
    block = _empty_block()

    for raw_format in formats if isinstance(formats, list) else []:
        if not isinstance(raw_format, Mapping):
            continue
        name = _text_or_none(raw_format.get("name"))
        descriptions = flatten_descriptions(raw_format.get("descriptions"))
        text = _text_or_none(raw_format.get("text"))
        entry = None if name is None else known_formats.get(name)
        item: dict[str, Any] | None = None

        if entry is None:
            if name is not None:
                block["unmapped"]["formats"].append(name)
        else:
            item = _apply_format_entry(block, entry, _new_item("discogs", name, descriptions, text))
            if item is not None:
                item["qty"] = _parse_quantity(raw_format.get("qty"))

        for description in descriptions:
            rule = known_descriptions.get(description)
            if rule is None:
                block["unmapped"]["descriptions"].append(description)
                continue
            target = rule["target"]
            if target == "ignore":
                continue
            value = rule.get("value")
            if target in _ITEM_ATTRIBUTES:
                if item is not None and item[target] is None:
                    item[target] = value
            elif target in _ITEM_LISTS:
                if item is not None:
                    item[_ITEM_LISTS[target]].append(value)
            elif target in _RELEASE_LISTS:
                block[_RELEASE_LISTS[target]].append(value)
            elif target in _BLOCK_SCALARS and block[target] is None:
                block[target] = value

        if item is not None:
            block["items"].append(_finish_item(item))

    return _finish_block(block)


def map_musicbrainz_release(release: object) -> dict[str, Any]:
    """Map a MusicBrainz release onto the canonical media block.

    Args:
        release: A mapping holding ``media`` (entries with ``format``, ``position``, ``title``,
            and ``track_count``), ``status``, ``packaging``, and ``release_group`` with
            ``primary_type`` and ``secondary_types``. Every key is optional, and a value of an
            unexpected type is skipped rather than raising.

    Returns:
        A JSON-ready media block: plain dicts and lists, every field present.
    """
    vocabulary: Mapping[str, Any] = _taxonomy()["musicbrainz"]
    source: Mapping[str, Any] = release if isinstance(release, Mapping) else {}
    block = _empty_block()

    raw_media = source.get("media")
    for medium in raw_media if isinstance(raw_media, list) else []:
        if not isinstance(medium, Mapping):
            continue
        raw_name = medium.get("format")
        name = raw_name if isinstance(raw_name, str) and raw_name != "" else None
        item = _new_item("musicbrainz", name, [], None)
        item["position"] = _as_integer(medium.get("position"))
        item["track_count"] = _as_integer(medium.get("track_count"))

        if name is None:
            item["medium"] = "other_unspecified"
        else:
            entry = vocabulary["formats"].get(name)
            if entry is None:
                block["unmapped"]["formats"].append(name)
                continue
            if _apply_format_entry(block, entry, item) is None:
                # No MusicBrainz format entry is release-fact-only in this vocabulary version.
                # The guard mirrors the reference mapper so a vocabulary that adds one behaves
                # identically here without a code change.
                continue  # pragma: no cover

        block["items"].append(_finish_item(item))

    status = source.get("status")
    if isinstance(status, str):
        edition = vocabulary["status"].get(status, _MISSING)
        if edition is _MISSING:
            block["unmapped"]["descriptions"].append(status)
        elif edition is not None:
            block["edition"].append(edition)

    packaging = source.get("packaging")
    if isinstance(packaging, str):
        mapped_packaging = vocabulary["packaging"].get(packaging, _MISSING)
        if mapped_packaging is _MISSING:
            block["unmapped"]["descriptions"].append(packaging)
        else:
            block["packaging"] = mapped_packaging

    raw_group = source.get("release_group")
    group: Mapping[str, Any] = raw_group if isinstance(raw_group, Mapping) else {}

    primary_type = group.get("primary_type")
    if isinstance(primary_type, str):
        release_kind = vocabulary["primary_types"].get(primary_type, _MISSING)
        if release_kind is _MISSING:
            block["unmapped"]["descriptions"].append(primary_type)
        else:
            block["release_kind"] = release_kind

    secondary_types = group.get("secondary_types")
    for secondary in secondary_types if isinstance(secondary_types, list) else []:
        if not isinstance(secondary, str):
            continue
        trait = vocabulary["secondary_types"].get(secondary, _MISSING)
        if trait is _MISSING:
            block["unmapped"]["descriptions"].append(secondary)
        else:
            block["traits"].append(trait)

    return _finish_block(block)


def legacy_format_names_to_media(names: Iterable[object]) -> dict[str, Any]:
    """Derive a best-effort media block from a flat list of raw Discogs format names.

    ADR 0007 keeps this path for events and stored records that predate the canonical block,
    where the provider's structure has already been flattened to one list mixing format names
    with their descriptions, for example ``["Vinyl", "LP", "Album"]``.

    The rule that recovers the structure: a name the vocabulary knows as a Discogs *format*
    name opens a new format entry, and every other name becomes a description of the format
    entry that precedes it. Names that appear before any format name attach to the first format
    entry, ahead of its own descriptions, so ``["Album", "Vinyl", "LP"]`` and
    ``["Vinyl", "Album", "LP"]`` agree. A list holding no format name at all still yields the
    release-level facts its descriptions carry, and contributes no item.

    Because the flattened list has lost which description belonged to which medium, a
    multi-format release is only recovered as well as its ordering allows. Prefer
    :func:`map_discogs_formats` on the raw provider structure whenever it is available.

    Args:
        names: The flattened format names, in the order the record carries them. A non-string
            entry is skipped.

    Returns:
        A JSON-ready media block: plain dicts and lists, every field present.
    """
    known_formats: Mapping[str, Any] = _taxonomy()["discogs"]["formats"]
    formats: list[dict[str, Any]] = []
    leading: list[str] = []

    for name in names:
        if not isinstance(name, str):
            continue
        if name in known_formats:
            formats.append({"name": name, "descriptions": []})
        elif formats:
            formats[-1]["descriptions"].append(name)
        else:
            leading.append(name)

    if leading:
        if formats:
            formats[0]["descriptions"] = [*leading, *formats[0]["descriptions"]]
        else:
            formats.append({"name": None, "descriptions": leading})

    return map_discogs_formats(formats)


def families_of(media_block: Mapping[str, Any] | None) -> list[str]:
    """Return the sorted, unique family ids a media block covers.

    The block's own ``families`` list is used when present; otherwise the families are read
    back off ``items``, so a partially built block still answers. An empty or missing block
    answers with an empty list.
    """
    if not media_block:
        return []
    families = media_block.get("families")
    if isinstance(families, list):
        return _sorted_unique(family for family in families if isinstance(family, str))
    items = media_block.get("items")
    if not isinstance(items, list):
        return []
    return _sorted_unique(item["family"] for item in items if isinstance(item, Mapping) and isinstance(item.get("family"), str))


def family_ids() -> list[str]:
    """Return every family id in the vocabulary, in the order the vocabulary declares them."""
    return [family["id"] for family in _taxonomy()["families"]]


def medium_ids() -> list[str]:
    """Return every medium id in the vocabulary, in the order the vocabulary declares them."""
    return [medium["id"] for medium in _taxonomy()["media"]]


def medium_label(medium_id: str) -> str:
    """Return a medium's human-readable label.

    Raises:
        KeyError: If the vocabulary holds no such medium id.
    """
    label: str = _media_by_id()[medium_id]["label"]
    return label
