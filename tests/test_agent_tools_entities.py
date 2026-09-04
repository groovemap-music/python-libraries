"""Tests for common.agent_tools.entities."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize(
    "tool_name,entity_type",
    [
        ("get_artist_details", "artist"),
        ("get_label_details", "label"),
        ("get_genre_details", "genre"),
        ("get_style_details", "style"),
        ("get_release_details", "release"),
    ],
)
@pytest.mark.asyncio
async def test_entity_details_delegates_to_handler(tool_name: str, entity_type: str) -> None:
    import common.agent_tools.entities as entities

    driver = AsyncMock()
    handler = AsyncMock(return_value={"id": "1", "name": "Example"})
    tool = getattr(entities, tool_name)

    result = await tool(driver=driver, name="Example", handler=handler)
    assert result == {"id": "1", "name": "Example", "_entity_type": entity_type}
    handler.assert_awaited_once_with(driver, "Example")


@pytest.mark.asyncio
async def test_entity_details_returns_error_when_not_found() -> None:
    from common.agent_tools.entities import get_artist_details

    driver = AsyncMock()
    handler = AsyncMock(return_value=None)
    result = await get_artist_details(driver=driver, name="Nobody", handler=handler)
    assert result == {"error": "artist 'Nobody' not found"}


@pytest.mark.asyncio
async def test_get_release_details_passes_through_the_canonical_media_block() -> None:
    from common.agent_tools.entities import get_release_details, media_of
    from common.media import map_discogs_formats

    block = map_discogs_formats([{"name": "Vinyl", "qty": "2", "descriptions": ["LP", "Album"]}])
    handler = AsyncMock(return_value={"id": "1", "name": "Autobahn", "media": block})

    result = await get_release_details(driver=AsyncMock(), name="Autobahn", handler=handler)

    assert result["media"] == block
    assert media_of(result) == block


def test_media_of_answers_none_for_a_release_stored_before_the_block_existed() -> None:
    from common.agent_tools.entities import media_of

    assert media_of({"id": "1", "name": "Autobahn"}) is None
    assert media_of({"id": "1", "media": "Vinyl"}) is None
