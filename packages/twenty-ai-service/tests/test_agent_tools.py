"""Tests for the agent-discovery meta-tools (agent/agent_tools.py).

Verifies (mirroring tests/test_crm_tools.py for the tool layer):
- get_agent_catalog returns only in-scope agents.
- learn_agent returns interaction schemas; errors OUT_OF_SCOPE for disallowed names.
- delegate_to_agent invokes the sub-agent and wraps the result in {ok, data}.
- delegate_to_agent errors UNKNOWN_AGENT for a missing agent.
- The scope guard fires BEFORE invoke (the sub-agent is never called).
- A sub-agent exception becomes a recoverable AGENT_FAILED result.
"""

from unittest.mock import AsyncMock

import pytest

from agent.agent_registry import AgentRegistry, AgentSpec
from agent.agent_scope import AgentScope
from agent.agent_tools import build_agent_tools


def _get_tool(tools, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool '{name}' not found in {[t.name for t in tools]}")


def _spec(name: str, invoke) -> AgentSpec:
    return AgentSpec(
        name=name,
        role=f"{name} role",
        when_to_use=f"{name} when_to_use",
        description=f"{name} description",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        invoke=invoke,
    )


@pytest.fixture
def registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(_spec("reader", AsyncMock(return_value={"response": "r"})))
    reg.register(_spec("writer", AsyncMock(return_value={"response": "w"})))
    return reg


# Scope that only allows the reader, to exercise out-of-scope behaviour.
READER_ONLY = AgentScope(name="reader-only", allowed_agents=frozenset({"reader"}))
BOTH = AgentScope(name="both", allowed_agents=frozenset({"reader", "writer"}))


class TestCatalog:
    @pytest.mark.asyncio
    async def test_catalog_shows_only_in_scope(self, registry: AgentRegistry) -> None:
        catalog = _get_tool(build_agent_tools(registry, READER_ONLY), "get_agent_catalog")
        result = await catalog.ainvoke({})
        names = {a["name"] for a in result["data"]["agents"]}
        assert names == {"reader"}

    @pytest.mark.asyncio
    async def test_catalog_entries_are_lightweight(self, registry: AgentRegistry) -> None:
        catalog = _get_tool(build_agent_tools(registry, BOTH), "get_agent_catalog")
        result = await catalog.ainvoke({})
        entry = result["data"]["agents"][0]
        # Lightweight routing view — name/role/when_to_use, but no schemas.
        assert set(entry) == {"name", "role", "when_to_use"}


class TestLearn:
    @pytest.mark.asyncio
    async def test_learn_returns_schemas(self, registry: AgentRegistry) -> None:
        learn = _get_tool(build_agent_tools(registry, BOTH), "learn_agent")
        result = await learn.ainvoke({"agent_names": ["reader"]})
        entry = result["data"]["agents"][0]
        assert entry["input_schema"] == {"type": "object"}
        assert "output_schema" in entry

    @pytest.mark.asyncio
    async def test_learn_rejects_out_of_scope(self, registry: AgentRegistry) -> None:
        learn = _get_tool(build_agent_tools(registry, READER_ONLY), "learn_agent")
        result = await learn.ainvoke({"agent_names": ["writer"]})
        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"


class TestDelegate:
    @pytest.mark.asyncio
    async def test_delegate_invokes_and_wraps(self, registry: AgentRegistry) -> None:
        delegate = _get_tool(build_agent_tools(registry, BOTH), "delegate_to_agent")
        result = await delegate.ainvoke({"agent": "reader", "instruction": "find John"})
        assert result["ok"] is True
        assert result["data"] == {"response": "r"}
        registry.get("reader").invoke.assert_awaited_once_with("find John")

    @pytest.mark.asyncio
    async def test_delegate_is_case_insensitive(self, registry: AgentRegistry) -> None:
        """A model emitting 'Reader' must still resolve to the registered reader."""
        delegate = _get_tool(build_agent_tools(registry, BOTH), "delegate_to_agent")
        result = await delegate.ainvoke({"agent": "Reader", "instruction": "find John"})
        assert result["ok"] is True
        registry.get("reader").invoke.assert_awaited_once_with("find John")

    @pytest.mark.asyncio
    async def test_learn_is_case_insensitive(self, registry: AgentRegistry) -> None:
        learn = _get_tool(build_agent_tools(registry, BOTH), "learn_agent")
        result = await learn.ainvoke({"agent_names": ["Reader", "WRITER"]})
        names = {a["name"] for a in result["data"]["agents"]}
        assert names == {"reader", "writer"}

    @pytest.mark.asyncio
    async def test_delegate_unknown_agent(self, registry: AgentRegistry) -> None:
        delegate = _get_tool(build_agent_tools(registry, BOTH), "delegate_to_agent")
        # Allow it in scope but don't register it, to hit UNKNOWN_AGENT.
        scope = AgentScope(name="s", allowed_agents=frozenset({"ghost"}))
        delegate = _get_tool(build_agent_tools(registry, scope), "delegate_to_agent")
        result = await delegate.ainvoke({"agent": "ghost", "instruction": "x"})
        assert result["ok"] is False
        assert result["error"]["code"] == "UNKNOWN_AGENT"

    @pytest.mark.asyncio
    async def test_scope_guard_fires_before_invoke(self, registry: AgentRegistry) -> None:
        delegate = _get_tool(build_agent_tools(registry, READER_ONLY), "delegate_to_agent")
        result = await delegate.ainvoke({"agent": "writer", "instruction": "x"})
        assert result["ok"] is False
        assert result["error"]["code"] == "OUT_OF_SCOPE"
        # The out-of-scope sub-agent must never have been invoked.
        registry.get("writer").invoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subagent_failure_is_recoverable(self) -> None:
        reg = AgentRegistry()
        reg.register(_spec("reader", AsyncMock(side_effect=RuntimeError("boom"))))
        delegate = _get_tool(build_agent_tools(reg, BOTH), "delegate_to_agent")
        result = await delegate.ainvoke({"agent": "reader", "instruction": "x"})
        assert result["ok"] is False
        assert result["error"]["code"] == "AGENT_FAILED"
        assert "boom" in result["error"]["message"]
