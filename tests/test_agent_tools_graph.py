"""Tests for common.agent_tools.graph."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_find_path_resolves_names_and_returns_path() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(side_effect=[{"id": "42"}, {"id": "99"}])
    find_shortest_path = AsyncMock(return_value={"path": [42, 99], "length": 1})

    result = await find_path(
        driver=driver,
        from_name="Kraftwerk",
        from_type="artist",
        to_name="Afrika Bambaataa",
        to_type="artist",
        max_depth=6,
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
    )

    assert result == {"path": [42, 99], "length": 1}
    assert resolve_name.await_count == 2
    find_shortest_path.assert_awaited_once_with(
        driver=driver,
        from_id="42",
        to_id="99",
        max_depth=6,
        from_type="artist",
        to_type="artist",
    )


@pytest.mark.asyncio
async def test_find_path_returns_error_when_source_missing() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(return_value=None)
    find_shortest_path = AsyncMock()

    result = await find_path(
        driver=driver,
        from_name="Nobody",
        from_type="artist",
        to_name="Kraftwerk",
        to_type="artist",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
    )

    assert result == {"error": "artist 'Nobody' not found"}
    find_shortest_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_path_returns_error_when_target_missing() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(side_effect=[{"id": "42"}, None])
    find_shortest_path = AsyncMock()

    result = await find_path(
        driver=driver,
        from_name="Kraftwerk",
        from_type="artist",
        to_name="Nobody",
        to_type="artist",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
    )

    assert result == {"error": "artist 'Nobody' not found"}
    find_shortest_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_path_returns_error_when_no_path_between_entities() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(side_effect=[{"id": "42"}, {"id": "99"}])
    find_shortest_path = AsyncMock(return_value=None)

    result = await find_path(
        driver=driver,
        from_name="Kraftwerk",
        from_type="artist",
        to_name="Afrika Bambaataa",
        to_type="artist",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
    )

    assert result == {"error": "No path found between the specified entities"}
    find_shortest_path.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_path_resolves_both_label_ends_by_id() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock()
    resolve_label_id = AsyncMock(side_effect=[{"id": "1001"}, {"id": "2002"}])
    find_shortest_path = AsyncMock(return_value={"path": [1001, 2002], "length": 1})

    result = await find_path(
        driver=driver,
        from_name="Same Name",
        from_type="label",
        to_name="Same Name",
        to_type="label",
        max_depth=4,
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
        from_label_id="1001",
        to_label_id="2002",
        resolve_label_id=resolve_label_id,
    )

    assert result == {"path": [1001, 2002], "length": 1}
    assert resolve_label_id.await_args_list == [((driver, "1001"),), ((driver, "2002"),)]
    resolve_name.assert_not_awaited()
    find_shortest_path.assert_awaited_once_with(
        driver=driver,
        from_id="1001",
        to_id="2002",
        max_depth=4,
        from_type="label",
        to_type="label",
    )


@pytest.mark.asyncio
async def test_find_path_mixes_label_id_end_with_name_end() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(return_value={"id": "42"})
    resolve_label_id = AsyncMock(return_value={"id": "1001"})
    find_shortest_path = AsyncMock(return_value={"path": [42, 1001], "length": 1})

    result = await find_path(
        driver=driver,
        from_name="Kraftwerk",
        from_type="artist",
        to_name="Same Name",
        to_type="label",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
        to_label_id="1001",
        resolve_label_id=resolve_label_id,
    )

    assert result == {"path": [42, 1001], "length": 1}
    resolve_name.assert_awaited_once_with(driver, "Kraftwerk", "artist")
    resolve_label_id.assert_awaited_once_with(driver, "1001")
    find_shortest_path.assert_awaited_once_with(
        driver=driver,
        from_id="42",
        to_id="1001",
        max_depth=6,
        from_type="artist",
        to_type="label",
    )


@pytest.mark.asyncio
async def test_find_path_mixes_name_end_with_label_id_end_from_side() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(return_value={"id": "1001"})
    resolve_label_id = AsyncMock(return_value={"id": "42"})
    find_shortest_path = AsyncMock(return_value={"path": [1001, 42], "length": 1})

    await find_path(
        driver=driver,
        from_name="Same Name",
        from_type="label",
        to_name="Kraftwerk",
        to_type="artist",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
        from_label_id="1001",
        resolve_label_id=resolve_label_id,
    )

    resolve_label_id.assert_awaited_once_with(driver, "1001")
    resolve_name.assert_awaited_once_with(driver, "Kraftwerk", "artist")


@pytest.mark.asyncio
async def test_find_path_ignores_id_on_non_label_end() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(side_effect=[{"id": "42"}, {"id": "99"}])
    resolve_label_id = AsyncMock()
    find_shortest_path = AsyncMock(return_value={"path": [42, 99], "length": 1})

    await find_path(
        driver=driver,
        from_name="Kraftwerk",
        from_type="artist",
        to_name="Afrika Bambaataa",
        to_type="artist",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
        from_label_id="ignored",
        to_label_id="ignored-too",
        resolve_label_id=resolve_label_id,
    )

    resolve_label_id.assert_not_awaited()
    assert resolve_name.await_args_list == [((driver, "Kraftwerk", "artist"),), ((driver, "Afrika Bambaataa", "artist"),)]


@pytest.mark.asyncio
async def test_find_path_without_ids_is_backward_compatible() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(side_effect=[{"id": "1001"}, {"id": "2002"}])
    resolve_label_id = AsyncMock()
    find_shortest_path = AsyncMock(return_value={"path": [1001, 2002], "length": 1})

    result = await find_path(
        driver=driver,
        from_name="Warp",
        from_type="label",
        to_name="Ninja Tune",
        to_type="label",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
    )

    assert result == {"path": [1001, 2002], "length": 1}
    assert resolve_name.await_args_list == [((driver, "Warp", "label"),), ((driver, "Ninja Tune", "label"),)]
    resolve_label_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_path_reports_unknown_label_id_by_id() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock(return_value={"id": "42"})
    resolve_label_id = AsyncMock(return_value=None)
    find_shortest_path = AsyncMock()

    result = await find_path(
        driver=driver,
        from_name="Kraftwerk",
        from_type="artist",
        to_name="Same Name",
        to_type="label",
        resolve_name=resolve_name,
        find_shortest_path_fn=find_shortest_path,
        to_label_id="9999",
        resolve_label_id=resolve_label_id,
    )

    assert result == {"error": "label id '9999' not found"}
    find_shortest_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_path_label_id_without_resolver_raises() -> None:
    from common.agent_tools.graph import find_path

    driver = AsyncMock()
    resolve_name = AsyncMock()
    find_shortest_path = AsyncMock()

    with pytest.raises(ValueError, match="resolve_label_id is required"):
        await find_path(
            driver=driver,
            from_name="Same Name",
            from_type="label",
            to_name="Kraftwerk",
            to_type="artist",
            resolve_name=resolve_name,
            find_shortest_path_fn=find_shortest_path,
            from_label_id="1001",
        )

    resolve_name.assert_not_awaited()
    find_shortest_path.assert_not_awaited()
