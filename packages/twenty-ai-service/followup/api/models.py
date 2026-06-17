"""Pydantic request / response models for the Follow-Up REST API.

Trigger requests map to the real entry points and the real trigger-dict keys
the graph reads.  Response models carry only JSON-safe primitives so FastAPI
serialization is trivial.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from followup.store.repositories import PendingAction


# ===========================================================================
# Trigger requests — each maps to a graph entry_point
# ===========================================================================


class FollowUpEventRequest(BaseModel):
    """Inbound email trigger → entry_point="email"."""

    email_id: str
    workspace_id: str
    sender_email: str  # graph reads trigger["sender_email"]
    subject: str
    body: str
    opportunity_id: Optional[str] = None  # email path RESOLVES the deal via extraction
    owner_user_id: Optional[str] = None  # lets check_calendar read the rep's calendar
    urgency: str = "medium"


class DirectFollowUpRequest(BaseModel):
    """Direct / manual trigger → entry_point="orchestrator"."""

    opportunity_id: str
    workspace_id: str
    owner_user_id: str
    instructions: str
    wants_meeting: bool = False  # graph maps this to requires_calendar
    proposed_times: Optional[list[str]] = None
    urgency: str = "medium"


class RiskSweepRequest(BaseModel):
    """Risk sweep trigger → entry_point="risk", looped per deal."""

    workspace_id: str
    sweep_id: str


# ===========================================================================
# Action requests — the accept / revise / reject loop
# ===========================================================================


class AcceptRequest(BaseModel):
    """Accept a pending action → execute via orchestrator→writer."""

    user_id: str  # acted_on_by


class ReviseRequest(BaseModel):
    """Ask the agent to revise a pending action (the "edit" loop)."""

    user_id: str
    instructions: str  # what the rep wants changed, in natural language


class RejectRequest(BaseModel):
    """Reject a pending action — marks it rejected, no side effects."""

    user_id: str
    reason: Optional[str] = None


# ===========================================================================
# Responses
# ===========================================================================


class FollowUpRunResult(BaseModel):
    """Result of a follow-up pipeline run."""

    run_id: str
    status: str  # completed | failed
    pending_action_id: Optional[str] = None
    error: Optional[str] = None


class PendingActionResponse(BaseModel):
    """Read-friendly projection of a PendingAction row."""

    id: str
    opportunity_id: str
    action_type: str
    action_payload: dict[str, Any]
    reasoning: Optional[str] = None
    urgency: str
    profile_narrative: Optional[str] = None
    draft_subject: Optional[str] = None  # from draft_result if present
    draft_body: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    expires_at: Optional[str] = None

    @classmethod
    def from_db(cls, action: PendingAction) -> "PendingActionResponse":
        """Build a response model from a DB dataclass row."""
        draft = action.draft_result or {}
        return cls(
            id=str(action.id),
            opportunity_id=str(action.opportunity_id),
            action_type=action.action_type,
            action_payload=action.action_payload or {},
            reasoning=action.reasoning,
            urgency=action.urgency,
            profile_narrative=action.profile_narrative,
            draft_subject=draft.get("subject"),
            draft_body=draft.get("body"),
            status=action.status,
            created_at=action.created_at.isoformat() if action.created_at else None,
            expires_at=action.expires_at.isoformat() if action.expires_at else None,
        )


class AcceptResult(BaseModel):
    """Result of accepting a pending action."""

    action_id: str
    execution_status: str  # completed | failed | unknown
    error: Optional[str] = None


class ReviseResult(BaseModel):
    """Result of a revise request — the old action is edited, a new one is created."""

    previous_action_id: str
    new_action: PendingActionResponse  # the re-run result, for another accept/reject


class RiskSweepResult(BaseModel):
    """Result of a risk sweep."""

    processed: int
    actions_created: int
    errors: int


__all__ = [
    "FollowUpEventRequest",
    "DirectFollowUpRequest",
    "RiskSweepRequest",
    "AcceptRequest",
    "ReviseRequest",
    "RejectRequest",
    "FollowUpRunResult",
    "PendingActionResponse",
    "AcceptResult",
    "ReviseResult",
    "RiskSweepResult",
]
