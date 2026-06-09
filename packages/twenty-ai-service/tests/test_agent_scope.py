"""Tests for the agent scope allow-list (agent/agent_scope.py)."""

from agent.agent_scope import AgentScope, ORCHESTRATOR_SCOPE, is_agent_allowed


class TestAgentScope:
    def test_orchestrator_sees_all_four_subagents(self) -> None:
        for name in ("reader", "writer", "followup", "researcher"):
            assert is_agent_allowed(name, ORCHESTRATOR_SCOPE)

    def test_unknown_agent_not_allowed(self) -> None:
        assert not is_agent_allowed("hacker", ORCHESTRATOR_SCOPE)

    def test_scope_restricts_to_its_allow_list(self) -> None:
        scope = AgentScope(name="reader-only", allowed_agents=frozenset({"reader"}))
        assert is_agent_allowed("reader", scope)
        assert not is_agent_allowed("writer", scope)
