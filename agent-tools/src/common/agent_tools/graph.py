"""GrooveMap graph data tools — find_path, collaborators, stats."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


ResolveNameFn = Callable[[Any, str, str], Awaitable[dict[str, Any] | None]]
ResolveLabelIdFn = Callable[[Any, str], Awaitable[dict[str, Any] | None]]
FindShortestPathFn = Callable[..., Awaitable[dict[str, Any] | None]]


async def find_path(
    *,
    driver: Any,
    from_name: str,
    from_type: str,
    to_name: str,
    to_type: str,
    max_depth: int = 6,
    resolve_name: ResolveNameFn,
    find_shortest_path_fn: FindShortestPathFn,
    from_label_id: str | None = None,
    to_label_id: str | None = None,
    resolve_label_id: ResolveLabelIdFn | None = None,
) -> dict[str, Any]:
    """Find the shortest path between two entities by name, or by label id.

    The caller injects ``resolve_name`` and ``find_shortest_path_fn`` so this
    module has zero coupling to ``api.queries``. The NLQ engine and the MCP
    server each pass their own resolver bound to the same shared implementation.

    ``from_label_id`` / ``to_label_id`` are optional. When one is set and that
    end's type is ``"label"``, the end is resolved by id through
    ``resolve_label_id(driver, label_id)`` instead of by name, so a label whose
    name is shared with other labels can be picked exactly. An id on a non-label
    end is ignored and that end resolves by name. Calls that pass no ids behave
    exactly as before and never touch ``resolve_label_id``. Passing an id on a
    label end without ``resolve_label_id`` raises ``ValueError``.
    """
    from_node = await _resolve_end(
        driver=driver,
        name=from_name,
        entity_type=from_type,
        label_id=from_label_id,
        resolve_name=resolve_name,
        resolve_label_id=resolve_label_id,
    )
    if from_node is None:
        return {"error": _not_found_message(from_type, from_name, from_label_id)}
    to_node = await _resolve_end(
        driver=driver,
        name=to_name,
        entity_type=to_type,
        label_id=to_label_id,
        resolve_name=resolve_name,
        resolve_label_id=resolve_label_id,
    )
    if to_node is None:
        return {"error": _not_found_message(to_type, to_name, to_label_id)}

    result = await find_shortest_path_fn(
        driver=driver,
        from_id=str(from_node["id"]),
        to_id=str(to_node["id"]),
        max_depth=max_depth,
        from_type=from_type,
        to_type=to_type,
    )
    if result is None:
        return {"error": "No path found between the specified entities"}
    return result


async def _resolve_end(
    *,
    driver: Any,
    name: str,
    entity_type: str,
    label_id: str | None,
    resolve_name: ResolveNameFn,
    resolve_label_id: ResolveLabelIdFn | None,
) -> dict[str, Any] | None:
    """Resolve one end of the path: by id for a label with an id, otherwise by name."""
    if label_id and entity_type == "label":
        if resolve_label_id is None:
            msg = "resolve_label_id is required when a label id is passed"
            raise ValueError(msg)
        return await resolve_label_id(driver, label_id)
    return await resolve_name(driver, name, entity_type)


def _not_found_message(entity_type: str, name: str, label_id: str | None) -> str:
    """Error text for an unresolved end; it names the id when the end was resolved by id."""
    if label_id and entity_type == "label":
        return f"label id '{label_id}' not found"
    return f"{entity_type} '{name}' not found"
