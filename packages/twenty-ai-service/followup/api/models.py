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
    # Meeting slots the sender requested in the email (ISO-8601 starts). When the
    # caller has already parsed them, pass them so check_calendar honours the
    # requested windows instead of free-picking the rep's next open slot.
    proposed_times: Optional[list[str]] = None
    duration_minutes: Optional[int] = None  # requested meeting length; defaults to 30
    urgency: str = "medium"


class DirectFollowUpRequest(BaseModel):
    """Direct / manual trigger → entry_point="orchestrator"."""

    opportunity_id: str
    workspace_id: str
    owner_user_id: str
    instructions: str
    wants_meeting: bool = False  # graph maps this to requires_calendar
    proposed_times: Optional[list[str]] = None
    duration_minutes: Optional[int] = None  # meeting length; defaults to 30 when unset
    timezone: Optional[str] = None  # rep's IANA tz for rendering proposed times
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
    disabled_step_indices: list[int] = []  # steps the rep toggled off before accepting


class ReviseRequest(BaseModel):
    """Ask the agent to revise a pending action (the "edit" loop)."""

    user_id: str
    instructions: str  # what the rep wants changed, in natural language


class ReviseStepRequest(BaseModel):
    """Edit ONE step of a pending action in place (Strategy A — targeted revise)."""

    user_id: str
    target: str  # step kind (draft_email | write_note | …) or numeric index
    instructions: str  # what the rep wants changed for that step
    requested_time: Optional[str] = None  # ISO-8601, for a book_meeting start change
    requested_duration_minutes: Optional[int] = None  # for a book_meeting duration change
    timezone: Optional[str] = None  # rep's IANA tz for rendering proposed times


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


# Human labels for the workflow step kinds the UI groups by.
_STEP_KIND_LABEL = {
    "create_task": "New task",
    "create_note": "New note",
    "update_stage": "Update deal",
    "update_opportunity": "Update deal",
    "draft_email": "Email draft",
    "book_meeting": "Calendar booking",
    "escalate": "Review at-risk deal",
}

# Step kinds that write an opportunity field (stage / close date) — their card
# detail is the grounded change (or the reason it can't be applied).
_OPP_UPDATE_KINDS = frozenset({"update_stage", "update_opportunity"})


class WorkflowStep(BaseModel):
    """One step of a follow-up workflow, projected for the grouped UI."""

    index: int  # position in the action's plan; used as the toggle key
    kind: str  # create_task | create_note | update_stage | draft_email | book_meeting
    title: str
    detail: Optional[str] = None
    priority: Optional[str] = None
    # draft_email steps
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    # book_meeting steps
    meeting_start: Optional[str] = None
    meeting_end: Optional[str] = None
    invitees: list[str] = []


