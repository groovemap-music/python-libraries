"""GrooveMap discovery tools — search, collaborators, trends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from common.media import family_ids, medium_ids


SearchFn = Callable[..., Awaitable[dict[str, Any]]]
CollaboratorsFn = Callable[..., Awaitable[list[dict[str, Any]]]]
TrendsHandler = Callable[[Any, str], Awaitable[list[dict[str, Any]]]]


def validate_media_filter(media: Iterable[str]) -> list[str]:
    """Check a media filter against the canonical taxonomy and return it as a list.

    A filter entry is a family id (``vinyl``) or a medium id (``vinyl_12``) from the vocabulary
    ADR 0007 makes authoritative. Validating here rather than at the query layer means an
    agent that hallucinates a medium gets one clear error naming what it got wrong, instead of
    an empty result set that reads as "no such records".

    Args:
        media: The requested family or medium ids.

    Returns:
        The ids as a list, unchanged and in the order given.

    Raises:
        ValueError: If any id is not in the taxonomy. The message names every unknown id.
    """
    requested = list(media)
    known = set(family_ids()) | set(medium_ids())
    unknown = sorted({value for value in requested if value not in known})
    if unknown:
        raise ValueError(
            f"unknown media ids: {', '.join(unknown)}. Valid ids are the taxonomy's family ids and medium ids, for example vinyl or vinyl_12."
        )
    return requested


async def search(
    *,
    pool: Any,
    redis: Any,
    q: str,
    types: list[str],
    genres: list[str],
    year_min: int | None,
    year_max: int | None,
    limit: int,
    offset: int,
    search_fn: SearchFn,
    media: list[str] | None = None,
) -> dict[str, Any]:
    """Run a catalog search, optionally narrowed to a set of media.

    Args:
        media: Family or medium ids from the canonical taxonomy, validated before the query
            runs. They reach the catalog-api search route as repeated ``media`` query
            parameters, which is how that route reads a list. ``None`` and an empty list both
            mean "no media filter", and in that case ``media`` is not passed to ``search_fn``
            at all, so a caller whose search implementation predates the parameter keeps
            working until it opts in.

    Raises:
        ValueError: If ``media`` holds an id the taxonomy does not define.
    """
    arguments: dict[str, Any] = {
        "pool": pool,
        "redis": redis,
        "q": q,
        "types": types,
        "genres": genres,
        "year_min": year_min,
        "year_max": year_max,
        "limit": limit,
        "offset": offset,
    }
    if media is not None:
        validated = validate_media_filter(media)
        if validated:
            arguments["media"] = validated
    return await search_fn(**arguments)


async def get_collaborators(
    *,
    driver: Any,
    artist_id: str,
    limit: int,
    collaborators_fn: CollaboratorsFn,
) -> dict[str, Any]:
    collaborators = await collaborators_fn(driver, artist_id, limit=limit)
    return {"collaborators": collaborators}


async def get_trends(
    *,
    driver: Any,
    entity_type: str,
    name: str,
    handler: TrendsHandler | None,
) -> dict[str, Any]:
    if handler is None:
        return {"error": f"Unknown trends type: {entity_type}"}
    results = await handler(driver, name)
    return {"trends": results}
