"""Tests for common.agent_tools.discovery (search, collaborators, trends)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_search_delegates() -> None:
    from common.agent_tools.discovery import search

    executor = AsyncMock(return_value={"results": [{"id": "1", "name": "Kraftwerk"}]})
    result = await search(
        pool=object(),
        redis=object(),
        q="Kraftwerk",
        types=["artist"],
        genres=[],
        year_min=None,
        year_max=None,
        limit=5,
        offset=0,
        search_fn=executor,
    )
    assert result == {"results": [{"id": "1", "name": "Kraftwerk"}]}
    executor.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_collaborators_wraps_list() -> None:
    from common.agent_tools.discovery import get_collaborators

    fn = AsyncMock(return_value=[{"id": "2"}])
    result = await get_collaborators(driver=object(), artist_id="1", limit=10, collaborators_fn=fn)
    assert result == {"collaborators": [{"id": "2"}]}


@pytest.mark.asyncio
async def test_get_trends_dispatches_by_type() -> None:
    from common.agent_tools.discovery import get_trends

    handler = AsyncMock(return_value=[{"year": 2025, "count": 10}])
    result = await get_trends(
        driver=object(),
        entity_type="artist",
        name="Kraftwerk",
        handler=handler,
    )
    assert result == {"trends": [{"year": 2025, "count": 10}]}


@pytest.mark.asyncio
async def test_get_trends_missing_handler_errors() -> None:
    from common.agent_tools.discovery import get_trends

    result = await get_trends(
        driver=object(),
        entity_type="artist",
        name="Kraftwerk",
        handler=None,
    )
    assert result == {"error": "Unknown trends type: artist"}


@pytest.mark.asyncio
async def test_search_forwards_a_validated_media_filter() -> None:
    """The catalog-api route reads a list as repeated `media` query parameters."""
    from common.agent_tools.discovery import search

    executor = AsyncMock(return_value={"results": []})
    await search(
        pool=object(),
        redis=object(),
        q="Kraftwerk",
        types=["release"],
        genres=[],
        year_min=None,
        year_max=None,
        limit=5,
        offset=0,
        search_fn=executor,
        media=["vinyl", "optical_cd"],
    )

    assert executor.await_args.kwargs["media"] == ["vinyl", "optical_cd"]


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [None, []])
async def test_search_omits_media_entirely_when_no_filter_was_given(media: list[str] | None) -> None:
    """A consumer whose search implementation predates the parameter must keep working."""
    from common.agent_tools.discovery import search

    executor = AsyncMock(return_value={"results": []})
    await search(
        pool=object(),
        redis=object(),
        q="Kraftwerk",
        types=["release"],
        genres=[],
        year_min=None,
        year_max=None,
        limit=5,
        offset=0,
        search_fn=executor,
        media=media,
    )

    assert "media" not in executor.await_args.kwargs


@pytest.mark.asyncio
async def test_search_rejects_media_ids_the_taxonomy_does_not_define() -> None:
    from common.agent_tools.discovery import search

    executor = AsyncMock(return_value={"results": []})
    with pytest.raises(ValueError, match="unknown media ids: laserdisk, vinyl_13") as error:
        await search(
            pool=object(),
            redis=object(),
            q="Kraftwerk",
            types=["release"],
            genres=[],
            year_min=None,
            year_max=None,
            limit=5,
            offset=0,
            search_fn=executor,
            media=["vinyl_13", "vinyl", "laserdisk"],
        )

    assert "vinyl_12" in str(error.value) or "vinyl" in str(error.value)
    executor.assert_not_awaited()


def test_validate_media_filter_accepts_every_family_and_medium_id() -> None:
    from common.agent_tools.discovery import validate_media_filter
    from common.media import family_ids, medium_ids

    ids = [*family_ids(), *medium_ids()]

    assert validate_media_filter(ids) == ids
    assert validate_media_filter([]) == []


def test_validate_media_filter_reports_each_unknown_id_once_and_sorted() -> None:
    from common.agent_tools.discovery import validate_media_filter

    with pytest.raises(ValueError, match=r"^unknown media ids: betamax, minidisk\."):
        validate_media_filter(["minidisk", "betamax", "minidisk"])
