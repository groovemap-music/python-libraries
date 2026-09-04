"""Conformance and behaviour tests for common.media.

The conformance suite is the contract that matters: ADR 0007 requires this mapper and the two
Rust producers to agree byte for byte, and the fixtures under tests/fixtures/media are the
design repository's own input/expected pairs, copied verbatim from taxonomy/media/v1/fixtures
at the vendored vocabulary's commit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from common.media import (
    _as_integer,
    _javascript_string,
    _parse_quantity,
    families_of,
    family_ids,
    flatten_descriptions,
    legacy_format_names_to_media,
    map_discogs_formats,
    map_musicbrainz_release,
    medium_ids,
    medium_label,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "media"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_the_full_conformance_suite_was_vendored() -> None:
    """A missing fixture would silently weaken the suite below, so count them explicitly."""
    assert len(FIXTURES) == 19


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.stem)
def test_mapper_matches_the_design_conformance_fixture(fixture_path: Path) -> None:
    fixture = _load(fixture_path)
    provider = fixture["provider"]

    if provider == "discogs":
        # The reference mapper reads fixture.input.formats, which is absent (undefined) in
        # the no-formats fixture and maps to an empty block rather than an error.
        actual = map_discogs_formats(fixture["input"].get("formats"))
    elif provider == "musicbrainz":
        actual = map_musicbrainz_release(fixture["input"])
    else:  # pragma: no cover - a new provider must be wired here deliberately
        pytest.fail(f"unknown fixture provider: {provider}")

    assert actual == fixture["expected"]


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.stem)
def test_mapper_output_is_json_serialisable(fixture_path: Path) -> None:
    """The block travels in events and JSONB columns, so it must hold only plain JSON types."""
    fixture = _load(fixture_path)
    actual = map_discogs_formats(fixture["input"].get("formats")) if fixture["provider"] == "discogs" else map_musicbrainz_release(fixture["input"])

    assert json.loads(json.dumps(actual)) == actual


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ("Album", ["Album"]),
        (["LP", "Album"], ["LP", "Album"]),
        (["LP", 7, None, "Album"], ["LP", "Album"]),
        ({"description": "Album"}, ["Album"]),
        ({"description": ["LP", "Album"]}, ["LP", "Album"]),
        ({"description": {"description": ["LP"]}}, ["LP"]),
        ({"other": ["LP"]}, []),
        (17, []),
    ],
)
def test_flatten_descriptions_accepts_both_provider_shapes(raw: object, expected: list[str]) -> None:
    assert flatten_descriptions(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("2", 2), (None, 1), ("", 1), ("abc", 1), ("0", 1), ("-3", 1), (3, 3), ("2 x LP", 2), ("  4  ", 4), (2.9, 2)],
)
def test_quantity_parsing_follows_the_reference_mapper(raw: object, expected: int) -> None:
    assert _parse_quantity(raw) == expected


@pytest.mark.parametrize("raw,expected", [(None, "null"), (True, "true"), (False, "false"), (12.0, "12"), (33.33, "33.33"), (7, "7"), ("7", "7")])
def test_javascript_string_rendering_matches_the_reference_mapper(raw: object, expected: str) -> None:
    """Family resolution keys on this rendering, so 12.0 has to become "12", not "12.0"."""
    assert _javascript_string(raw) == expected


@pytest.mark.parametrize("raw,expected", [(3, 3), (3.0, 3), (3.5, None), (True, None), ("3", None), (None, None)])
def test_integer_coercion_rejects_non_integral_values(raw: object, expected: int | None) -> None:
    assert _as_integer(raw) == expected


def test_discogs_mapping_tolerates_a_malformed_formats_list() -> None:
    assert map_discogs_formats(None)["items"] == []
    assert map_discogs_formats("Vinyl")["items"] == []

    block = map_discogs_formats([None, "Vinyl", 7, {"qty": "1"}])
    assert block["items"] == []
    assert block["unmapped"] == {"formats": [], "descriptions": []}


def test_discogs_descriptions_apply_without_a_mapped_format_entry() -> None:
    """Box Set is a release fact, not a medium: its descriptions still reach the block."""
    block = map_discogs_formats([{"name": "Box Set", "qty": "1", "descriptions": ["Compilation", "Limited Edition"]}])

    assert block["items"] == []
    assert block["container"] == "box_set"
    assert block["traits"] == ["compilation"]
    assert block["edition"] == ["limited"]


def test_first_value_wins_for_scalar_release_facts() -> None:
    block = map_discogs_formats([{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album", "Single", "Gatefold", "Digipak"]}])

    assert block["release_kind"] == "album"
    assert block["packaging"] == "gatefold"


def test_first_value_wins_for_item_attributes_and_lists_stay_sorted() -> None:
    block = map_discogs_formats([{"name": "Vinyl", "qty": "1", "descriptions": ['12"', '7"', "Picture Disc", "Etched", "Picture Disc"]}])
    item = block["items"][0]

    assert item["size_inches"] == 12
    assert item["appearance"] == ["etched", "picture_disc"]


def test_unmapped_values_are_preserved_sorted_and_deduplicated() -> None:
    block = map_discogs_formats(
        [
            {"name": "Wax Cylinder Deluxe", "qty": "1", "descriptions": ["Zither Mix"]},
            {"name": "Vinyl", "qty": "1", "descriptions": ["Zither Mix", "Aardvark Cut"]},
        ]
    )

    assert block["unmapped"] == {"formats": ["Wax Cylinder Deluxe"], "descriptions": ["Aardvark Cut", "Zither Mix"]}


def test_musicbrainz_mapping_tolerates_a_malformed_release() -> None:
    empty = map_musicbrainz_release(None)

    assert empty["items"] == []
    assert empty["release_kind"] is None
    assert map_musicbrainz_release({"media": "CD"})["items"] == []
    assert map_musicbrainz_release({"media": [None, 7]})["items"] == []


def test_musicbrainz_medium_without_a_format_falls_back_to_other() -> None:
    block = map_musicbrainz_release({"media": [{"format": "", "position": 1.0, "track_count": "many"}]})
    item = block["items"][0]

    assert item["medium"] == "other_unspecified"
    assert item["family"] == "other"
    assert item["position"] == 1
    assert item["track_count"] is None


def test_musicbrainz_unknown_values_land_in_unmapped() -> None:
    block = map_musicbrainz_release(
        {
            "media": [],
            "status": "Rumoured",
            "packaging": "Hessian Sack",
            "release_group": {"primary_type": "Fanzine", "secondary_types": ["Bootleg-ish", 7]},
        }
    )

    assert block["unmapped"]["descriptions"] == ["Bootleg-ish", "Fanzine", "Hessian Sack", "Rumoured"]
    assert block["release_kind"] is None
    assert block["packaging"] is None


def test_musicbrainz_release_group_of_the_wrong_shape_is_ignored() -> None:
    block = map_musicbrainz_release({"media": [], "release_group": "Album", "status": 7, "packaging": 7})

    assert block["release_kind"] is None
    assert block["unmapped"]["descriptions"] == []


def test_legacy_names_recover_a_format_entry_and_its_descriptions() -> None:
    block = legacy_format_names_to_media(["Vinyl", "LP", "Album"])
    item = block["items"][0]

    assert item["medium"] == "vinyl_12"
    assert item["size_inches"] == 12
    assert item["source"]["descriptions"] == ["LP", "Album"]
    assert block["release_kind"] == "album"


def test_legacy_names_attach_leading_descriptions_to_the_first_format_entry() -> None:
    leading = legacy_format_names_to_media(["Album", "Vinyl", "LP"])
    trailing = legacy_format_names_to_media(["Vinyl", "Album", "LP"])

    assert leading["items"][0]["source"]["descriptions"] == ["Album", "LP"]
    assert leading["items"] == trailing["items"]
    assert leading == trailing


def test_legacy_names_split_a_multi_format_release_on_each_format_name() -> None:
    block = legacy_format_names_to_media(["Vinyl", "LP", "CD", "Album", 7])

    assert [item["medium"] for item in block["items"]] == ["vinyl_12", "optical_cd"]
    assert block["items"][0]["source"]["descriptions"] == ["LP"]
    assert block["items"][1]["source"]["descriptions"] == ["Album"]
    assert block["families"] == ["optical", "vinyl"]


def test_legacy_names_without_a_format_name_still_yield_release_facts() -> None:
    block = legacy_format_names_to_media(["Album", "Reissue"])

    assert block["items"] == []
    assert block["release_kind"] == "album"
    assert block["edition"] == ["reissue"]
    assert block["unmapped"] == {"formats": [], "descriptions": []}


def test_legacy_names_of_an_empty_list_produce_an_empty_block() -> None:
    assert legacy_format_names_to_media([]) == map_discogs_formats([])


def test_families_of_reads_the_block_families_list() -> None:
    block = map_discogs_formats([{"name": "Vinyl", "qty": "1", "descriptions": ["LP"]}, {"name": "CD", "qty": "1", "descriptions": []}])

    assert families_of(block) == ["optical", "vinyl"]


@pytest.mark.parametrize("block", [None, {}, {"items": "none"}])
def test_families_of_an_absent_or_malformed_block_is_empty(block: dict[str, Any] | None) -> None:
    assert families_of(block) == []


def test_families_of_falls_back_to_the_item_families() -> None:
    partial = {"items": [{"family": "vinyl"}, {"family": "optical"}, {"family": "vinyl"}, {"family": 7}, "not a mapping"]}

    assert families_of(partial) == ["optical", "vinyl"]


def test_vocabulary_id_helpers_expose_the_closed_sets() -> None:
    families = family_ids()
    media = medium_ids()

    assert families == ["vinyl", "shellac", "grooved_other", "tape", "optical", "digital", "video", "other"]
    assert len(media) == len(set(media))
    assert all(f"{family}_unspecified" in media for family in families)


def test_medium_label_resolves_a_known_id_and_rejects_an_unknown_one() -> None:
    assert medium_label("vinyl_12") == '12" vinyl'

    with pytest.raises(KeyError):
        medium_label("vinyl_13")


def test_typed_media_block_matches_what_the_mapper_returns() -> None:
    """The agent-tools types describe the mapper's output, so drift between them must fail."""
    from common.agent_tools.schemas import MediaBlock, MediaItem, MediaSource, MediaUnmapped

    block = map_discogs_formats([{"name": "Vinyl", "qty": "1", "descriptions": ["LP", "Album"]}])

    assert set(MediaBlock.__annotations__) == set(block)
    assert set(MediaItem.__annotations__) == set(block["items"][0])
    assert set(MediaSource.__annotations__) == set(block["items"][0]["source"])
    assert set(MediaUnmapped.__annotations__) == set(block["unmapped"])
