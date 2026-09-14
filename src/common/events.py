"""First-party event and impression envelopes, and their standard-library validation.

ADR 0010 in the ``design`` repository adds two append-only tables in an ``activity`` schema:
``activity.events`` carries a typed envelope with a JSONB payload, and ``activity.impressions``
carries what a shown recommendation needs for later offline evaluation. The published JSON
Schemas for both are vendored into ``common.event_vocabulary`` with a digest check, together
with the closed version 1 event-type vocabulary.

The validation here is the standard library only, so ``groovemap-runtime`` gains no schema
library in its base dependencies and a consumer pinned to an older lockfile keeps resolving.
What keeps this validator honest is the conformance test rather than the shared code: the suite
validates all twenty-two vendored fixtures against the vendored JSON Schemas with
``jsonschema`` (a development dependency) and asserts this validator returns the same verdict on
every one, so the Python model cannot drift from the published schema without a failing test.

Two details where the published schema is narrower than a casual reading of the ADR, and where
the schema wins because the conformance test measures against it: impression ``position`` is
one-based (``minimum: 1``), and ``propensity`` is a probability a policy actually assigned, so
zero is excluded (``exclusiveMinimum: 0``, ``maximum: 1``).

:class:`Impression` carries two fields the envelope does not: ``recorded_at`` and
``consent_purposes``. Both are columns of the landed ``activity.impressions`` table — the second
is ``NOT NULL`` — so a row cannot be written without them, while the wire envelope leaves them
to the writer. They are therefore optional on the way in and always present in
:meth:`Impression.to_row`, and :func:`validate_impression` accepts a document that omits them,
which is what keeps its verdict identical to the schema's on every published fixture.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from common.identity import new_id


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


__all__ = [
    "Event",
    "EventValidationError",
    "Impression",
    "consent_purposes",
    "event_types",
    "is_valid_event_type",
    "new_event",
    "new_impression",
    "payload_schema_for",
    "surfaces",
    "validate_event",
    "validate_impression",
]

_VOCABULARY_PACKAGE: Final = "common.event_vocabulary"
_VOCABULARY_RESOURCE: Final = "event-types.json"
_EVENT_SCHEMA_RESOURCE: Final = "event-envelope.schema.json"
_IMPRESSION_SCHEMA_RESOURCE: Final = "impression.schema.json"

# The idempotency key is bounded by the envelope schema, not by the column, because it is the
# key a retried write is deduplicated on and an unbounded one would make that index unbounded.
_IDEMPOTENCY_KEY_MAX_LENGTH: Final = 200

# The two columns `activity.impressions` carries beyond the published envelope. A document may
# omit them; a row may not.
_IMPRESSION_COLUMN_ONLY_FIELDS: Final[tuple[str, ...]] = ("recorded_at", "consent_purposes")


class EventValidationError(ValueError):
    """One envelope field failed validation.

    Subclasses ``ValueError`` so a writer that already guards against bad input keeps working,
    and names the offending field so a rejection is actionable without re-deriving it from the
    message.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field = field_name
        self.message = message


def _read_vendored(resource: str) -> dict[str, Any]:
    document = (files(_VOCABULARY_PACKAGE) / resource).read_text(encoding="utf-8")
    parsed: dict[str, Any] = json.loads(document)
    return parsed


@cache
def _vocabulary() -> dict[str, Any]:
    """Return the vendored event-type vocabulary, parsed once per process."""
    return _read_vendored(_VOCABULARY_RESOURCE)


@cache
def _event_schema() -> dict[str, Any]:
    """Return the vendored event-envelope JSON Schema, parsed once per process."""
    return _read_vendored(_EVENT_SCHEMA_RESOURCE)


@cache
def _impression_schema() -> dict[str, Any]:
    """Return the vendored impression JSON Schema, parsed once per process."""
    return _read_vendored(_IMPRESSION_SCHEMA_RESOURCE)


@cache
def _types_by_id() -> dict[str, dict[str, Any]]:
    return {entry["id"]: entry for entry in _vocabulary()["event_types"]}


