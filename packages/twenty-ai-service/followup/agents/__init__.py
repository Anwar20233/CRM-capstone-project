"""Anti-corruption adapter layer wiring the orchestrator to the real subagents.

The orchestrator (``followup/orchestrator``) is the ground-truth coordinator and
talks to subagents through the Protocols in ``followup/contracts``. The
next-step and drafting agents were built independently with their own context
and result shapes. This package adapts them to the orchestrator's contracts so
they can be swapped in without touching either side:

* :func:`build_agent_bundle` — the real bundle the orchestrator runs with.
* :class:`OrchestratorNextStepAgent` / :class:`OrchestratorDraftingAgent` — the
  adapters themselves.
"""

from __future__ import annotations

from followup.agents.bundle import build_agent_bundle, subagent_model, use_mock_agents
from followup.agents.drafting_adapter import OrchestratorDraftingAgent
from followup.agents.next_step_adapter import OrchestratorNextStepAgent

__all__ = [
    "build_agent_bundle",
    "subagent_model",
    "use_mock_agents",
    "OrchestratorNextStepAgent",
    "OrchestratorDraftingAgent",
]
