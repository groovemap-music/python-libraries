"""Tests for the first-party event and impression envelopes.

The conformance test is the load-bearing one: it validates every vendored fixture against the
vendored JSON Schemas with `jsonschema` — a development dependency the runtime never ships — and
asserts the standard-library validator in `common.events` returns the same verdict on each, so
the Python model cannot drift from the published schema without a failing test here.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from common.events import (
    Event,
    EventValidationError,
    Impression,
    consent_purposes,
    event_types,
    is_valid_event_type,
    new_event,
    new_impression,
    payload_schema_for,
    surfaces,
    validate_event,
    validate_impression,
)


FIXTURE_DIRECTORY = Path(__file__).resolve().parent / "fixtures" / "events"
VOCABULARY_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "common" / "event_vocabulary"

SUBJECT = uuid.UUID("0192f0a1-0000-7000-8000-00000000a001")
ITEM = uuid.UUID("0192f0b2-0000-7000-8000-00000000c001")
CANDIDATE_SET = uuid.UUID("0192f0c3-0000-7000-8000-00000000d002")
REQUEST = uuid.UUID("0192f0c3-0000-7000-8000-00000000d003")


def fixtures() -> list[Path]:
    """Return every conformance fixture the design repository publishes, source record aside."""
    return sorted(path for path in FIXTURE_DIRECTORY.glob("*.json") if path.name != "source.json")


def schema_for(envelope: str) -> dict[str, Any]:
    resource = "event-envelope.schema.json" if envelope == "event" else "impression.schema.json"
    document: dict[str, Any] = json.loads((VOCABULARY_DIRECTORY / resource).read_text())
    return document


def validator_for(envelope: str) -> Draft202012Validator:
    schema = schema_for(envelope)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def stdlib_verdict(envelope: str, document: Any) -> bool:
    validate = validate_event if envelope == "event" else validate_impression
    try:
        validate(document)
    except EventValidationError:
        return False
    return True


def valid_event_document() -> dict[str, Any]:
    case = json.loads((FIXTURE_DIRECTORY / "event-search-query.json").read_text())
    document: dict[str, Any] = case["document"]
    return document


def valid_impression_document() -> dict[str, Any]:
    case = json.loads((FIXTURE_DIRECTORY / "impression-recommendation-shown.json").read_text())
    document: dict[str, Any] = case["document"]
    return document


class TestVocabulary:
    def test_version_1_carries_exactly_the_twenty_one_published_types(self) -> None:
        assert len(event_types()) == 21
        assert event_types()[0] == "search.query"
        assert "recommendation.hidden" in event_types()
        assert is_valid_event_type("account.erasure_requested")
        assert not is_valid_event_type("search.exploded")

    def test_every_type_names_a_surface_the_vocabulary_carries(self) -> None:
        assert surfaces() == ("search", "recommendation", "fit", "collection", "wantlist", "consent", "account")
        assert all(event_type.split(".")[0] in surfaces() for event_type in event_types())

    def test_fit_is_a_vendored_surface_with_its_outcome_event_types(self) -> None:
        assert "fit" in surfaces()
        assert {event_type for event_type in event_types() if event_type.startswith("fit.")} == {
            "fit.shown",
            "fit.opened",
            "fit.saved",
            "fit.dismissed",
            "fit.hidden",
        }
        for event_type in ("fit.shown", "fit.opened", "fit.saved", "fit.dismissed", "fit.hidden"):
            assert is_valid_event_type(event_type)

    def test_consent_is_granted_for_exactly_two_purposes(self) -> None:
        assert consent_purposes() == ("product_analytics", "model_training")

    def test_a_payload_schema_resolves_to_the_vendored_sub_schema(self) -> None:
        schema = payload_schema_for("search.query")

        assert schema["required"] == ["query", "filters", "result_count", "request_id"]
        assert schema["additionalProperties"] is False

    def test_the_returned_payload_schema_cannot_be_mutated(self) -> None:
        with pytest.raises(TypeError):
            payload_schema_for("search.query")["required"] = []  # type: ignore[index]

    def test_an_unknown_event_type_has_no_payload_schema(self) -> None:
        with pytest.raises(KeyError):
            payload_schema_for("search.exploded")


class TestConformance:
    def test_the_vendored_fixture_set_is_the_published_one(self) -> None:
        cases = [json.loads(path.read_text()) for path in fixtures()]

        assert len(cases) == 22
        assert sum(1 for case in cases if not case["valid"]) == 4
        assert {case["envelope"] for case in cases} == {"event", "impression"}

    @pytest.mark.parametrize("fixture", fixtures(), ids=lambda path: path.stem)
    def test_the_stdlib_validator_agrees_with_the_published_schema(self, fixture: Path) -> None:
        case = json.loads(fixture.read_text())
        schema_valid = validator_for(case["envelope"]).is_valid(case["document"])

        assert schema_valid is case["valid"], "the vendored schema disagrees with the fixture's own verdict"
        assert stdlib_verdict(case["envelope"], case["document"]) is schema_valid

    @pytest.mark.parametrize("fixture", fixtures(), ids=lambda path: path.stem)
    def test_every_valid_fixture_round_trips_through_its_model(self, fixture: Path) -> None:
        case = json.loads(fixture.read_text())
        if not case["valid"]:
            pytest.skip("invalid fixtures are rejected rather than modelled")

        model: Event | Impression = Event.from_mapping(case["document"]) if case["envelope"] == "event" else Impression.from_mapping(case["document"])
        row = model.to_row()

        assert set(case["document"]) <= set(row)
        for name, value in case["document"].items():
            rendered = row[name]
            if isinstance(rendered, uuid.UUID):
                rendered = str(rendered)
            elif isinstance(rendered, datetime):
                rendered = value  # compared through the parse below
            assert rendered == value, name
        assert stdlib_verdict(case["envelope"], row) is True


class TestEventModel:
    def test_to_row_is_keyed_by_column_name_and_carries_every_envelope_column(self) -> None:
        row = Event.from_mapping(valid_event_document()).to_row()

        assert set(row) == set(schema_for("event")["required"])
        assert isinstance(row["event_id"], uuid.UUID)
        assert isinstance(row["occurred_at"], datetime)
        assert isinstance(row["consent_purposes"], list)
        assert isinstance(row["payload"], dict)

    def test_from_mapping_rejects_a_document_the_schema_rejects(self) -> None:
        document = valid_event_document()
        del document["producer"]

        with pytest.raises(EventValidationError) as raised:
            Event.from_mapping(document)

        assert raised.value.field == "producer"

    def test_a_null_session_is_carried_rather_than_dropped(self) -> None:
        document = valid_event_document()
        document["session_id"] = None

        event = Event.from_mapping(document)

        assert event.session_id is None
        assert event.to_row()["session_id"] is None


class TestImpressionModel:
    def test_to_row_adds_the_two_columns_the_envelope_leaves_to_the_writer(self) -> None:
        row = Impression.from_mapping(valid_impression_document()).to_row()

        assert set(row) == set(schema_for("impression")["required"]) | {"recorded_at", "consent_purposes"}
        assert row["recorded_at"] is None
        assert row["consent_purposes"] == []

    def test_a_row_carrying_both_extra_columns_round_trips(self) -> None:
        document = valid_impression_document()
        document["recorded_at"] = "2026-09-13T18:05:00.412Z"
        document["consent_purposes"] = ["product_analytics"]

        impression = Impression.from_mapping(document)

        assert impression.consent_purposes == ("product_analytics",)
        assert impression.recorded_at == datetime(2026, 9, 13, 18, 5, 0, 412000, tzinfo=UTC)

    def test_the_four_decision_fields_survive_the_round_trip(self) -> None:
        impression = Impression.from_mapping(valid_impression_document())

        assert impression.policy_id == "similar-artist-v3"
        assert impression.candidate_set_id == CANDIDATE_SET
        assert impression.position == 1
        assert impression.propensity == 0.25


class TestValidateEvent:
    def test_a_non_mapping_document_is_rejected(self) -> None:
        with pytest.raises(EventValidationError) as raised:
            validate_event(["not", "an", "object"])  # type: ignore[arg-type]

        assert raised.value.field == "event"

    def test_a_field_outside_the_envelope_is_rejected(self) -> None:
        document = valid_event_document()
        document["ip_address"] = "203.0.113.4"

        with pytest.raises(EventValidationError) as raised:
            validate_event(document)

        assert raised.value.field == "ip_address"

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("event_id", "not-a-uuid"),
            ("event_id", 17),
            ("event_type", "search.exploded"),
            ("event_type", 17),
            ("schema_version", 0),
            ("schema_version", True),
            ("schema_version", "1"),
            ("subject_id", None),
            ("session_id", "not-a-uuid"),
            ("occurred_at", "not-a-timestamp"),
            ("occurred_at", 1789),
            ("recorded_at", "2026-09-13T18:05:00"),
            ("producer", ""),
            ("producer", None),
            ("consent_purposes", "product_analytics"),
            ("consent_purposes", ["marketing"]),
            ("consent_purposes", ["product_analytics", "product_analytics"]),
            ("model_version", ""),
            ("feature_version", 3),
            ("idempotency_key", ""),
            ("idempotency_key", "k" * 201),
            ("payload", "{}"),
        ],
    )
    def test_a_field_outside_the_contract_is_rejected_by_name(self, field_name: str, value: Any) -> None:
        document = valid_event_document()
        document[field_name] = value

        with pytest.raises(EventValidationError) as raised:
            validate_event(document)

        assert raised.value.field == field_name

    def test_a_naive_timestamp_is_rejected_because_it_would_silently_move_the_event(self) -> None:
        document = valid_event_document()
        document["occurred_at"] = datetime(2026, 9, 13, 18, 5)

        with pytest.raises(EventValidationError, match="UTC offset"):
            validate_event(document)

    def test_native_uuid_and_datetime_values_are_accepted(self) -> None:
        document = valid_event_document()
        document["event_id"] = uuid.UUID(document["event_id"])
        document["occurred_at"] = datetime.now(UTC)

        validate_event(document)


class TestValidateImpression:
    def test_a_non_mapping_document_is_rejected(self) -> None:
        with pytest.raises(EventValidationError) as raised:
            validate_impression(None)  # type: ignore[arg-type]

        assert raised.value.field == "impression"

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("impression_id", "not-a-uuid"),
            ("subject_id", None),
            ("surface", "billboard"),
            ("surface", 4),
            ("policy_id", ""),
            ("candidate_set_id", "not-a-uuid"),
            ("position", 0),
            ("position", 1.5),
            ("item_id", "not-a-uuid"),
            ("score", "0.5"),
            ("score", True),
            ("propensity", 0),
            ("propensity", 1.5),
            ("request_id", "not-a-uuid"),
            ("occurred_at", "not-a-timestamp"),
            ("recorded_at", "not-a-timestamp"),
            ("consent_purposes", ["marketing"]),
        ],
    )
    def test_a_field_outside_the_contract_is_rejected_by_name(self, field_name: str, value: Any) -> None:
        document = valid_impression_document()
        document[field_name] = value

        with pytest.raises(EventValidationError) as raised:
            validate_impression(document)

        assert raised.value.field == field_name

    def test_a_missing_decision_field_is_rejected_by_name(self) -> None:
        document = valid_impression_document()
        del document["candidate_set_id"]

        with pytest.raises(EventValidationError) as raised:
            validate_impression(document)

        assert raised.value.field == "candidate_set_id"

    def test_a_field_outside_the_envelope_and_the_table_is_rejected(self) -> None:
        document = valid_impression_document()
        document["user_id"] = str(SUBJECT)

        with pytest.raises(EventValidationError) as raised:
            validate_impression(document)

        assert raised.value.field == "user_id"

    def test_a_position_of_one_is_accepted_because_positions_are_one_based(self) -> None:
        document = valid_impression_document()
        document["position"] = 1

        validate_impression(document)

    def test_a_propensity_of_exactly_one_is_accepted(self) -> None:
        document = valid_impression_document()
        document["propensity"] = 1

        validate_impression(document)


class TestConstructors:
    def test_new_event_stamps_the_id_the_schema_version_and_recorded_at(self) -> None:
        before = datetime.now(UTC)
        event = new_event(
            event_type="collection.item_added",
            subject_id=SUBJECT,
            producer="catalog-api",
            consent_purposes=["product_analytics"],
            idempotency_key="collection.item_added:1",
            payload={"item_id": str(ITEM), "artifact_id": None, "owned_copy_id": None},
        )

        assert event.event_id.version == 7
        assert event.schema_version == 1
        assert before <= event.recorded_at <= datetime.now(UTC)
        assert event.occurred_at == event.recorded_at
        assert event.consent_purposes == ("product_analytics",)
        validate_event(event.to_row())

    def test_new_event_keeps_a_caller_supplied_occurrence_time(self) -> None:
        occurred_at = datetime.now(UTC) - timedelta(minutes=5)

        event = new_event(
            event_type="consent.granted",
            subject_id=SUBJECT,
            producer="catalog-api",
            consent_purposes=["product_analytics", "model_training"],
            idempotency_key="consent.granted:1",
            payload={"purpose": "model_training"},
            occurred_at=occurred_at,
            session_id=None,
            model_version="ranker-2026-09",
            feature_version="features-v4",
        )

        assert event.occurred_at == occurred_at
        assert event.recorded_at > event.occurred_at
        assert event.model_version == "ranker-2026-09"

    def test_new_event_rejects_a_type_outside_the_closed_vocabulary(self) -> None:
        with pytest.raises(EventValidationError) as raised:
            new_event(
                event_type="search.exploded",
                subject_id=SUBJECT,
                producer="catalog-api",
                consent_purposes=["product_analytics"],
                idempotency_key="search.exploded:1",
            )

        assert raised.value.field == "event_type"

    def test_new_event_rejects_an_invalid_field_before_it_becomes_a_row(self) -> None:
        with pytest.raises(EventValidationError) as raised:
            new_event(
                event_type="search.query",
                subject_id=SUBJECT,
                producer="",
                consent_purposes=["product_analytics"],
                idempotency_key="search.query:1",
            )

        assert raised.value.field == "producer"

    def test_new_impression_stamps_the_id_and_recorded_at(self) -> None:
        impression = new_impression(
            subject_id=SUBJECT,
            surface="recommendation",
            policy_id="similar-artist-v3",
            candidate_set_id=CANDIDATE_SET,
            position=1,
            item_id=ITEM,
            score=0.8125,
            propensity=0.25,
            request_id=REQUEST,
            consent_purposes=["product_analytics"],
        )

        assert impression.impression_id.version == 7
        assert impression.recorded_at is not None
        assert impression.occurred_at == impression.recorded_at
        assert impression.to_row()["consent_purposes"] == ["product_analytics"]
        validate_impression(impression.to_row())

    def test_new_impression_rejects_a_zero_propensity(self) -> None:
        with pytest.raises(EventValidationError) as raised:
            new_impression(
                subject_id=SUBJECT,
                surface="recommendation",
                policy_id="similar-artist-v3",
                candidate_set_id=CANDIDATE_SET,
                position=1,
                item_id=ITEM,
                score=0.5,
                propensity=0.0,
                request_id=REQUEST,
                consent_purposes=["product_analytics"],
            )

        assert raised.value.field == "propensity"