@cache
def event_types() -> tuple[str, ...]:
    """Return the closed version 1 event-type vocabulary, in vocabulary order.

    Adding a type is additive within version 1; renaming or removing one is a new vocabulary
    version, because a stored ``event_type`` is historical data the immutability trigger will
    not let anyone rewrite.
    """
    return tuple(entry["id"] for entry in _vocabulary()["event_types"])


@cache
def surfaces() -> tuple[str, ...]:
    """Return the surfaces events and impressions are recorded against, in vocabulary order."""
    return tuple(surface["id"] for surface in _vocabulary()["surfaces"])


@cache
def consent_purposes() -> tuple[str, ...]:
    """Return the two purposes consent is granted for, in vocabulary order.

    A writer snapshots the purposes active at the moment of the write onto the row, which is what
    makes an old row interpretable later without reconstructing the grant history.
    """
    return tuple(_vocabulary()["consent_purposes"])


def is_valid_event_type(event_type: str) -> bool:
    """Return whether ``event_type`` names a type the closed version 1 vocabulary carries."""
    return event_type in _types_by_id()


@cache
def payload_schema_for(event_type: str) -> Mapping[str, Any]:
    """Return the payload schema an event type's body is described by.

    The returned sub-schema is resolved out of the vendored ``event-types.json`` document and
    still references that document's ``#/$defs``, so validating against it needs the whole
    document as the resolution base rather than the fragment alone.

    Args:
        event_type: A type from :func:`event_types`.

    Returns:
        A read-only view of the vendored sub-schema.

    Raises:
        KeyError: If the vocabulary holds no such event type.
    """
    pointer: str = _types_by_id()[event_type]["payload_schema"]
    node: Any = _vocabulary()
    for segment in pointer.removeprefix("#/").split("/"):
        node = node[segment]
    resolved: Mapping[str, Any] = MappingProxyType(node)
    return resolved


