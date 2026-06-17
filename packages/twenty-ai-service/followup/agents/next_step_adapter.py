"""Adapter: the real Next-Step Intelligence agent behind the orchestrator's
``NextStepAgent`` contract.

The orchestrator speaks ``NextStepRequest -> NextStepPlan`` (steps keyed by
``STEP_KINDS``). The merged agent speaks ``DealContext + FollowUpEvent ->
NextStepAgentResult`` (recommendations keyed by an orchestrator-tool name). This
adapter is the anti-corruption layer between them:

* IN  — translate the orchestrator's in-memory deal picture + the trigger that
  caused the run (the "what made this run" description, never the raw email) into
  the agent's context, and run it on the subagent model.
* OUT — fold the agent's scored recommendations into a single ``NextStepPlan``,
  mapping each recommendation's tool to a ``STEP_KIND`` and preserving its
  reasoning / evidence / fact refs so the rep reviews a grounded plan.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from followup.agents.mapping import to_next_step_context
from followup.contracts.next_step import (
    NEXT_STEP_TYPES,
    NextStepPlan,
    NextStepRequest,
    PlannedStep,
)
from followup.next_step.agents.next_step.next_step_agent import run_next_step_agent
from followup.next_step.agents.next_step.schemas import (
    NextStepAgentResult,
    RecommendedAction,
)
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType
from tracing import traceable

logger = logging.getLogger(__name__)

# The agent emits an orchestrator-tool name; the orchestrator follows a plan of
# STEP_KINDS. This is the one place the two vocabularies meet.
_TOOL_TO_KIND: dict[str, str] = {
    "send_email": "draft_email",
    "schedule_meeting": "book_meeting",
    "create_task": "create_task",
    "create_reminder": "create_task",
    "update_opportunity": "update_stage",
    "log_activity": "write_note",
}

# Headline action_type for the bundled pending action, by the top step's kind.
_KIND_TO_HEADLINE: dict[str, str] = {
    "draft_email": "follow_up_call",
    "book_meeting": "schedule_meeting",
    "create_task": "follow_up_call",
    "write_note": "check_in",
    "update_stage": "close_deal",
}

# How many steps a single bundled plan carries (one per kind, top priority wins).
_MAX_STEPS = 4


def _priority_label(priority: int) -> str:
    if priority <= 2:
        return "high"
    if priority == 3:
        return "medium"
    return "low"


def _step_from_recommendation(rec: RecommendedAction) -> PlannedStep:
    tool = (rec.orchestrator_action.tool or "").lower()
    kind = _TOOL_TO_KIND.get(tool) or _TOOL_TO_KIND.get(rec.action_type.lower(), "draft_email")
    return PlannedStep(
        kind=kind,
        intent=rec.description or rec.orchestrator_action.instruction or rec.title,
        priority=_priority_label(rec.priority),
        # Carry the agent's grounding so the rep reviews WHY + the manner/style
        # the agent recommended — threaded into the writers downstream.
        metadata={
            "title": rec.title,
            "approach": rec.reasoning,
            "evidence": list(rec.evidence),
            "profile_fact_refs": list(rec.profile_fact_refs),
            "action_type": rec.action_type,
            "source_priority": rec.priority,
        },
    )


def _dedupe_by_kind(steps: list[PlannedStep]) -> list[PlannedStep]:
    """Keep the highest-priority step per kind, ordered by source priority."""
    by_kind: dict[str, PlannedStep] = {}
    for step in steps:
        existing = by_kind.get(step.kind)
        if existing is None or step.metadata["source_priority"] < existing.metadata["source_priority"]:
            by_kind[step.kind] = step
    ordered = sorted(by_kind.values(), key=lambda s: s.metadata["source_priority"])
    return ordered[:_MAX_STEPS]


def _headline_action(steps: list[PlannedStep]) -> str:
    if not steps:
        return "no_action"
    top = steps[0]
    action_type = (top.metadata.get("action_type") or "").lower()
    if action_type in NEXT_STEP_TYPES:
        return action_type
    return _KIND_TO_HEADLINE.get(top.kind, "follow_up_call")


def _fallback_plan(opportunity_id: str, summary: str) -> NextStepPlan:
    """A safe single-step plan when the agent returns nothing usable (LLM hiccup)."""
    return NextStepPlan(
        steps=[
            PlannedStep(
                kind="draft_email",
                intent="Send a brief, helpful follow-up that keeps the deal moving.",
                priority="medium",
                metadata={"source_priority": 3, "title": "Follow up", "approach": summary},
            )
        ],
        headline_action="check_in",
        summary=summary or "Follow-up recommended.",
        metadata={"opportunity_id": opportunity_id, "source": "next_step_fallback"},
    )


def _trigger_description(request: NextStepRequest) -> str:
    """A distilled 'what made this run' — the orchestrator owns the raw trigger."""
    lines: list[str] = []
    trigger = request.trigger
    if trigger is not None:
        if getattr(trigger, "subject", None):
            lines.append(f"Inbound email subject: {trigger.subject}")
        if getattr(trigger, "body", None):
            lines.append(f"Message: {trigger.body}")
    classification = request.classification or {}
    if classification.get("type"):
        lines.append(
            f"Triage: type={classification.get('type')}, "
            f"urgency={classification.get('urgency')}"
        )
    if not lines and request.narrative:
        lines.append(request.narrative)
    return "\n".join(lines)


class OrchestratorNextStepAgent:
    """Real next-step agent wrapped to satisfy the orchestrator's ``NextStepAgent``."""

    def __init__(self, model: str | None = None) -> None:
        self._model = model

    @traceable(name="next_step_agent", run_type="chain")
    async def run(self, request: NextStepRequest) -> NextStepPlan:
        deal = request.deal_context
        opportunity_id = str(deal.opportunity_id)
        try:
            context = to_next_step_context(deal)
            event = FollowUpEvent(
                event_id=str(uuid.uuid4()),
                idempotency_key=str(uuid.uuid4()),
                # An inbound email/signal is a logged activity on the deal; the
                # orchestrator-driven path bypasses the agent's event-type gate
                # (it supplies trigger_context), so this is informational only.
                event_type=FollowUpEventType.ACTIVITY_LOGGED,
                opportunity_id=opportunity_id,
                # workspace_id/user_id are event metadata the agent does not read;
                # the orchestrator's profile context does not carry them.
                workspace_id="orchestrator",
                user_id="orchestrator",
                occurred_at=datetime.now(timezone.utc),
            )
            result = await run_next_step_agent(
                context,
                event,
                trigger_context=_trigger_description(request),
                model=self._model,
            )
        except Exception as error:  # noqa: BLE001 — never crash the orchestrator
            logger.exception("next-step adapter failed for %s", opportunity_id)
            return _fallback_plan(opportunity_id, f"Next-step unavailable ({error}).")

        return self._to_plan(result, opportunity_id, deal)

    def _to_plan(
        self, result: NextStepAgentResult, opportunity_id: str, deal
    ) -> NextStepPlan:
        if result.skipped:
            # A deliberate skip (e.g. closed deal) → no action, reason preserved.
            return NextStepPlan(
                steps=[],
                headline_action="no_action",
                summary=result.skip_reason or "No next step recommended.",
                metadata={"opportunity_id": opportunity_id, "source": "next_step_skip"},
            )
        if not result.recommended_actions:
            return _fallback_plan(opportunity_id, result.summary_reasoning)

        steps = _dedupe_by_kind(
            [_step_from_recommendation(rec) for rec in result.recommended_actions]
        )
        recipient = next((c.email for c in deal.contacts if c.email), None)
        return NextStepPlan(
            steps=steps,
            headline_action=_headline_action(steps),
            summary=result.summary_reasoning or "Next-step plan ready.",
            metadata={
                "opportunity_id": opportunity_id,
                "source": "next_step_agent",
                "confidence": result.confidence,
                "recipient_email": recipient,
            },
        )


__all__ = ["OrchestratorNextStepAgent"]
