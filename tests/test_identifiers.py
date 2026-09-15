"""Tests for the canonical identifier and company blocks.

The conformance test is the load-bearing one: it validates every vendored design fixture, and a
generated mutation of each, against the vendored JSON Schemas with `jsonschema` — a development
dependency the runtime never ships — and asserts the standard-library validators in
`common.identifiers` return the same verdict every time, so the Python contract cannot drift
from the published schema without a failing test here.
"""

from __future__ import annotations

import json
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from common.identifiers import (
    IdentifierValidationError,
    alias_identifier_types,
    alias_refs_for_release,
    company_role_categories,
    identifier_types,
    validate_companies_block,
    validate_identifiers_block,
)
from common.identity import AliasRef, providers


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
IDENTIFIER_VOCABULARY_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "common" / "identifier_vocabulary"
COMPANY_VOCABULARY_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "common" / "company_role_vocabulary"

IDENTIFIERS = "identifiers"
COMPANIES = "companies"


def fixtures(directory: str) -> list[Path]:
    """Return every conformance fixture the design repository publishes, source record aside."""
    return sorted(path for path in (FIXTURE_ROOT / directory).glob("*.json") if path.name != "source.json")


IDENTIFIER_FIXTURES = fixtures("identifiers")
COMPANY_FIXTURES = fixtures("company-roles")