def _require_mapping(field_name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise EventValidationError(field_name, "must be an object")


def _require_fields(field_name: str, document: Mapping[str, Any], required: tuple[str, ...], allowed: frozenset[str]) -> None:
    """Enforce the schema's required set and its ``additionalProperties: false``."""
    for name in required:
        if name not in document:
            raise EventValidationError(name, "is required")
    for name in sorted(document):
        if name not in allowed:
            raise EventValidationError(name, f"is not part of the {field_name} envelope")


def _require_uuid(field_name: str, value: Any) -> None:
    if isinstance(value, uuid.UUID):
        return
    if not isinstance(value, str):
        raise EventValidationError(field_name, "must be a UUID")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise EventValidationError(field_name, "must be a UUID") from exc


def _require_nullable_uuid(field_name: str, value: Any) -> None:
    if value is not None:
        _require_uuid(field_name, value)


def _require_timestamp(field_name: str, value: Any) -> None:
    """Require a timezone-aware instant, as a datetime or an ISO 8601 string.

    ``occurred_at`` and ``recorded_at`` are split precisely so a late or replayed write stays
    honest, which only holds if both carry an offset: a naive timestamp would be read as the
    reader's local time and silently move the event.
    """
    moment = value
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError as exc:
            raise EventValidationError(field_name, "must be an ISO 8601 timestamp") from exc
    if not isinstance(moment, datetime):
        raise EventValidationError(field_name, "must be an ISO 8601 timestamp")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise EventValidationError(field_name, "must carry a UTC offset")


def _require_text(field_name: str, value: Any, *, maximum_length: int | None = None) -> None:
    if not isinstance(value, str) or not value:
        raise EventValidationError(field_name, "must be a non-empty string")
    if maximum_length is not None and len(value) > maximum_length:
        raise EventValidationError(field_name, f"must be at most {maximum_length} characters")


def _require_nullable_text(field_name: str, value: Any) -> None:
    if value is not None:
        _require_text(field_name, value)


def _require_integer(field_name: str, value: Any, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventValidationError(field_name, "must be an integer")
    if value < minimum:
        raise EventValidationError(field_name, f"must be at least {minimum}")


def _require_number(field_name: str, value: Any, *, exclusive_minimum: float | None = None, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EventValidationError(field_name, "must be a number")
    if exclusive_minimum is not None and value <= exclusive_minimum:
        raise EventValidationError(field_name, f"must be greater than {exclusive_minimum}")
    if maximum is not None and value > maximum:
        raise EventValidationError(field_name, f"must be at most {maximum}")


def _require_consent_purposes(field_name: str, value: Any) -> None:
    if not isinstance(value, list | tuple):
        raise EventValidationError(field_name, "must be an array")
    known = consent_purposes()
    for purpose in value:
        if purpose not in known:
            raise EventValidationError(field_name, f"{purpose!r} is not a consent purpose")
    if len(set(value)) != len(value):
        raise EventValidationError(field_name, "must not repeat a purpose")


def validate_event(document: Mapping[str, Any]) -> None:
    """Validate one event envelope against ADR 0010's published contract.

    Args:
        document: The envelope, keyed by column name. Values may be JSON scalars, as a decoded
            wire document carries them, or the native ``UUID`` and ``datetime`` objects
            :meth:`Event.to_row` produces.

    Raises:
        EventValidationError: On the first field that fails, naming that field.
    """
    _require_mapping("event", document)
    _require_fields("event", document, _event_required(), _event_allowed())

    _require_text("event_type", document["event_type"])
    if not is_valid_event_type(document["event_type"]):
        raise EventValidationError("event_type", f"{document['event_type']!r} is not in the version 1 vocabulary")
    _require_uuid("event_id", document["event_id"])
    _require_integer("schema_version", document["schema_version"], minimum=1)
    _require_uuid("subject_id", document["subject_id"])
    _require_nullable_uuid("session_id", document["session_id"])
    _require_timestamp("occurred_at", document["occurred_at"])
    _require_timestamp("recorded_at", document["recorded_at"])
    _require_text("producer", document["producer"])
    _require_consent_purposes("consent_purposes", document["consent_purposes"])
    _require_nullable_text("model_version", document["model_version"])
    _require_nullable_text("feature_version", document["feature_version"])
    _require_text("idempotency_key", document["idempotency_key"], maximum_length=_IDEMPOTENCY_KEY_MAX_LENGTH)
    _require_mapping("payload", document["payload"])


def validate_impression(document: Mapping[str, Any]) -> None:
    """Validate one impression row against ADR 0010's published contract.

    ``recorded_at`` and ``consent_purposes`` are columns the landed table adds beyond the
    envelope: absent is accepted, present is validated.

    Args:
        document: The impression, keyed by column name, in either wire or native types.

    Raises:
        EventValidationError: On the first field that fails, naming that field.
    """
    _require_mapping("impression", document)
    _require_fields("impression", document, _impression_required(), _impression_allowed())

    _require_uuid("impression_id", document["impression_id"])
    _require_uuid("subject_id", document["subject_id"])
    _require_text("surface", document["surface"])
    if document["surface"] not in surfaces():
        raise EventValidationError("surface", f"{document['surface']!r} is not a surface")
    _require_text("policy_id", document["policy_id"])
    _require_uuid("candidate_set_id", document["candidate_set_id"])
    _require_integer("position", document["position"], minimum=1)
    _require_uuid("item_id", document["item_id"])
    _require_number("score", document["score"])
    _require_number("propensity", document["propensity"], exclusive_minimum=0.0, maximum=1.0)
    _require_uuid("request_id", document["request_id"])
    _require_timestamp("occurred_at", document["occurred_at"])

    if document.get("recorded_at") is not None:
        _require_timestamp("recorded_at", document["recorded_at"])
    if "consent_purposes" in document:
        _require_consent_purposes("consent_purposes", document["consent_purposes"])


@cache
def _event_required() -> tuple[str, ...]:
    return tuple(_event_schema()["required"])


@cache
def _event_allowed() -> frozenset[str]:
    return frozenset(_event_schema()["properties"])


@cache
def _impression_required() -> tuple[str, ...]:
    return tuple(_impression_schema()["required"])


@cache
def _impression_allowed() -> frozenset[str]:
    return frozenset(_impression_schema()["properties"]) | frozenset(_IMPRESSION_COLUMN_ONLY_FIELDS)


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _as_optional_uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else _as_uuid(value)


def _as_datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


@dataclass(frozen=True, slots=True)
class Event:
    """One row of ``activity.events``, with every column of the ADR 0010 envelope.

    ``occurred_at`` is when the thing happened and ``recorded_at`` is when the writer stored it;
    the pair is what keeps a late or replayed write honest. ``consent_purposes`` is the snapshot
    taken at write time, which a training-time reader re-checks against the grant table rather
    than trusting on its own.
    """

    event_id: uuid.UUID
    event_type: str
    schema_version: int
    subject_id: uuid.UUID
    session_id: uuid.UUID | None
    occurred_at: datetime
    recorded_at: datetime
    producer: str
    consent_purposes: tuple[str, ...]
    model_version: str | None
    feature_version: str | None
    idempotency_key: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Return the envelope keyed by ``activity.events`` column name, ready to insert."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "producer": self.producer,
            "consent_purposes": list(self.consent_purposes),
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "idempotency_key": self.idempotency_key,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> Event:
        """Build an event from a decoded wire document, validating it first.

        Raises:
            EventValidationError: If the document is not a valid envelope.
        """
        validate_event(document)
        return cls(
            event_id=_as_uuid(document["event_id"]),
            event_type=document["event_type"],
            schema_version=document["schema_version"],
            subject_id=_as_uuid(document["subject_id"]),
            session_id=_as_optional_uuid(document["session_id"]),
            occurred_at=_as_datetime(document["occurred_at"]),
            recorded_at=_as_datetime(document["recorded_at"]),
            producer=document["producer"],
            consent_purposes=tuple(document["consent_purposes"]),
            model_version=document["model_version"],
            feature_version=document["feature_version"],
            idempotency_key=document["idempotency_key"],
            payload=dict(document["payload"]),
        )


@dataclass(frozen=True, slots=True)
class Impression:
    """One row of ``activity.impressions``, with every column of the ADR 0010 envelope.

    ``policy_id``, ``candidate_set_id``, ``position``, and ``propensity`` are the four fields
    that justify a separate table: each describes the ranking decision as it was made, and none
    can be recovered afterwards from the catalog or from the outcome.

    Outcomes are never columns here. Opened, saved, dismissed, and hidden are events carrying
    this ``impression_id``, which is what keeps the row immutable while one impression accrues
    several outcomes over time.

    ``recorded_at`` and ``consent_purposes`` are columns of the landed table rather than fields
    of the published envelope, so they default and are always present in :meth:`to_row`.
    """

    impression_id: uuid.UUID
    subject_id: uuid.UUID
    surface: str
    policy_id: str
    candidate_set_id: uuid.UUID
    position: int
    item_id: uuid.UUID
    score: float
    propensity: float
    request_id: uuid.UUID
    occurred_at: datetime
    recorded_at: datetime | None = None
    consent_purposes: tuple[str, ...] = ()

    def to_row(self) -> dict[str, Any]:
        """Return the impression keyed by ``activity.impressions`` column name, ready to insert."""
        return {
            "impression_id": self.impression_id,
            "subject_id": self.subject_id,
            "surface": self.surface,
            "policy_id": self.policy_id,
            "candidate_set_id": self.candidate_set_id,
            "position": self.position,
            "item_id": self.item_id,
            "score": self.score,
            "propensity": self.propensity,
            "request_id": self.request_id,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "consent_purposes": list(self.consent_purposes),
        }

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> Impression:
        """Build an impression from a decoded wire document or a row, validating it first.

        Raises:
            EventValidationError: If the document is not a valid impression.
        """
        validate_impression(document)
        recorded_at = document.get("recorded_at")
        return cls(
            impression_id=_as_uuid(document["impression_id"]),
            subject_id=_as_uuid(document["subject_id"]),
            surface=document["surface"],
            policy_id=document["policy_id"],
            candidate_set_id=_as_uuid(document["candidate_set_id"]),
            position=document["position"],
            item_id=_as_uuid(document["item_id"]),
            score=document["score"],
            propensity=document["propensity"],
            request_id=_as_uuid(document["request_id"]),
            occurred_at=_as_datetime(document["occurred_at"]),
            recorded_at=None if recorded_at is None else _as_datetime(recorded_at),
            consent_purposes=tuple(document.get("consent_purposes", ())),
        )


def new_event(
    *,
    event_type: str,
    subject_id: uuid.UUID,
    producer: str,
    consent_purposes: Iterable[str],
    idempotency_key: str,
    payload: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
    session_id: uuid.UUID | None = None,
    model_version: str | None = None,
    feature_version: str | None = None,
) -> Event:
    """Mint a valid event, stamping the id, the schema version, and ``recorded_at``.

    ``producer`` and ``consent_purposes`` are explicit rather than defaulted: the producer names
    which service's code wrote the row, and the purposes are the consent snapshot the row is
    interpreted under years later. Neither has a safe default.

    ``idempotency_key`` is explicit for the same reason. It is what makes a retried write safe,
    so a generated one would defeat the guarantee it exists for by making every retry a new row.

    Args:
        event_type: A type from :func:`event_types`.
        subject_id: The pseudonymous subject, never the user id.
        producer: The service that wrote the row.
        consent_purposes: The purposes active at the moment of the write.
        idempotency_key: The key this write deduplicates on, unique with ``occurred_at``.
        payload: The type-specific body. Defaults to an empty object.
        occurred_at: When the thing happened. Defaults to now.
        session_id: The session it happened in, when there is one.
        model_version: The model version in play, when there is one.
        feature_version: The feature version in play, when there is one.

    Returns:
        A validated :class:`Event`.

    Raises:
        EventValidationError: If the event type is unknown, or any field is invalid.
    """
    if not is_valid_event_type(event_type):
        raise EventValidationError("event_type", f"{event_type!r} is not in the version 1 vocabulary")

    recorded_at = datetime.now(UTC)
    event = Event(
        event_id=new_id(),
        event_type=event_type,
        schema_version=_types_by_id()[event_type]["schema_version"],
        subject_id=subject_id,
        session_id=session_id,
        occurred_at=recorded_at if occurred_at is None else occurred_at,
        recorded_at=recorded_at,
        producer=producer,
        consent_purposes=tuple(consent_purposes),
        model_version=model_version,
        feature_version=feature_version,
        idempotency_key=idempotency_key,
        payload=dict(payload or {}),
    )
    validate_event(event.to_row())
    return event


def new_impression(
    *,
    subject_id: uuid.UUID,
    surface: str,
    policy_id: str,
    candidate_set_id: uuid.UUID,
    position: int,
    item_id: uuid.UUID,
    score: float,
    propensity: float,
    request_id: uuid.UUID,
    consent_purposes: Iterable[str],
    occurred_at: datetime | None = None,
) -> Impression:
    """Mint a valid impression, stamping the id and ``recorded_at``.

    Positions are one-based, and ``propensity`` is the probability the live policy assigned to
    choosing this item, so it is greater than zero and at most one.

    Args:
        subject_id: The pseudonymous subject, never the user id.
        surface: A surface from :func:`surfaces`.
        policy_id: The ranking policy that made the decision.
        candidate_set_id: The candidate set the decision was made over.
        position: The one-based position the item was shown at.
        item_id: The native id of the item shown.
        score: The score the policy gave the item.
        propensity: The probability the policy would choose the item.
        request_id: The request the list was built for.
        consent_purposes: The purposes active at the moment of the write.
        occurred_at: When the item was shown. Defaults to now.

    Returns:
        A validated :class:`Impression`.

    Raises:
        EventValidationError: If any field is invalid.
    """
    recorded_at = datetime.now(UTC)
    impression = Impression(
        impression_id=new_id(),
        subject_id=subject_id,
        surface=surface,
        policy_id=policy_id,
        candidate_set_id=candidate_set_id,
        position=position,
        item_id=item_id,
        score=score,
        propensity=propensity,
        request_id=request_id,
        occurred_at=recorded_at if occurred_at is None else occurred_at,
        recorded_at=recorded_at,
        consent_purposes=tuple(consent_purposes),
    )
    validate_impression(impression.to_row())
    return impression
