"""Schemas for shared GrooveMap agent tool inputs and outputs.

The canonical media block that ADR 0007 defines is modelled here as typed dictionaries rather
than as a validating model. `common.media` returns plain JSON-ready dicts by design, so the
block can be attached to an event or written to a JSONB column with no serializer, and these
types describe that value exactly instead of wrapping it. They also keep the second
distribution's dependency set at the one entry its contract test pins: `groovemap-agent-tools`
depends on `groovemap-runtime` and on nothing else, so adding a validation library here would
reach every consumer that installs it.

Every field is always present in a block, holding ``None`` or an empty list when unknown, so
none of these keys is optional.
"""

from __future__ import annotations

from typing import Literal, TypedDict


__all__ = ["MediaBlock", "MediaItem", "MediaSource", "MediaUnmapped"]


class MediaSource(TypedDict):
    """The provider fields a media item was derived from, as received."""

    provider: Literal["discogs", "musicbrainz"]
    name: str | None
    descriptions: list[str]
    text: str | None


class MediaItem(TypedDict):
    """One source medium entry, resolved onto the canonical vocabulary."""

    family: str
    medium: str
    qty: int
    size_inches: float | None
    speed_rpm: float | None
    channels: str | None
    codec: str | None
    variants: list[str]
    appearance: list[str]
    position: int | None
    track_count: int | None
    source: MediaSource


class MediaUnmapped(TypedDict):
    """Raw provider values the vocabulary did not recognize; sorted, unique, never dropped."""

    formats: list[str]
    descriptions: list[str]


class MediaBlock(TypedDict):
    """The canonical media block ADR 0007 attaches to every release-shaped record."""

    taxonomy_version: str
    items: list[MediaItem]
    families: list[str]
    release_kind: str | None
    traits: list[str]
    edition: list[str]
    packaging: str | None
    container: str | None
    flags: list[str]
    unmapped: MediaUnmapped
