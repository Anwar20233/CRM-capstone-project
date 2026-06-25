"""Agent meta-tools — dynamic sub-agent discovery for the orchestrator.

This is the agent-layer parallel to ``crm_tools.build_crm_tools``.  Where CRM
tools give a worker three meta-tools to discover and run *tools*, these give an
agent three meta-tools to discover and activate *sub-agents*:

    get_tool_catalog  ->  get_agent_catalog   (what sub-agents exist + their roles)
    learn_tools       ->  learn_agent          (how to interact: input/output schema)
    execute_tool      ->  delegate_to_agent    (activate a sub-agent with a task)

The orchestrator's system prompt therefore never hardcodes which sub-agents
exist or how to talk to them — it discovers them at runtime, exactly as it
discovers CRM tools.  The same factory works for any agent that owns
sub-agents (pass a different registry + scope), so the pattern is reusable
across the whole system.

Returned envelopes mirror ``bridge_client.forward`` (``{ok, data}`` /
``{ok, error}``) and reuse the ``OUT_OF_SCOPE`` / ``UNKNOWN_*`` error codes from
``crm_tools`` so the LLM loop and masking layer treat sub-agent results exactly
like tool results.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from agent.agent_registry import AgentRegistry
from agent.agent_scope import AgentScope, is_agent_allowed


def build_agent_tools(
    registry: AgentRegistry, scope: AgentScope
) -> list[StructuredTool]:
    """Build the three agent-discovery meta-tools, closed over (registry, scope)."""

    # -- get_agent_catalog ----------------------------------------------

    async def _get_agent_catalog() -> dict:
        """List the sub-agents you can delegate to (name + role).

        Call learn_agent next to get a sub-agent's interaction schema before
        delegating to it.
        """
        return {"ok": True, "data": {"agents": registry.catalog(scope)}}

    # -- learn_agent -----------------------------------------------------

    async def _learn_agent(agent_names: list[str]) -> dict:
        """Fetch the interaction schema (input/output) for specific sub-agents.

        Always learn a sub-agent before delegating so you send a well-shaped
        instruction. Example: learn_agent(["reader", "writer"]).
        """
        blocked = [n for n in agent_names if not is_agent_allowed(n, scope)]
        if blocked:
            return {
                "ok": False,
                "error": {
                    "code": "OUT_OF_SCOPE",
                    "message": (
                        f"Agents not available in '{scope.name}' scope: "
                        + ", ".join(blocked)
                    ),
                },
            }
        return {"ok": True, "data": {"agents": registry.learn(agent_names, scope)}}

    # -- delegate_to_agent ----------------------------------------------

    async def _delegate_to_agent(agent: str, instruction: str) -> dict:
        """Activate a sub-agent with a single natural-language instruction.

        Discover agents with get_agent_catalog and learn their schemas with
        learn_agent first. Returns the sub-agent's result. Example:
        delegate_to_agent(agent="reader", instruction="Find the person John").
        """
        # Scope guard fires before any work, mirroring crm_tools.execute_tool.
        if not is_agent_allowed(agent, scope):
            return {
                "ok": False,
                "error": {
                    "code": "OUT_OF_SCOPE",
                    "message": f"Agent '{agent}' is not available in '{scope.name}' scope",
                },
            }

        spec = registry.get(agent)
        if spec is None:
            return {
                "ok": False,
                "error": {
                    "code": "UNKNOWN_AGENT",
                    "message": f"No sub-agent named '{agent}' is registered",
                },
            }

        try:
            result = await spec.invoke(instruction)
        except Exception as error:  # noqa: BLE001
            # A sub-agent failure becomes a recoverable result the orchestrator
            # can react to, not a crashed turn (mirrors BaseWorker.invoke_tool).
            return {
                "ok": False,
                "error": {
                    "code": "AGENT_FAILED",
                    "message": f"Sub-agent '{agent}' failed: {error}",
                },
            }

        return {"ok": True, "data": result}

    return [
        StructuredTool.from_function(
            coroutine=_get_agent_catalog, name="get_agent_catalog"
        ),
        StructuredTool.from_function(coroutine=_learn_agent, name="learn_agent"),
        StructuredTool.from_function(
            coroutine=_delegate_to_agent, name="delegate_to_agent"
        ),
    ]
