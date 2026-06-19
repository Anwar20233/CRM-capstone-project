"""REST endpoints for the Follow-Up Intelligence Agent (Step 7).

    POST /followup/events              — email trigger
    POST /followup/direct              — direct / manual trigger
    POST /followup/workflows/risk/sweep — risk sweep (loops per deal)
    GET  /followup/actions              — list pending actions for an opportunity
    POST /followup/actions/{id}/accept  — accept → execute via orchestrator→writer
    POST /followup/actions/{id}/revise  — revise → re-run pipeline → new pending action
    POST /followup/actions/{id}/reject  — reject (status only, no side effects)
    GET  /followup/profile/{opportunity_id} — profile narrative
    POST /followup/actions/expire       — bulk-expire stale actions (cron calls in Step 8)
    POST /followup/profile/consolidate  — stub (Step 8)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from followup.api.dependencies import get_accept_graph, get_deps, get_followup_graph
from followup.api.execution import FollowupActionExecutor, _action_to_fact
from followup.api.models import (
    AcceptRequest,
    AcceptResult,
    ChatRequest,
    ChatResponse,
    DirectFollowUpRequest,
    EmailFetchRequest,
    EmailFetchResult,
    EmailReviewRequest,
    EmailReviewResult,
    EmailSendOutboxRequest,
    EmailSendOutboxResult,
    FollowUpEventRequest,
    FollowUpRunResult,
    PendingActionResponse,
    RejectRequest,
    ReviseRequest,
    ReviseResult,
    ReviseStepRequest,
    ReviseStepResult,
    RiskScoreResponse,
    RiskSweepRequest,
    RiskSweepResult,
)
from followup.orchestrator.deps import OrchestratorDeps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/followup", tags=["followup"])


# ===========================================================================
# Helpers
# ===========================================================================


async def _run_followup_pipeline(
    deps: OrchestratorDeps,
    graph: Any,
    entry_point: str,
    trigger: dict[str, Any],
    opportunity_id: str | None,
    workspace_id: str,
) -> FollowUpRunResult:
    """Execute the follow-up pipeline and return the run result."""
    run_id = str(uuid.uuid4())
    initial_state = {
        "entry_point": entry_point,
        "trigger": trigger,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "trace": [],
    }
    if opportunity_id:
        initial_state["opportunity_id"] = opportunity_id

    try:
        result = await graph.ainvoke(initial_state)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Follow-up pipeline run %s failed", run_id)
        return FollowUpRunResult(
            run_id=run_id, status="failed", error=str(exc)
        )

    status = result.get("status", "failed")
    pending = result.get("pending_action")
    pending_id = str(pending["id"]) if pending and isinstance(pending, dict) else None

    return FollowUpRunResult(
        run_id=run_id,
        status=status,
        pending_action_id=pending_id,
        error=result.get("error"),
    )


# ===========================================================================
# Trigger endpoints
# ===========================================================================


@router.post("/events", response_model=FollowUpRunResult)
async def trigger_email_event(
    request: FollowUpEventRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    graph: Any = Depends(get_followup_graph),
) -> FollowUpRunResult:
    """Trigger a follow-up pipeline from an inbound email."""
    trigger = {
        "id": request.email_id,
        "sender_email": request.sender_email,
        "subject": request.subject,
        "body": request.body,
        "urgency": request.urgency,
    }
    if request.owner_user_id:
        trigger["owner_user_id"] = request.owner_user_id

    return await _run_followup_pipeline(
        deps=deps,
        graph=graph,
        entry_point="email",
        trigger=trigger,
        opportunity_id=request.opportunity_id,
        workspace_id=request.workspace_id,
    )


@router.post("/direct", response_model=FollowUpRunResult)
async def trigger_direct_followup(
    request: DirectFollowUpRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    graph: Any = Depends(get_followup_graph),
) -> FollowUpRunResult:
    """Trigger a direct / manual follow-up for a specific opportunity."""
    trigger = {
        "instructions": request.instructions,
        "urgency": request.urgency,
        "wants_meeting": request.wants_meeting,
        "owner_user_id": request.owner_user_id,
    }
    if request.proposed_times:
        trigger["proposed_times"] = request.proposed_times
    if request.duration_minutes:
        trigger["duration_minutes"] = request.duration_minutes
    if request.timezone:
        trigger["timezone"] = request.timezone

    return await _run_followup_pipeline(
        deps=deps,
        graph=graph,
        entry_point="orchestrator",
        trigger=trigger,
        opportunity_id=request.opportunity_id,
        workspace_id=request.workspace_id,
    )


@router.post("/workflows/risk/sweep", response_model=RiskSweepResult)
async def trigger_risk_sweep(
    request: RiskSweepRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    graph: Any = Depends(get_followup_graph),
) -> RiskSweepResult:
    """Run the risk sweep across active opportunities.

    NOTE: ``CRMReader.get_active_opportunities(workspace_id)`` is a flagged gap.
    The caller (Step 8 cron or the person building the agent) is expected to
    supply the opportunity ids.  For now this endpoint accepts the sweep_id
    and iterates whatever active deals the reader can find.
    """
    processed = 0
    actions_created = 0
    errors = 0

    # Attempt to get active opportunities — this is a flagged gap (the cron
    # in Step 8 will supply ids). For now we try the reader; if it doesn't
    # support it, return an empty sweep.
    try:
        # get_active_opportunities is not on the CRMReader protocol yet.
        # The person building the agent will handle this.
        reader = deps.pipeline.crm_reader
        if hasattr(reader, "get_active_opportunities"):
            opportunities = await reader.get_active_opportunities(request.workspace_id)
        else:
            logger.warning(
                "CRMReader does not implement get_active_opportunities; "
                "risk sweep skipped — the Step 8 cron will supply ids."
            )
            return RiskSweepResult(processed=0, actions_created=0, errors=0)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to fetch active opportunities for risk sweep")
        return RiskSweepResult(processed=0, actions_created=0, errors=1)

    for opp in opportunities:
        opp_id = opp.get("id") if isinstance(opp, dict) else str(opp)
        processed += 1
        try:
            result = await _run_followup_pipeline(
                deps=deps,
                graph=graph,
                entry_point="risk",
                trigger={"sweep_id": request.sweep_id},
                opportunity_id=opp_id,
                workspace_id=request.workspace_id,
            )
            if result.pending_action_id:
                actions_created += 1
        except Exception:  # noqa: BLE001
            errors += 1

    return RiskSweepResult(
        processed=processed, actions_created=actions_created, errors=errors
    )


# ===========================================================================
# Action CRUD + review loop
# ===========================================================================


@router.get("/actions", response_model=list[PendingActionResponse])
async def list_actions(
    opportunity_id: str,
    status: str = "pending",
    deps: OrchestratorDeps = Depends(get_deps),
) -> list[PendingActionResponse]:
    """List pending actions for an opportunity (opportunity tab)."""
    from followup.api.models import SourceEmail
    from followup.store.repositories import InboundEmailRepository

    actions = await deps.pipeline.pending_actions.list_pending(
        uuid.UUID(opportunity_id), status=status
    )
    responses = [PendingActionResponse.from_db(a) for a in actions]

    # Attach the inbound email each email-triggered workflow responds to.
    inbound_repo = InboundEmailRepository(deps.pipeline.executor)
    for action, response in zip(actions, responses):
        if "email" in (action.trigger_type or "") and action.trigger_id:
            try:
                email = await inbound_repo.get(uuid.UUID(action.trigger_id))
            except Exception:  # noqa: BLE001 — never fail the list over a bad trigger id
                email = None
            if email is not None:
                response.source_email = SourceEmail(
                    subject=email.subject,
                    body=email.body,
                    sender_email=email.sender_email,
                )

    return responses


@router.post("/actions/{action_id}/accept", response_model=AcceptResult)
async def accept_action(
    action_id: str,
    request: AcceptRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    accept_graph: Any = Depends(get_accept_graph),
) -> AcceptResult:
    """Accept a pending action — execute via orchestrator→writer."""
    action = await deps.pipeline.pending_actions.get(uuid.UUID(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Action is '{action.status}', not 'pending'",
        )

    # Run the accept graph.
    initial_state = {
        "action_id": action_id,
        "user_id": request.user_id,
        "workspace_id": str(action.workspace_id),
        "opportunity_id": str(action.opportunity_id),
        "disabled_step_indices": request.disabled_step_indices,
    }
    try:
        result = await accept_graph.ainvoke(initial_state)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Accept graph failed for action %s", action_id)
        return AcceptResult(
            action_id=action_id, execution_status="failed", error=str(exc)
        )

    # Re-read the action to get execution_status.
    updated = await deps.pipeline.pending_actions.get(uuid.UUID(action_id))
    exec_status = updated.execution_status if updated else "unknown"
    exec_error = updated.execution_error if updated else None

    return AcceptResult(
        action_id=action_id,
        execution_status=exec_status or "unknown",
        error=exec_error,
    )


@router.post("/actions/{action_id}/revise", response_model=ReviseResult)
async def revise_action(
    action_id: str,
    request: ReviseRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    graph: Any = Depends(get_followup_graph),
) -> ReviseResult:
    """Revise a pending action — re-run the pipeline with rep instructions.

    Marks the prior action as ``edited`` and returns a new pending action
    for another accept/reject cycle (the chat loop).
    """
    action = await deps.pipeline.pending_actions.get(uuid.UUID(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Action is '{action.status}', not 'pending'",
        )

    # Mark the prior action as edited.
    action.status = "edited"
    action.acted_on_at = datetime.now(timezone.utc)
    action.acted_on_by = uuid.UUID(request.user_id)
    await deps.pipeline.pending_actions.save(action)

    # Re-run the follow-up pipeline via the orchestrator entry point,
    # carrying the rep's instructions plus the prior action's context.
    prior_draft = action.draft_result or {}
    trigger = {
        "instructions": request.instructions,
        "previous_action_id": action_id,
        "prior_draft": prior_draft,
        "urgency": action.urgency,
        "owner_user_id": str(action.acted_on_by) if action.acted_on_by else None,
    }

    result = await _run_followup_pipeline(
        deps=deps,
        graph=graph,
        entry_point="orchestrator",
        trigger=trigger,
        opportunity_id=str(action.opportunity_id),
        workspace_id=str(action.workspace_id),
    )

    if result.status != "completed" or not result.pending_action_id:
        raise HTTPException(
            status_code=500,
            detail=f"Revise re-run failed: {result.error or 'no pending action created'}",
        )

    # Fetch the newly created action.
    new_action = await deps.pipeline.pending_actions.get(
        uuid.UUID(result.pending_action_id)
    )
    if new_action is None:
        raise HTTPException(
            status_code=500,
            detail="Revise completed but new action not found in DB",
        )

    return ReviseResult(
        previous_action_id=action_id,
        new_action=PendingActionResponse.from_db(new_action),
    )


@router.post("/actions/{action_id}/revise-step", response_model=ReviseStepResult)
async def revise_action_step(
    action_id: str,
    request: ReviseStepRequest,
    deps: OrchestratorDeps = Depends(get_deps),
) -> ReviseStepResult:
    """Edit ONE step of a pending action in place (Strategy A — targeted revise).

    Preserves the rest of the workflow. For a meeting-time change that lands on a
    booked slot, returns ``status="unavailable"`` with free alternatives instead
    of applying the change.
    """
    from followup.orchestrator.revise import revise_step_in_place

    action = await deps.pipeline.pending_actions.get(uuid.UUID(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Action is '{action.status}', not 'pending'",
        )

    result = await revise_step_in_place(
        deps,
        action=action,
        target=request.target,
        instructions=request.instructions,
        requested_time=request.requested_time,
        requested_duration_minutes=request.requested_duration_minutes,
        user_id=request.user_id,
        tz_name=request.timezone,
    )

    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result.get("error", "revise failed"))
    if result["status"] == "unavailable":
        return ReviseStepResult(
            status="unavailable",
            requested_time=result.get("requested_time"),
            alternatives=result.get("alternatives", []),
        )
    return ReviseStepResult(
        status="updated",
        action=PendingActionResponse.from_db(result["action"]),
    )


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: str,
    request: RejectRequest,
    deps: OrchestratorDeps = Depends(get_deps),
) -> dict[str, str]:
    """Reject a pending action — marks it rejected, no side effects."""
    action = await deps.pipeline.pending_actions.get(uuid.UUID(action_id))
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"Action is '{action.status}', not 'pending'",
        )

    action.status = "rejected"
    action.acted_on_at = datetime.now(timezone.utc)
    action.acted_on_by = uuid.UUID(request.user_id)
    await deps.pipeline.pending_actions.save(action)

    return {"action_id": action_id, "status": "rejected"}


# ===========================================================================
# Conversational chat (opportunity Follow-Up tab)
# ===========================================================================


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    accept_graph: Any = Depends(get_accept_graph),
    followup_graph: Any = Depends(get_followup_graph),
) -> ChatResponse:
    """One conversational turn for the opportunity Follow-Up tab.

    The agent may read or act on pending actions (accept / reject / revise) or
    create a new follow-up, reusing the same operations as the structured
    endpoints. Returns the reply plus the refreshed pending-action list.
    """
    from followup.chat import run_followup_chat

    result = await run_followup_chat(
        deps=deps,
        accept_graph=accept_graph,
        followup_graph=followup_graph,
        run_pipeline=_run_followup_pipeline,
        opportunity_id=request.opportunity_id,
        workspace_id=request.workspace_id,
        user_id=request.user_id,
        message=request.message,
        history=[turn.model_dump() for turn in (request.history or [])],
        tz_name=request.timezone,
    )

    return ChatResponse(reply=result.reply, actions=result.actions)


# ===========================================================================
# Email monitoring workflows (Phase 1 fetch, Phase 2 review, outbox send)
# ===========================================================================


@router.post("/workflows/email/fetch", response_model=EmailFetchResult)
async def workflow_email_fetch(
    request: EmailFetchRequest,
    deps: OrchestratorDeps = Depends(get_deps),
) -> EmailFetchResult:
    """Phase 1 — fetch inbound CRM messages into the queue (no LLM)."""
    from datetime import datetime

    from followup.store.repositories import InboundEmailRepository
    from followup.workflows.email.fetch import fetch_inbound_emails

    since = None
    if request.since:
        since = datetime.fromisoformat(request.since.replace("Z", "+00:00"))

    result = await fetch_inbound_emails(
        InboundEmailRepository(deps.pipeline.executor),
        workspace_id=request.workspace_id,
        since=since,
    )
    return EmailFetchResult(
        fetched=result.fetched,
        enqueued=result.enqueued,
        skipped_duplicate=result.skipped_duplicate,
    )


@router.post("/workflows/email/review", response_model=EmailReviewResult)
async def workflow_email_review(
    request: EmailReviewRequest,
    deps: OrchestratorDeps = Depends(get_deps),
    graph: Any = Depends(get_followup_graph),
) -> EmailReviewResult:
    """Phase 2 — process queued inbound emails through the follow-up pipeline."""
    from followup.store.repositories import InboundEmailRepository
    from followup.workflows.email.review import review_pending_emails

    result = await review_pending_emails(
        InboundEmailRepository(deps.pipeline.executor),
        graph,
        workspace_id=request.workspace_id,
        batch_size=request.batch_size,
    )
    return EmailReviewResult(
        claimed=result.claimed,
        processed=result.processed,
        skipped=result.skipped,
        failed=result.failed,
    )


@router.post("/workflows/email/send-outbox", response_model=EmailSendOutboxResult)
async def workflow_email_send_outbox(
    request: EmailSendOutboxRequest,
    deps: OrchestratorDeps = Depends(get_deps),
) -> EmailSendOutboxResult:
    """Send accepted draft emails from the outbox (idempotent)."""
    from followup.workflows.email.send_outbox import send_outbox_batch

    result = await send_outbox_batch(
        deps.pipeline.pending_actions,
        workspace_id=request.workspace_id,
        batch_size=request.batch_size,
    )
    return EmailSendOutboxResult(
        claimed=result.claimed,
        sent=result.sent,
        skipped=result.skipped,
        failed=result.failed,
    )


# ===========================================================================
# Profile + maintenance endpoints
# ===========================================================================


@router.get("/profile/{opportunity_id}")
async def get_profile_narrative(
    opportunity_id: str,
    deps: OrchestratorDeps = Depends(get_deps),
) -> dict[str, Any]:
    """Return the synthesized profile narrative for an opportunity."""
    from followup.profile.service import ProfileNotFound

    try:
        narrative = await deps.profile_service.build_profile_narrative(opportunity_id)
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    from followup.profile.schemas import _row_to_dict

    contacts = [
        {
            "crm_id": c.crm_id,
            "name": c.name,
            "role": c.role,
            "email": c.email,
        }
        for c in narrative.contacts
    ]
    return {
        "opportunity_id": narrative.opportunity_id,
        "narrative": narrative.narrative,
        "contacts": contacts,
        "key_facts": narrative.key_facts,
        "relationships": narrative.relationships,
        "risk_score": narrative.risk_score,
        "generated_at": narrative.generated_at.isoformat(),
    }


@router.get("/risk/{opportunity_id}", response_model=RiskScoreResponse)
async def get_daily_risk(
    opportunity_id: str,
    deps: OrchestratorDeps = Depends(get_deps),
) -> RiskScoreResponse:
    """Return the latest daily-computed risk score (read-only, no LLM)."""
    from followup.store.repositories import RiskDailyScoreRepository

    repo = RiskDailyScoreRepository(deps.pipeline.executor)
    score = await repo.get_latest(uuid.UUID(opportunity_id))
    if score is None:
        return RiskScoreResponse(opportunity_id=opportunity_id)

    return RiskScoreResponse(
        opportunity_id=opportunity_id,
        risk_score=score.risk_score,
        risk_level=score.risk_level,
        top_factors=score.top_factors or [],
        assessed_at=score.assessed_at.isoformat() if score.assessed_at else None,
    )


@router.post("/actions/expire")
async def expire_stale_actions(
    deps: OrchestratorDeps = Depends(get_deps),
) -> dict[str, int]:
    """Bulk-expire stale pending actions (cron calls this in Step 8)."""
    expired = await deps.pipeline.pending_actions.expire_stale(
        datetime.now(timezone.utc)
    )
    return {"expired": expired}


@router.post("/profile/consolidate")
async def consolidate_profiles() -> dict[str, str]:
    """Stub — profile consolidation is implemented in Step 8."""
    return {"status": "stub", "message": "Profile consolidation is not yet implemented (Step 8)"}


__all__ = ["router"]
