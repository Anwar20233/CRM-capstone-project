"""Dependency bundle for the Follow-Up orchestrator graph.

``OrchestratorDeps`` composes the pieces the graph nodes need into one object,
analogous to ``PipelineDeps`` for the extraction graph:

* ``pipeline`` — the Step-1/2/3/5 bundle (CRM reader, calendar reader, the
  repositories incl. ``pending_actions`` / ``runs``, the chat model). Its
  ``crm_orchestrator`` is the write path a deferred write is later routed to.
* ``agents`` — the P2/P3/P4 agents (Step-4 mocks by default, swappable).
* ``profile_service`` — the read path used by ``load_profile``.
* ``content_author`` — the agent's own LLM authors for note/task content (the
  next-step agent decides the steps; the follow-up agent writes the words).
* ``task_registry`` — the follow-up task registry (the hot-loaded known steps).

Nodes receive ``deps`` by closure (``build_followup_graph(deps)``), mirroring
``build_extraction_graph(deps)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from followup.contracts import AgentBundle
from followup.orchestrator.authoring import ContentAuthor
from followup.orchestrator.tasks import (
    FollowupTaskRegistry,
    build_default_task_registry,
)
from followup.profile.dependencies import PipelineDeps
from followup.profile.service import ProfileService


@dataclass
class OrchestratorDeps:
    """Everything the follow-up orchestrator graph needs, assembled once."""

    pipeline: PipelineDeps
    agents: AgentBundle = field(default_factory=AgentBundle)
    profile_service: ProfileService = None  # type: ignore[assignment]
    content_author: ContentAuthor = None  # type: ignore[assignment]
    task_registry: FollowupTaskRegistry = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.profile_service is None:
            self.profile_service = ProfileService(self.pipeline)
        if self.content_author is None:
            self.content_author = ContentAuthor(self)
        if self.task_registry is None:
            # Handlers close over self (incl. content_author), so the registry
            # is built last.
            self.task_registry = build_default_task_registry(self)

    @classmethod
    def create(cls, executor: Any, model: Optional[str] = None) -> "OrchestratorDeps":
        """Build deps with the bridge-backed pipeline + the real subagent bundle.

        Model split: the orchestrator's OWN reasoning (classify, content
        authoring) runs on a smarter model (``FOLLOWUP_ORCHESTRATOR_MODEL``,
        default DeepSeek Flash); the reader + subagents run on the worker model
        (``model`` / ``FOLLOWUP_SUBAGENT_MODEL``, default Qwen). An explicit
        ``model`` argument still overrides the worker model for both.
        """
        import os

        from agent.models import FOLLOWUP_ORCHESTRATOR_MODEL_ALIAS
        from followup.agents.bundle import build_agent_bundle, subagent_model

        orchestrator_model = (
            os.environ.get("FOLLOWUP_ORCHESTRATOR_MODEL")
            or os.environ.get("ORCHESTRATOR_MODEL")
            or (
                os.environ.get("LLM_MODEL", "gpt-4o-mini")
                if os.environ.get("LLM_PROVIDER", "").lower() == "openai"
                else FOLLOWUP_ORCHESTRATOR_MODEL_ALIAS
            )
        )
        worker_model = model or subagent_model()
        return cls(
            pipeline=PipelineDeps.create(
                executor, model=worker_model, chat_model=orchestrator_model
            ),
            agents=build_agent_bundle(),
        )


__all__ = ["OrchestratorDeps"]
