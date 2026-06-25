"""Helpers for building bridge find-tool argument dicts.

Twenty's find executor destructures args as ``{ limit, offset, orderBy, ...filter }``.
Filter fields must be top-level — never wrapped in a ``filter`` key.
"""

from __future__ import annotations

from typing import Any


def find_tool_args(
    *filter_dicts: dict[str, Any],
    limit: int | None = None,
    offset: int | None = None,
    order_by: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge filter fields with pagination/sort into a flat find-tool args dict."""
    args: dict[str, Any] = {}
    for filter_dict in filter_dicts:
        args.update(filter_dict)
    if limit is not None:
        args["limit"] = limit
    if offset is not None:
        args["offset"] = offset
    if order_by is not None:
        args["orderBy"] = order_by
    return args
