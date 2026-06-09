"""Agent Scope — which sub-agents an agent is allowed to discover and call.

This is the agent-layer parallel to ``tool_scope.py``.  Where ``ToolScope``
classifies *tools* by capability (read/write/meta), an ``AgentScope`` is a plain
allow-list of *sub-agent* names — there are only a handful of agents and their
names are arbitrary, so prefix classification would be over-engineering.

The ``scope`` argument is what makes the agent-discovery abstraction reusable:
the top-level orchestrator gets ``ORCHESTRATOR_SCOPE`` (sees reader/writer/
followup/researcher), but a future Follow-up agent that owns its *own*
sub-agents would build ``build_agent_tools(its_registry, FOLLOWUP_SCOPE)`` and
only discover its children.  Same abstraction, different scope.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentScope:
    """Declares which sub-agents an agent may discover and delegate to."""

    name: str
    allowed_agents: frozenset[str]


def is_agent_allowed(agent_name: str, scope: AgentScope) -> bool:
    """Return ``True`` if *agent_name* may be delegated to within *scope*.

    Matching is case-insensitive so a model emitting "Reader" still resolves to
    the registered ``reader``.
    """
    allowed_lower = {name.lower() for name in scope.allowed_agents}
    return agent_name.lower() in allowed_lower


# ---------------------------------------------------------------------------
# Pre-built scope
# ---------------------------------------------------------------------------

# The top-level chat orchestrator: sees all four sub-agents (reader and writer
# are real; followup and researcher are stubs for now).
ORCHESTRATOR_SCOPE = AgentScope(
    name="orchestrator",
    allowed_agents=frozenset({"reader", "writer", "followup", "researcher"}),
)
