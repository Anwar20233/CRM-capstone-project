"""Follow-Up orchestrator (Step 6 + Step 7) — the LangGraph pipeline.

Takes a trigger (email / risk / orchestrator), loads the opportunity profile,
decides what to do, runs the recommended follow-up tasks (the hot-loaded known
steps), and persists a pending action for the rep to review. Step 7 adds the
accept graph (accept → execute → update_profile → END) and the REST surface.

Public surface:

* ``build_followup_graph`` — compile the pipeline bound to ``OrchestratorDeps``.
* ``build_accept_graph`` — compile the accept pipeline bound to ``OrchestratorDeps``.
* ``AcceptState`` — the accept graph's state type.
* ``OrchestratorDeps`` — the dependency bundle (+ ``create()``).
* ``FollowUpState`` / ``EntryPoint`` — the graph state + entry-point literal.
* ``FollowupTaskRegistry`` / ``FOLLOWUP_SCOPE`` / ``build_task_tools`` /
  ``build_default_task_registry`` — the follow-up task catalog (known steps).
"""

from __future__ import annotations

from followup.orchestrator.deps import OrchestratorDeps
from followup.orchestrator.graph import AcceptState, build_accept_graph, build_followup_graph
from followup.orchestrator.routing import STEP_PREP, prep_tasks_for_plan
from followup.orchestrator.state import EntryPoint, FollowUpState, RunStatus
from followup.orchestrator.tasks import (
    FOLLOWUP_SCOPE,
    FollowupTaskRegistry,
    FollowupTaskScope,
    FollowupTaskSpec,
    TaskContext,
    build_default_task_registry,
    build_task_tools,
)

__all__ = [
    "build_followup_graph",
    "build_accept_graph",
    "AcceptState",
    "OrchestratorDeps",
    "FollowUpState",
    "EntryPoint",
    "RunStatus",
    "FollowupTaskRegistry",
    "FollowupTaskScope",
    "FollowupTaskSpec",
    "FOLLOWUP_SCOPE",
    "TaskContext",
    "build_task_tools",
    "build_default_task_registry",
    "STEP_PREP",
    "prep_tasks_for_plan",
]
