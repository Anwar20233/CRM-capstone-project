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

__all__ = [
    "build_agent_bundle",
    "subagent_model",
    "use_mock_agents",
    "OrchestratorNextStepAgent",
    "OrchestratorDraftingAgent",
]


def __getattr__(name: str):
    if name in {"build_agent_bundle", "subagent_model", "use_mock_agents"}:
        from followup.agents.bundle import (
            build_agent_bundle,
            subagent_model,
            use_mock_agents,
        )

        return {
            "build_agent_bundle": build_agent_bundle,
            "subagent_model": subagent_model,
            "use_mock_agents": use_mock_agents,
        }[name]

    if name == "OrchestratorDraftingAgent":
        from followup.agents.drafting_adapter import OrchestratorDraftingAgent

        return OrchestratorDraftingAgent

    if name == "OrchestratorNextStepAgent":
        from followup.agents.next_step_adapter import OrchestratorNextStepAgent

        return OrchestratorNextStepAgent

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