def document(directory: str, name: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads((FIXTURE_ROOT / directory / f"{name}.json").read_text())
    return parsed


def expected_block(directory: str, name: str) -> dict[str, Any]:
    block: dict[str, Any] = document(directory, name)["expected"]
    return block


def schema_for(kind: str) -> dict[str, Any]:
    directory = IDENTIFIER_VOCABULARY_DIRECTORY if kind == IDENTIFIERS else COMPANY_VOCABULARY_DIRECTORY
    resource = "identifier-block.schema.json" if kind == IDENTIFIERS else "company-block.schema.json"
    parsed: dict[str, Any] = json.loads((directory / resource).read_text())
    return parsed


def vocabulary_for(kind: str) -> dict[str, Any]:
    directory = IDENTIFIER_VOCABULARY_DIRECTORY if kind == IDENTIFIERS else COMPANY_VOCABULARY_DIRECTORY
    resource = "identifier-types.json" if kind == IDENTIFIERS else "company-roles.json"
    parsed: dict[str, Any] = json.loads((directory / resource).read_text())
    return parsed


@cache
def validator_for(kind: str) -> Draft202012Validator:
    return Draft202012Validator(schema_for(kind), format_checker=Draft202012Validator.FORMAT_CHECKER)


def stdlib_verdict(kind: str, block: Any) -> bool:
    validate = validate_identifiers_block if kind == IDENTIFIERS else validate_companies_block
    try:
        validate(block)
    except IdentifierValidationError:
        return False
    return True


def kind_of(fixture: Path) -> str:
    return IDENTIFIERS if fixture.parent.name == "identifiers" else COMPANIES


def assert_validators_agree(kind: str, block: Any, case: str) -> None:
    """Assert the vendored schema and the standard-library validator reach the same verdict."""
    schema_valid = validator_for(kind).is_valid(block)
    assert stdlib_verdict(kind, block) is schema_valid, f"{case}: the standard-library validator disagrees with the vendored schema"


def mutations(block: dict[str, Any]) -> list[tuple[str, Any]]:
    """Return one mutated block per structural rule the published schema carries.

    The mutations are deliberately a mix of invalid and still-valid ones. The conformance test
    asserts only that both validators reach the same verdict, so a mutation that the schema
    happens to accept is as useful as one it rejects.
    """
    cases: list[tuple[str, Any]] = [("not-an-object", ["identifiers"]), ("empty-object", {}), ("extra-key", {**block, "unexpected": 1})]

    for key, value in block.items():
        cases.append((f"drop-{key}", {name: held for name, held in block.items() if name != key}))
        cases.append((f"null-{key}", {**block, key: None}))
        if isinstance(value, str):
            cases.append((f"renumber-{key}", {**block, key: "2"}))
            cases.append((f"empty-{key}", {**block, key: ""}))
        if isinstance(value, list):
            cases.append((f"scalar-{key}", {**block, key: "not an array"}))
            if value:
                cases.append((f"repeat-{key}", {**block, key: [*value, value[0]]}))
                cases.append((f"unknown-{key}", {**block, key: [*value, "crimped"]}))
                cases.append((f"empty-entry-{key}", {**block, key: [*value, ""]}))

    for name, holder in (("unmapped", block["unmapped"]),):
        for key, value in holder.items():
            cases.append((f"drop-{name}.{key}", {**block, name: {held: kept for held, kept in holder.items() if held != key}}))
            cases.append((f"repeat-{name}.{key}", {**block, name: {**holder, key: [*value, *value[:1]]}}))
            cases.append((f"empty-entry-{name}.{key}", {**block, name: {**holder, key: [*value, ""]}}))
        cases.append((f"extra-{name}", {**block, name: {**holder, "unexpected": []}}))

    if block["items"]:
        cases.extend(item_mutations(block))
    return cases


def item_mutations(block: dict[str, Any]) -> list[tuple[str, Any]]:
    """Return one mutated block per rule the schema puts on the first item and its source."""
    cases: list[tuple[str, Any]] = []
    item = block["items"][0]

    def with_first(replacement: Any) -> dict[str, Any]:
        mutated = deepcopy(block)
        mutated["items"][0] = replacement
        return mutated

    cases.append(("item-not-an-object", with_first("barcode")))
    cases.append(("item-extra-key", with_first({**item, "unexpected": 1})))

    for key, value in item.items():
        cases.append((f"item-drop-{key}", with_first({name: held for name, held in item.items() if name != key})))
        cases.append((f"item-null-{key}", with_first({**item, key: None})))
        cases.append((f"item-empty-{key}", with_first({**item, key: ""})))
        cases.append((f"item-unknown-{key}", with_first({**item, key: "crimped"})))
        cases.append((f"item-number-{key}", with_first({**item, key: 7})))
        cases.append((f"item-float-{key}", with_first({**item, key: 1.0})))
        cases.append((f"item-zero-{key}", with_first({**item, key: 0})))
        cases.append((f"item-boolean-{key}", with_first({**item, key: True})))
        if isinstance(value, dict):
            for inner in value:
                cases.append((f"item-{key}-drop-{inner}", with_first({**item, key: {held: kept for held, kept in value.items() if held != inner}})))
                cases.append((f"item-{key}-unknown-{inner}", with_first({**item, key: {**value, inner: "crimped"}})))
                cases.append((f"item-{key}-null-{inner}", with_first({**item, key: {**value, inner: None}})))
            cases.append((f"item-{key}-extra", with_first({**item, key: {**value, "unexpected": 1}})))
    return cases


class TestVendoredVocabularies:
    def test_version_1_carries_exactly_the_seven_published_identifier_types(self) -> None:
        assert len(identifier_types()) == 7
        assert identifier_types()[0] == "barcode"
        assert set(identifier_types()) == {"barcode", "matrix_runout", "label_code", "rights_society", "asin", "other", "catalog_number"}

    def test_version_1_carries_exactly_the_nine_published_role_categories(self) -> None:
        assert len(company_role_categories()) == 9
        assert company_role_categories()[0] == "manufacturing"
        assert "recording_facility" in company_role_categories()

    def test_only_the_three_alias_bearing_types_mint_a_provider_alias(self) -> None:
        assert alias_identifier_types() == ("barcode", "catalog_number", "matrix_runout")
        assert set(alias_identifier_types()) < set(identifier_types())

    def test_each_alias_namespace_names_a_provider_the_identity_vocabulary_carries(self) -> None:
        namespaces = vocabulary_for(IDENTIFIERS)["alias_namespaces"]

        assert [namespace["provider"] for namespace in namespaces] == ["barcode", "catalog_number", "matrix"]
        assert all(namespace["provider"] in providers() for namespace in namespaces)
        assert all(namespace["alias_source"] == "catalog" for namespace in namespaces)

    def test_the_block_schemas_close_the_same_sets_the_vocabularies_publish(self) -> None:
        identifier_schema = schema_for(IDENTIFIERS)
        company_schema = schema_for(COMPANIES)

        assert set(identifier_schema["$defs"]["identifierTypeId"]["enum"]) == set(identifier_types())
        assert set(company_schema["$defs"]["roleCategoryId"]["enum"]) == set(company_role_categories())
        assert identifier_schema["properties"]["identifiers_version"]["const"] == "1"
        assert company_schema["properties"]["companies_version"]["const"] == "1"
        assert identifier_schema["properties"]["items"]["items"]["properties"]["source"]["properties"]["provider"]["const"] == "discogs"
        assert company_schema["properties"]["items"]["items"]["properties"]["source"]["properties"]["provider"]["const"] == "discogs"

    def test_the_unmapped_marker_is_the_type_and_category_the_vocabularies_fall_back_to(self) -> None:
        assert vocabulary_for(IDENTIFIERS)["unmapped_type"] in identifier_types()
        assert vocabulary_for(COMPANIES)["unmapped_category"] in company_role_categories()

    def test_every_raw_provider_string_maps_into_the_closed_set(self) -> None:
        assert set(vocabulary_for(IDENTIFIERS)["discogs"]["types"].values()) <= set(identifier_types())
        assert set(vocabulary_for(COMPANIES)["discogs"]["roles"].values()) <= set(company_role_categories())


class TestConformance:
    def test_the_vendored_fixture_set_is_the_published_one(self) -> None:
        assert len(IDENTIFIER_FIXTURES) == 10
        assert len(COMPANY_FIXTURES) == 10
        assert all(json.loads(path.read_text())["provider"] == "discogs" for path in IDENTIFIER_FIXTURES + COMPANY_FIXTURES)

    @pytest.mark.parametrize("fixture", IDENTIFIER_FIXTURES + COMPANY_FIXTURES, ids=lambda path: f"{path.parent.name}/{path.stem}")
    def test_every_published_block_is_valid_under_both_validators(self, fixture: Path) -> None:
        kind = kind_of(fixture)
        block = json.loads(fixture.read_text())["expected"]

        assert validator_for(kind).is_valid(block), "the vendored schema rejects a block the design repository publishes"
        assert stdlib_verdict(kind, block) is True

    @pytest.mark.parametrize("fixture", IDENTIFIER_FIXTURES + COMPANY_FIXTURES, ids=lambda path: f"{path.parent.name}/{path.stem}")
    def test_the_stdlib_validator_agrees_with_the_published_schema_on_every_mutation(self, fixture: Path) -> None:
        kind = kind_of(fixture)
        block = json.loads(fixture.read_text())["expected"]

        for case, mutated in mutations(block):
            assert_validators_agree(kind, mutated, f"{fixture.stem}/{case}")

    @pytest.mark.parametrize("fixture", IDENTIFIER_FIXTURES + COMPANY_FIXTURES, ids=lambda path: f"{path.parent.name}/{path.stem}")
    def test_at_least_one_mutation_of_every_block_is_rejected(self, fixture: Path) -> None:
        kind = kind_of(fixture)
        block = json.loads(fixture.read_text())["expected"]

        assert any(not stdlib_verdict(kind, mutated) for _, mutated in mutations(block))


class TestIdentifierBlockValidation:
    def test_a_non_mapping_block_is_rejected(self) -> None:
        not_a_mapping: Any = ["identifiers"]

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(not_a_mapping)

        assert failure.value.field == ""

    def test_a_missing_field_is_rejected_by_name(self) -> None:
        block = expected_block("identifiers", "discogs-no-identifiers")
        del block["aliases"]

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(block)

        assert failure.value.field == "aliases"

    def test_a_field_outside_the_block_is_rejected_by_name(self) -> None:
        block = expected_block("identifiers", "discogs-no-identifiers")
        block["isrcs"] = []

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(block)

        assert failure.value.field == "isrcs"

    def test_a_future_block_version_is_rejected_rather_than_read_as_version_1(self) -> None:
        block = expected_block("identifiers", "discogs-no-identifiers")
        block["identifiers_version"] = "2"

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(block)

        assert failure.value.field == "identifiers_version"

    def test_an_item_failure_names_its_position_in_the_block(self) -> None:
        block = expected_block("identifiers", "discogs-matrix-runout-inscriptions")
        block["items"][1]["value"] = ""

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(block)

        assert failure.value.field == "items[1].value"

    def test_a_source_failure_names_the_nested_field(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")
        block["items"][0]["source"]["field"] = "notes"

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(block)

        assert failure.value.field == "items[0].source.field"

    def test_a_repeated_alias_is_rejected_because_the_schema_requires_unique_entries(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")
        block["aliases"].append(dict(block["aliases"][0]))

        with pytest.raises(IdentifierValidationError) as failure:
            validate_identifiers_block(block)

        assert failure.value.field == "aliases"

    def test_a_repeated_alias_written_with_reordered_keys_is_still_a_repeat(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")
        first = block["aliases"][0]
        block["aliases"].append({"external_id": first["external_id"], "provider": first["provider"]})

        with pytest.raises(IdentifierValidationError):
            validate_identifiers_block(block)

    def test_a_null_item_description_is_accepted_because_most_identifiers_carry_none(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")

        assert block["items"][0]["description"] is None
        validate_identifiers_block(block)


class TestCompaniesBlockValidation:
    def test_a_missing_field_is_rejected_by_name(self) -> None:
        block = expected_block("company-roles", "discogs-no-companies")
        del block["role_categories"]

        with pytest.raises(IdentifierValidationError) as failure:
            validate_companies_block(block)

        assert failure.value.field == "role_categories"

    def test_an_unknown_role_category_is_rejected_by_name(self) -> None:
        block = expected_block("company-roles", "discogs-unmapped-role")
        block["items"][0]["role_category"] = "remixing"

        with pytest.raises(IdentifierValidationError) as failure:
            validate_companies_block(block)

        assert failure.value.field == "items[0].role_category"

    def test_a_raw_role_outside_the_vocabulary_is_carried_rather_than_rejected(self) -> None:
        block = expected_block("company-roles", "discogs-unmapped-role")

        assert block["items"][0]["role"] == "Remixed At"
        assert block["items"][0]["role_category"] == "other"
        assert block["unmapped"]["roles"] == ["Remixed At"]
        validate_companies_block(block)

    def test_an_unusable_provider_id_is_null_rather_than_zero(self) -> None:
        block = expected_block("company-roles", "discogs-malformed-entries")

        assert block["items"][0]["discogs_id"] is None
        validate_companies_block(block)

        block["items"][0]["discogs_id"] = 0
        with pytest.raises(IdentifierValidationError) as failure:
            validate_companies_block(block)

        assert failure.value.field == "items[0].discogs_id"

    def test_a_boolean_provider_id_is_not_an_integer(self) -> None:
        block = expected_block("company-roles", "discogs-rights-holders")
        block["items"][0]["discogs_id"] = True

        with pytest.raises(IdentifierValidationError):
            validate_companies_block(block)

    def test_a_whole_float_provider_id_is_an_integer_as_json_schema_reads_it(self) -> None:
        block = expected_block("company-roles", "discogs-rights-holders")
        block["items"][0]["discogs_id"] = 82835.0

        validate_companies_block(block)

    def test_an_entity_type_that_is_not_a_digit_string_is_rejected(self) -> None:
        block = expected_block("company-roles", "discogs-rights-holders")
        block["items"][0]["source"]["entity_type"] = "pressing"

        with pytest.raises(IdentifierValidationError) as failure:
            validate_companies_block(block)

        assert failure.value.field == "items[0].source.entity_type"

    def test_a_company_catalogue_number_is_carried_when_the_plant_stamped_one(self) -> None:
        block = expected_block("company-roles", "discogs-company-catalogue-number")

        assert block["items"][0]["catno"] == "S-12345"
        assert block["items"][1]["catno"] is None
        validate_companies_block(block)


class TestAliasExtraction:
    @pytest.mark.parametrize("fixture", IDENTIFIER_FIXTURES, ids=lambda path: f"{path.parent.name}/{path.stem}")
    def test_the_extracted_refs_are_exactly_the_aliases_the_block_publishes(self, fixture: Path) -> None:
        block = json.loads(fixture.read_text())["expected"]

        expected = [AliasRef(alias["provider"], "release", alias["external_id"]) for alias in block["aliases"]]

        assert alias_refs_for_release(block) == expected

    def test_a_barcode_is_compared_as_digits_so_printed_grouping_does_not_change_identity(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")

        refs = alias_refs_for_release(block)

        assert AliasRef("barcode", "release", "5012394144777") in refs

    def test_a_catalogue_number_is_compared_without_case_or_internal_spacing(self) -> None:
        block = expected_block("identifiers", "discogs-duplicate-alias-values")

        refs = alias_refs_for_release(block)

        assert [ref for ref in refs if ref.provider == "catalog_number"] == [AliasRef("catalog_number", "release", "FAC 73")]

    def test_a_matrix_inscription_keeps_its_case_because_the_stamp_is_the_evidence(self) -> None:
        block = expected_block("identifiers", "discogs-matrix-runout-inscriptions")

        refs = alias_refs_for_release(block)

        assert refs == [
            AliasRef("matrix", "release", "PB 41447 A2 UTOPIA MS"),
            AliasRef("matrix", "release", "PB 41447-B.2 UTOPIA MS"),
        ]

    def test_two_items_that_normalize_alike_mint_one_alias(self) -> None:
        block = expected_block("identifiers", "discogs-duplicate-alias-values")

        assert len(block["items"]) == 4
        assert len(alias_refs_for_release(block)) == 2

    def test_a_type_without_an_alias_namespace_mints_nothing(self) -> None:
        block = expected_block("identifiers", "discogs-label-code-and-rights-society")

        assert [item["type"] for item in block["items"]] == ["label_code", "rights_society"]
        assert alias_refs_for_release(block) == []

    def test_a_block_with_no_identifiers_mints_nothing(self) -> None:
        assert alias_refs_for_release(expected_block("identifiers", "discogs-no-identifiers")) == []

    def test_a_value_that_normalizes_away_is_skipped_rather_than_minting_an_empty_key(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")
        block["items"][0]["value"] = "- -"
        block["aliases"] = [alias for alias in block["aliases"] if alias["provider"] != "barcode"]

        refs = alias_refs_for_release(block)

        assert [ref.provider for ref in refs] == ["catalog_number"]

    def test_a_malformed_block_mints_no_aliases_at_all(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")
        del block["unmapped"]

        with pytest.raises(IdentifierValidationError):
            alias_refs_for_release(block)

    def test_the_entity_kind_is_overridable_and_held_to_the_identity_vocabulary(self) -> None:
        block = expected_block("identifiers", "discogs-barcode-and-catalogue-number")

        assert all(ref.entity_kind == "master" for ref in alias_refs_for_release(block, "master"))
        with pytest.raises(ValueError, match="unknown entity kind"):
            alias_refs_for_release(block, "pressing")
