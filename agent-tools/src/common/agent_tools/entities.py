"""GrooveMap entity detail tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast


if TYPE_CHECKING:
    from common.agent_tools.schemas import MediaBlock


HandlerFn = Callable[[Any, str], Awaitable[dict[str, Any] | None]]


async def _entity_details(entity_type: str, *, driver: Any, name: str, handler: HandlerFn) -> dict[str, Any]:
    node = await handler(driver, name)
    if node is None:
        return {"error": f"{entity_type} '{name}' not found"}
    result = dict(node)
    result["_entity_type"] = entity_type
    return result


async def get_artist_details(*, driver: Any, name: str, handler: HandlerFn) -> dict[str, Any]:
    return await _entity_details("artist", driver=driver, name=name, handler=handler)


async def get_label_details(*, driver: Any, name: str, handler: HandlerFn) -> dict[str, Any]:
    return await _entity_details("label", driver=driver, name=name, handler=handler)


async def get_genre_details(*, driver: Any, name: str, handler: HandlerFn) -> dict[str, Any]:
    return await _entity_details("genre", driver=driver, name=name, handler=handler)


async def get_style_details(*, driver: Any, name: str, handler: HandlerFn) -> dict[str, Any]:
    return await _entity_details("style", driver=driver, name=name, handler=handler)


async def get_release_details(*, driver: Any, name: str, handler: HandlerFn) -> dict[str, Any]:
    """Return one release's stored fields, tagged with its entity type.

    A release stored since ADR 0007 carries a ``media`` key holding the canonical media block:
    its resolved media items plus the release-level kind, traits, edition, packaging, and
    container facts. It is passed through untouched, exactly as the store holds it, and is
    typed as :class:`~common.agent_tools.schemas.MediaBlock`.

    The key is absent for a release written before the block existed, so read it with
    :func:`media_of` (or ``result.get("media")``) rather than by subscript. Derive a
    best-effort block from the legacy ``formats`` names with
    ``common.media.legacy_format_names_to_media`` when one is needed and none is stored.
    """
    return await _entity_details("release", driver=driver, name=name, handler=handler)


def media_of(release: dict[str, Any]) -> MediaBlock | None:
    """Return a release's canonical media block, or ``None`` when it carries none."""
    media = release.get("media")
    if not isinstance(media, dict):
        return None
    return cast("MediaBlock", media)