class SourceEmail(BaseModel):
    """The inbound email a workflow was generated in response to."""

    subject: Optional[str] = None
    body: Optional[str] = None
    sender_email: Optional[str] = None


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
    steps: list[WorkflowStep] = []
    source_email: Optional[SourceEmail] = None
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
            steps=cls._build_steps(action),
            status=action.status,
            created_at=action.created_at.isoformat() if action.created_at else None,
            expires_at=action.expires_at.isoformat() if action.expires_at else None,
        )

    @staticmethod
    def _build_steps(action: PendingAction) -> list["WorkflowStep"]:
        """Project the action's plan into grouped, UI-friendly steps."""
        payload = action.action_payload or {}
        raw_steps = payload.get("steps")
        # The primary drafted email (column wins over payload copy).
        draft = action.draft_result or payload.get("draft") or {}
        # Prep-task artifacts: the note body / task title / meeting title that
        # actually get written on accept (the executor reads these, not the
        # planner's labels) — projected here so the card shows, and the rep edits,
        # the real content.
        task_results = payload.get("task_results") or {}
        calendar = payload.get("calendar") or {}
        chosen_slot = next(
            (s for s in (calendar.get("available_slots") or []) if s.get("available")),
            None,
        )
        recipient = draft.get("recipient_email")

        steps: list[WorkflowStep] = []
        draft_attached = False

        if isinstance(raw_steps, list) and raw_steps:
            for index, step in enumerate(raw_steps):
                kind = step.get("kind") or "action"
                meta = step.get("metadata") or {}
                title = meta.get("title") or _STEP_KIND_LABEL.get(
                    kind, kind.replace("_", " ").title()
                )
                ws = WorkflowStep(
                    index=index,
                    kind=kind,
                    title=title,
                    detail=step.get("intent"),
                    priority=step.get("priority"),
                )
                if kind == "draft_email" and not draft_attached:
                    ws.email_subject = draft.get("subject")
                    ws.email_body = draft.get("body")
                    draft_attached = True
                elif kind == "book_meeting":
                    if chosen_slot is not None:
                        ws.meeting_start = chosen_slot.get("start")
                        ws.meeting_end = chosen_slot.get("end")
                        ws.invitees = [recipient] if recipient else []
                    meeting = task_results.get("book_meeting") or {}
                    if meeting.get("title"):
                        ws.title = meeting["title"]
                elif kind == "write_note":
                    note = task_results.get("write_note") or {}
                    ws.detail = note.get("body") or ws.detail
                elif kind == "create_task":
                    task = task_results.get("create_task") or {}
                    if task.get("title"):
                        ws.title = task["title"]
                elif kind in _OPP_UPDATE_KINDS:
                    # Show the grounded change ("Move stage to Proposal") so the rep
                    # reviews the concrete edit — or the reason it can't be applied,
                    # never a vague intent that silently does nothing on accept.
                    change = task_results.get(kind) or {}
                    if change.get("valid") and change.get("display"):
                        ws.detail = change["display"]
                    elif change.get("reason"):
                        ws.detail = f"⚠ Needs review: {change['reason']}"
                steps.append(ws)
            return steps

        # Legacy single-step actions: synthesize one step from the artifact.
        if draft.get("subject") or draft.get("body"):
            steps.append(
                WorkflowStep(
                    index=0,
                    kind="draft_email",
                    title=draft.get("subject") or "Email draft",
                    detail=action.reasoning,
                    email_subject=draft.get("subject"),
                    email_body=draft.get("body"),
                )
            )
        elif chosen_slot is not None:
            steps.append(
                WorkflowStep(
                    index=0,
                    kind="book_meeting",
                    title="Calendar booking",
                    detail=action.reasoning,
                    meeting_start=chosen_slot.get("start"),
                    meeting_end=chosen_slot.get("end"),
                    invitees=[recipient] if recipient else [],
                )
            )
        else:
            # No structured plan/artifact (e.g. a risk escalation): use a clean
            # label as the title and keep the reasoning in the expandable detail.
            label = _STEP_KIND_LABEL.get(
                action.action_type, action.action_type.replace("_", " ").title()
            )
            steps.append(
                WorkflowStep(
                    index=0,
                    kind=action.action_type,
                    title=label,
                    detail=action.reasoning,
                )
            )
        return steps


class StepEdit(BaseModel):
    """A manual edit the rep made to one workflow step before accepting.

    Only the fields relevant to the step's kind are sent; ``None`` means "leave
    as authored". ``index`` matches the projected ``WorkflowStep.index``.
    """

    index: int
    email_subject: Optional[str] = None  # draft_email
    email_body: Optional[str] = None  # draft_email
    title: Optional[str] = None  # create_task / book_meeting
    detail: Optional[str] = None  # write_note body / update_stage intent


class EditActionRequest(BaseModel):
    """Persist the rep's manual edits to a pending action's steps."""

    user_id: str
    steps: list[StepEdit] = []


class EditActionResult(BaseModel):
    """The re-projected action after the rep's edits were saved."""

    action: PendingActionResponse


def apply_step_edits(action: PendingAction, edits: list[StepEdit]) -> None:
    """Write the rep's manual edits into the action's payload, in place.

    Each edit is routed to the exact payload location the executor reads on
    accept (the drafted email, the prep-task note/task/meeting artifacts, or the
    step's intent), so the edited content is what actually gets written — no LLM
    re-composition. The action's ``kind`` per index is derived from the same
    projection the UI saw, so the client can't mis-target a field.
    """
    kind_by_index = {step.index: step.kind for step in PendingActionResponse._build_steps(action)}

    payload = action.action_payload or {}
    raw_steps = payload.get("steps")
    task_results = payload.setdefault("task_results", {})

    def _set_step_field(index: int, field: str, value: Any) -> None:
        if not isinstance(raw_steps, list) or index >= len(raw_steps):
            # Legacy single-step action with no plan list: the executor falls
            # back to the action's reasoning as the step intent.
            if field == "intent":
                action.reasoning = value
            return
        step = raw_steps[index]
        if field == "title":
            step.setdefault("metadata", {})["title"] = value
        else:
            step[field] = value

    def _set_step_change(index: int, value: str) -> None:
        # Record the rep's edited value as the step's proposed change, keeping any
        # field the planner already chose (so a close-date edit stays a date).
        if not isinstance(raw_steps, list) or index >= len(raw_steps):
            return
        metadata = raw_steps[index].setdefault("metadata", {})
        existing = metadata.get("change") or {}
        metadata["change"] = {"field": existing.get("field"), "value": value}

    for edit in edits:
        kind = kind_by_index.get(edit.index)
        if kind is None:
            continue

        if kind == "draft_email":
            draft = payload.setdefault("draft", {})
            if edit.email_subject is not None:
                draft["subject"] = edit.email_subject
            if edit.email_body is not None:
                draft["body"] = edit.email_body
            # from_db prefers draft_result; the send builder prefers payload["draft"].
            # Keep both in sync so the edit is what every reader sees.
            mirror = action.draft_result if action.draft_result is not None else {}
            if edit.email_subject is not None:
                mirror["subject"] = edit.email_subject
            if edit.email_body is not None:
                mirror["body"] = edit.email_body
            action.draft_result = {**draft, **mirror}
        elif kind == "write_note":
            if edit.detail is not None:
                task_results.setdefault("write_note", {})["body"] = edit.detail
        elif kind == "create_task":
            if edit.title is not None:
                task_results.setdefault("create_task", {})["title"] = edit.title
                _set_step_field(edit.index, "title", edit.title)
        elif kind == "book_meeting":
            if edit.title is not None:
                task_results.setdefault("book_meeting", {})["title"] = edit.title
                _set_step_field(edit.index, "title", edit.title)
        elif kind in _OPP_UPDATE_KINDS:
            # The rep retyped the desired change. Stash it as the step's proposed
            # value AND drop any plan-time validated change, so the executor
            # re-grounds the edited text against the real pipeline on accept rather
            # than writing the old (now stale) value.
            if edit.detail is not None:
                _set_step_field(edit.index, "intent", edit.detail)
                _set_step_change(edit.index, edit.detail)
                task_results.pop(kind, None)
        elif edit.detail is not None:
            # Any other intent-driven step.
            _set_step_field(edit.index, "intent", edit.detail)

    action.action_payload = payload


