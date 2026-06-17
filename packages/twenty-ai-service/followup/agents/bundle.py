"""Factory that assembles the orchestrator's ``AgentBundle`` with the real
subagents wired in.

Real-by-default: ``build_agent_bundle()`` returns the next-step and drafting
adapters (risk stays the mock — no real risk agent exists yet). Set
``FOLLOWUP_USE_MOCK_AGENTS=1`` to get the all-mock bundle (tests that construct
``AgentBundle()`` directly already get mocks and are unaffected).

The subagents run on their own model (``FOLLOWUP_SUBAGENT_MODEL``, default Qwen)
independent of the orchestrator's reasoning model — see ``OrchestratorDeps``.
"""

from __future__ import annotations

import logging
import os

from agent.models import FOLLOWUP_SUBAGENT_MODEL_ALIAS
from followup.contracts import AgentBundle

logger = logging.getLogger(__name__)


def subagent_model() -> str:
    """The model the follow-up subagents run on (env override → Qwen default)."""
    return os.environ.get("FOLLOWUP_SUBAGENT_MODEL") or FOLLOWUP_SUBAGENT_MODEL_ALIAS


def use_mock_agents() -> bool:
    return os.environ.get("FOLLOWUP_USE_MOCK_AGENTS", "").lower() in ("1", "true", "yes")


def build_agent_bundle() -> AgentBundle:
    """Assemble the bundle the orchestrator runs with.

    Real next-step + drafting adapters by default; the all-mock bundle when
    ``FOLLOWUP_USE_MOCK_AGENTS`` is set.
    """
    if use_mock_agents():
        logger.info("follow-up agents: using MOCK bundle (FOLLOWUP_USE_MOCK_AGENTS set)")
        return AgentBundle()

    # Imported lazily so the all-mock path never imports the heavier subagent deps.
    from followup.agents.drafting_adapter import OrchestratorDraftingAgent
    from followup.agents.next_step_adapter import OrchestratorNextStepAgent

    model = subagent_model()
    logger.info("follow-up agents: using REAL bundle (subagent model=%s)", model)
    return AgentBundle(
        next_step=OrchestratorNextStepAgent(model=model),
        drafting=OrchestratorDraftingAgent(model=model),
    )


__all__ = ["build_agent_bundle", "subagent_model", "use_mock_agents"]
