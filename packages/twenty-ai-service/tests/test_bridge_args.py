"""Tests for bridge find-tool argument helpers."""

from __future__ import annotations

from agent.tools.bridge_args import find_tool_args


class TestFindToolArgs:
    def test_merges_filter_fields_at_top_level(self) -> None:
        args = find_tool_args({"name": {"ilike": "%Uber%"}}, limit=10)
        assert args == {"name": {"ilike": "%Uber%"}, "limit": 10}
        assert "filter" not in args

    def test_includes_offset_and_order_by(self) -> None:
        args = find_tool_args(
            {"companyId": {"eq": "uuid-1"}},
            limit=50,
            offset=5,
            order_by={"updatedAt": "DescNullsLast"},
        )
        assert args == {
            "companyId": {"eq": "uuid-1"},
            "limit": 50,
            "offset": 5,
            "orderBy": {"updatedAt": "DescNullsLast"},
        }

    def test_merges_multiple_filter_dicts(self) -> None:
        args = find_tool_args(
            {"companyId": {"eq": "uuid-1"}},
            {"stage": {"neq": "CLOSED_LOST"}},
            limit=1,
        )
        assert args["companyId"] == {"eq": "uuid-1"}
        assert args["stage"] == {"neq": "CLOSED_LOST"}
        assert args["limit"] == 1
