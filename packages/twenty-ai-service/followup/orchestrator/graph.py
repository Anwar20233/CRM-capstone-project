"""LangGraph wiring for the Follow-Up orchestrator.

    START → route_entry ──email──→ extract ─┐
                        └─else──────────────┴→ load_profile → assess_risk
            ──email──────────────────→ plan → run_tasks → create_pending → END
            └─risk/orchestrator─→ classify ─┘

    assess_risk re-scores from the just-extracted facts (every run). The email
    path then goes straight to plan — the next-step agent reads its own playbook
    and decides from the raw trigger, so there is no LLM email triage. The
    risk/orchestrator paths pass through classify first (deterministic labelling).
    plan calls the next-step agent (email) or synthesizes a plan
    (risk/orchestrator); run_tasks follows the plan's steps; create_pending
    persists one bundled pending action.

Accept graph (Step 7):

    START → accept → execute → update_profile → END

``build_followup_graph(deps)`` compiles a graph whose nodes close over ``deps``
(mirrors ``build_extraction_graph(deps)``).
``build_accept_graph(deps)`` compiles the linear accept pipeline.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from followup.orchestrator.deps import OrchestratorDeps
from followup.orchestrator.nodes import build_nodes
from followup.orchestrator.routing import route_entry
from followup.orchestrator.state import FollowUpState
from followup.store.repositories import PendingAction

logger = logging.getLogger(__name__)


# ===========================================================================
# AcceptState — dedicated state for the accept graph (no entry_point /
# classification, just action acceptance + execution + profile logging).
# ===========================================================================


class AcceptState(TypedDict, total=False):
    action_id: str
    user_id: str
    workspace_id: str
    opportunity_id: str
    disabled_step_indices: list[int]  # steps the rep toggled off before accepting
    pending_action: Optional[PendingAction]
    status: str  # running | completed | failed
    error: Optional[str]


# ===========================================================================
# Follow-up graph (unchanged)
# ===========================================================================


def build_followup_graph(deps: OrchestratorDeps):
    """Compile and return the follow-up StateGraph bound to ``deps``."""
    from langgraph.graph import END, START, StateGraph

    nodes = build_nodes(deps)
    builder = StateGraph(FollowUpState)
    for name, fn in nodes.items():
        builder.add_node(name, fn)

    # Entry: email triggers extraction first; everything else loads the profile.
    builder.add_conditional_edges(
        START,
        route_entry,
        {"extract": "extract", "load_profile": "load_profile"},
    )
    builder.add_edge("extract", "load_profile")

    # After risk scoring: email path skips classify (the next-step agent reads its
    # own stage playbook and decides from the raw trigger — no LLM triage needed);
    # risk/orchestrator paths still go through classify (deterministic labelling).
    builder.add_edge("load_profile", "assess_risk")
    builder.add_conditional_edges(
        "assess_risk",
        lambda state: "plan" if state.get("entry_point") == "email" else "classify",
        {"plan": "plan", "classify": "classify"},
    )
    builder.add_edge("classify", "plan")
    builder.add_edge("plan", "run_tasks")
    builder.add_edge("run_tasks", "create_pending")
    builder.add_edge("create_pending", END)

    return builder.compile(name="followup-pipeline")


# ===========================================================================
# Accept graph (Step 7) — linear: accept → execute → update_profile → END
# ===========================================================================


def build_accept_graph(deps: OrchestratorDeps):
    """Compile the accept pipeline: mark accepted → execute write → log fact."""
    from langgraph.graph import END, START, StateGraph

    async def accept_node(state: AcceptState) -> dict[str, Any]:
        """Mark the action as accepted."""
        try:
            action = await deps.pipeline.pending_actions.get(
                uuid.UUID(state["action_id"])
            )
            if action is None:
                return {"status": "failed", "error": "Action not found"}
            if action.status != "pending":
                return {
                    "status": "failed",
                    "error": f"Action is '{action.status}', not 'pending'",
                }

            action.status = "accepted"
            action.acted_on_at = datetime.now(timezone.utc)
            action.acted_on_by = uuid.UUID(state["user_id"])
            await deps.pipeline.pending_actions.save(action)

            return {"pending_action": action, "status": "running"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("accept node failed")
            return {"status": "failed", "error": f"accept: {exc}"}

    async def execute_node(state: AcceptState) -> dict[str, Any]:
        """Route the action through orchestrator→writer via FollowupActionExecutor."""
        if state.get("status") == "failed":
            return {}
        action: PendingAction = state["pending_action"]
        try:
            from followup.api.execution import FollowupActionExecutor

            executor = FollowupActionExecutor()
            result = await executor.execute(
                action, disabled_step_indices=state.get("disabled_step_indices") or []
            )

            action.execution_status = result["status"]
            action.execution_error = result.get("error")
            action.executed_at = datetime.now(timezone.utc)
            await deps.pipeline.pending_actions.save(action)

            if result["status"] == "failed":
                return {
                    "pending_action": action,
                    "status": "failed",
                    "error": f"execute: {result.get('error')}",
                }
            return {"pending_action": action}
        except Exception as exc:  # noqa: BLE001
            logger.exception("execute node failed")
            action.execution_status = "failed"
            action.execution_error = str(exc)
            action.executed_at = datetime.now(timezone.utc)
            await deps.pipeline.pending_actions.save(action)
            return {
                "pending_action": action,
                "status": "failed",
                "error": f"execute: {exc}",
            }

    async def update_profile_node(state: AcceptState) -> dict[str, Any]:
        """Log the outcome as a commitment fact via ProfileFactRepository."""
        if state.get("status") == "failed":
            return {}
        action: PendingAction = state["pending_action"]
        if action.execution_status != "completed":
            return {"status": "completed"}  # nothing to log
        try:
            from followup.api.execution import _action_to_fact

            fact_data = _action_to_fact(action)
            await deps.pipeline.facts.create(fact_data)
            return {"status": "completed"}
        except Exception as exc:  # noqa: BLE001
            # Profile logging is non-critical — don't fail the accept.
            logger.warning("update_profile failed (non-critical): %s", exc)
            return {"status": "completed"}

    builder = StateGraph(AcceptState)
    builder.add_node("accept", accept_node)
    builder.add_node("execute", execute_node)
    builder.add_node("update_profile", update_profile_node)

    builder.add_edge(START, "accept")
    builder.add_edge("accept", "execute")
    builder.add_edge("execute", "update_profile")
    builder.add_edge("update_profile", END)

    return builder.compile(name="followup-accept")


__all__ = ["build_followup_graph", "build_accept_graph", "AcceptState"]
