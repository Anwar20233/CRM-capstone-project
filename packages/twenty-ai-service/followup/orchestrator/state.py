"""Shared state for the Follow-Up orchestrator graph.

``FollowUpState`` is the single object every node reads and partially updates,
in the LangGraph ``TypedDict`` style (mirrors
``followup/profile/graph.py::ExtractionState``). All typed payloads are the real
dataclasses from Steps 3–5 so the state stays strongly typed end to end.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from followup.calendar.availability import CalendarResult
from followup.contracts.drafting import DraftResult
from followup.contracts.next_step import NextStepPlan
from followup.contracts.risk import RiskAssessment
from followup.profile.schemas import DealContext

# The three triggers that activate the follow-up agent. There is no CRM-change
# trigger: only an inbound email, a risk sweep, or a direct/manual request.
EntryPoint = Literal["email", "risk", "orchestrator"]

RunStatus = Literal["running", "completed", "failed"]


class FollowUpState(TypedDict, total=False):
    # --- Input (set by the caller) ---
    entry_point: EntryPoint
    trigger: dict[str, Any]  # raw trigger payload
    opportunity_id: str
    workspace_id: str
    run_id: str

    # --- Populated by pipeline nodes ---
    # The full deal picture, loaded once via ProfileService.build_deal_context
    # (shadow entities excluded). Replaces the spec's separate `profile`.
    deal_context: Optional[DealContext]
    profile_narrative: Optional[str]
    classification: Optional[dict[str, Any]]
    plan: Optional[NextStepPlan]  # the next-step agent's ordered plan of steps
    risk_assessment: Optional[RiskAssessment]
    calendar: Optional[CalendarResult]
    draft: Optional[DraftResult]
    task_results: dict[str, Any]  # per-task outputs that aren't first-class fields
    pending_action: Optional[dict[str, Any]]  # the created pending action record

    # --- Status / observability ---
    error: Optional[str]
    status: RunStatus
    trace: list[str]  # each node appends its name; node-order assertions + run log


__all__ = ["FollowUpState", "EntryPoint", "RunStatus"]
