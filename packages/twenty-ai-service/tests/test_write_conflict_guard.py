"""Tests for the writer graph's deterministic conflict guardrail.

The conflict_check node is structural enforcement inside the writer graph — it
runs for every write tool call BEFORE the bridge, independent of the LLM or its
system prompt. The headline case: a deal close date the model resolved to a past
year (the "7 July" → "2023" bug) must be blocked, never written.

All pure data — no LLM, no bridge, no DB.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.workers import write_gate
from agent.workers.write_gate import conflict_check_node, has_blocking_conflict


def _execute_call(tool: str, tool_args: dict, call_id: str = "tc1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "execute_tool",
                "type": "tool_call",
                "args": {"tool": tool, "tool_args": tool_args},
            }
        ],
    )


async def test_past_close_date_is_blocked() -> None:
    state = {"messages": [_execute_call("update_opportunity", {"id": "o1", "closeDate": "2023-07-07"})]}
    out = await conflict_check_node(state)

    rejections = out.get("messages", [])
    assert len(rejections) == 1
    assert isinstance(rejections[0], ToolMessage)
    assert "WRITE_BLOCKED" in rejections[0].content
    # The guard routes the turn back to the LLM to correct the value.
    assert has_blocking_conflict({"messages": state["messages"] + rejections})


async def test_future_close_date_passes() -> None:
    future = (date.today() + timedelta(days=30)).isoformat()
    state = {"messages": [_execute_call("update_opportunity", {"id": "o1", "closeDate": future})]}
    out = await conflict_check_node(state)

    # No rejection injected → the graph proceeds to the gate / tools.
    assert out == {}
    assert not has_blocking_conflict(state)


async def test_non_write_calls_pass_through() -> None:
    # A read/meta call carries no proposed writes — the guard is a no-op.
    state = {"messages": [_execute_call("find_opportunities", {"limit": 5})]}
    assert await conflict_check_node(state) == {}


async def test_one_bad_write_cancels_the_sibling_in_the_same_turn() -> None:
    # Two writes in one AIMessage; one has a past date. Every pending tool_call
    # must get a ToolMessage (LangGraph requires one per id), the bad one
    # WRITE_BLOCKED and the other CANCELLED — nothing reaches the bridge.
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "bad",
                "name": "execute_tool",
                "type": "tool_call",
                "args": {"tool": "update_opportunity", "tool_args": {"id": "o1", "closeDate": "2020-01-01"}},
            },
            {
                "id": "ok",
                "name": "execute_tool",
                "type": "tool_call",
                "args": {"tool": "create_note", "tool_args": {"content": "hi"}},
            },
        ],
    )
    out = await conflict_check_node({"messages": [msg]})
    rejections = {m.tool_call_id: m.content for m in out["messages"]}

    assert "WRITE_BLOCKED" in rejections["bad"]
    assert "CANCELLED" in rejections["ok"]


async def test_order_of_magnitude_value_change_is_blocked(monkeypatch) -> None:
    # The reported bug: "budget to 1000" became $1,000,000 against an 85,000 deal.
    # With current state fetched, an 11x jump must be flagged before the bridge.
    async def _fake_current(action: str, record_id: str) -> dict:
        return {"amount": {"amountMicros": 85000000000, "currencyCode": "USD"}}

    monkeypatch.setattr(write_gate, "_fetch_current_record", _fake_current)
    state = {
        "messages": [
            _execute_call(
                "update_opportunity",
                {"id": "o1", "amount": {"amountMicros": 1000000000000, "currencyCode": "USD"}},
            )
        ]
    }
    out = await conflict_check_node(state)
    assert out.get("messages")
    assert "WRITE_BLOCKED" in out["messages"][0].content
    assert "magnitude" in out["messages"][0].content or "x increase" in out["messages"][0].content


async def test_modest_value_change_passes(monkeypatch) -> None:
    async def _fake_current(action: str, record_id: str) -> dict:
        return {"amount": {"amountMicros": 85000000000, "currencyCode": "USD"}}

    monkeypatch.setattr(write_gate, "_fetch_current_record", _fake_current)
    state = {
        "messages": [
            _execute_call(
                "update_opportunity",
                {"id": "o1", "amount": {"amountMicros": 95000000000, "currencyCode": "USD"}},
            )
        ]
    }
    assert await conflict_check_node(state) == {}


async def test_create_has_no_baseline_so_value_rules_skip(monkeypatch) -> None:
    # A create has no current state — magnitude can't apply, and we must not
    # fetch (no id). Only value-free rules (past date) could fire.
    async def _boom(action: str, record_id: str) -> dict:  # pragma: no cover
        raise AssertionError("create must not trigger a current-state read")

    monkeypatch.setattr(write_gate, "_fetch_current_record", _boom)
    state = {"messages": [_execute_call("create_opportunity", {"name": "Big", "amount": {"amountMicros": 999999999999}})]}
    assert await conflict_check_node(state) == {}


async def test_unverifiable_numeric_change_is_blocked_when_read_fails(monkeypatch) -> None:
    # The silent-failure bug: if current state can't be read, a numeric change
    # must NOT slip through unchecked — it is blocked loudly instead.
    async def _fail(action: str, record_id: str) -> dict:
        raise write_gate._ReadFailed("reader role not permitted")

    monkeypatch.setattr(write_gate, "_fetch_current_record", _fail)
    state = {
        "messages": [
            _execute_call(
                "update_opportunity",
                {"id": "o1", "amount": {"amountMicros": 1000000, "currencyCode": "USD"}},
            )
        ]
    }
    out = await conflict_check_node(state)
    assert out.get("messages")
    assert "WRITE_BLOCKED" in out["messages"][0].content
    assert "could not read" in out["messages"][0].content


async def test_non_numeric_edit_passes_even_when_read_fails(monkeypatch) -> None:
    # A text/name edit isn't value-sensitive — a read failure must not block it.
    async def _fail(action: str, record_id: str) -> dict:
        raise write_gate._ReadFailed("reader role not permitted")

    monkeypatch.setattr(write_gate, "_fetch_current_record", _fail)
    state = {"messages": [_execute_call("update_opportunity", {"id": "o1", "name": "Renamed Deal"})]}
    assert await conflict_check_node(state) == {}


async def test_non_ai_last_message_is_ignored() -> None:
    assert await conflict_check_node({"messages": [HumanMessage(content="hi")]}) == {}
    assert await conflict_check_node({"messages": []}) == {}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