class AcceptResult(BaseModel):
    """Result of accepting a pending action."""

    action_id: str
    execution_status: str  # completed | failed | unknown
    error: Optional[str] = None


class ReviseResult(BaseModel):
    """Result of a revise request — the old action is edited, a new one is created."""

    previous_action_id: str
    new_action: PendingActionResponse  # the re-run result, for another accept/reject


class ReviseStepResult(BaseModel):
    """Result of an in-place step revise (Strategy A).

    ``status="updated"`` carries the refreshed action; ``status="unavailable"``
    carries the requested meeting time plus free alternatives (no change applied).
    """

    status: str  # updated | unavailable
    action: Optional[PendingActionResponse] = None
    requested_time: Optional[str] = None
    alternatives: list[dict[str, Any]] = []


class RiskSweepResult(BaseModel):
    """Result of a risk sweep."""

    processed: int
    actions_created: int
    errors: int


class EmailFetchRequest(BaseModel):
    """Phase 1 — collect inbound emails into the queue."""

    workspace_id: str
    since: Optional[str] = None  # ISO-8601 cursor override


class EmailFetchResult(BaseModel):
    fetched: int
    enqueued: int
    skipped_duplicate: int


class EmailReviewRequest(BaseModel):
    """Phase 2 — process queued inbound emails."""

    workspace_id: str
    batch_size: int = 10


class EmailReviewResult(BaseModel):
    claimed: int
    processed: int
    skipped: int
    failed: int


class EmailSendOutboxRequest(BaseModel):
    """Outbox poller — send accepted draft emails."""

    workspace_id: str
    batch_size: int = 20


class EmailSendOutboxResult(BaseModel):
    claimed: int
    sent: int
    skipped: int
    failed: int


# ===========================================================================
# Conversational chat (opportunity Follow-Up tab)
# ===========================================================================


class ChatMessage(BaseModel):
    """One prior turn of the conversation."""

    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    """A chat turn from the rep on a specific opportunity."""

    opportunity_id: str
    user_id: str
    message: str
    history: Optional[list[ChatMessage]] = None
    workspace_id: Optional[str] = None  # needed to create new follow-ups
    timezone: Optional[str] = None  # rep's IANA tz for resolving clock times


class ChatResponse(BaseModel):
    """Assistant reply plus the refreshed pending-action list."""

    reply: str
    actions: list[PendingActionResponse]


class RiskScoreResponse(BaseModel):
    """The latest daily-computed risk score for an opportunity (0-100 scale)."""

    opportunity_id: str
    risk_score: Optional[float] = None
    risk_level: Optional[str] = None
    top_factors: list[dict[str, Any]] = []
    assessed_at: Optional[str] = None


__all__ = [
    "FollowUpEventRequest",
    "DirectFollowUpRequest",
    "RiskSweepRequest",
    "AcceptRequest",
    "ReviseRequest",
    "RejectRequest",
    "StepEdit",
    "EditActionRequest",
    "EditActionResult",
    "apply_step_edits",
    "FollowUpRunResult",
    "WorkflowStep",
    "SourceEmail",
    "PendingActionResponse",
    "AcceptResult",
    "ReviseResult",
    "ReviseStepRequest",
    "ReviseStepResult",
    "RiskSweepResult",
    "EmailFetchRequest",
    "EmailFetchResult",
    "EmailReviewRequest",
    "EmailReviewResult",
    "EmailSendOutboxRequest",
    "EmailSendOutboxResult",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "RiskScoreResponse",
]
