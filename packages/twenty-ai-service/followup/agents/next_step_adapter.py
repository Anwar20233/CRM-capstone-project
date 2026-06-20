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
from followup.next_step.context.schemas import DealContext as NextStepDealContext
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType
from followup.profile.masking import ProfileMasker
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
    # The planner is calibrated to reserve priority 1 for genuine urgency (active
    # risk, hard deadlines, escalations) and rate normal forward progress 2–3.
    # Mapping only priority 1 to "high" keeps that meaning intact; collapsing 1–2
    # into "high" (the old split) made every healthy deal look urgent.
    if priority <= 1:
        return "high"
    if priority <= 3:
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


def _inbound_signal(request: NextStepRequest) -> dict[str, str] | None:
    """The triggering email as a timeline activity, so engagement reflects it.

    A deal whose buyer just emailed is actively engaged — folding the inbound
    email into the activity timeline is what makes that true downstream (the
    engagement clock resets to the email's arrival, not a stale CRM gap).
    """
    trigger = request.trigger
    if trigger is None:
        return None
    summary = getattr(trigger, "subject", None) or getattr(trigger, "body", "") or "Inbound email"
    received_at = getattr(trigger, "received_at", None) or datetime.now(timezone.utc).isoformat()
    return {"type": "email", "date": received_at, "summary": summary}


def _trigger_description(request: NextStepRequest) -> str:
    """Raw trigger signal passed to the agent.

    Passes the original email subject and body without any pre-classification.
    The agent reads its own stage playbook and BANT framework and decides what
    actions to take from the raw trigger — it is not told what 'type' of email
    arrived or what 'urgency' a classifier assigned.
    """
    lines: list[str] = []
    trigger = request.trigger
    if trigger is not None:
        if getattr(trigger, "subject", None):
            lines.append(f"Inbound email subject: {trigger.subject}")
        if getattr(trigger, "body", None):
            lines.append(f"Message body:\n{trigger.body}")
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
            context = to_next_step_context(deal, inbound_signal=_inbound_signal(request))
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
            # PII discipline (mirrors the drafting/note/task authors): mask names,
            # emails and phone numbers — in the deal picture AND the raw inbound
            # email — before the cloud planner sees them, then restore real values
            # in the recommendations so the rep reviews a plan with real names.
            masker = self._masker(deal)
            result = await run_next_step_agent(
                self._mask_context(context, masker),
                event,
                trigger_context=masker.mask(_trigger_description(request)),
                model=self._model,
            )
            result = self._unmask_result(result, masker)
        except Exception as error:  # noqa: BLE001 — never crash the orchestrator
            logger.exception("next-step adapter failed for %s", opportunity_id)
            return _fallback_plan(opportunity_id, f"Next-step unavailable ({error}).")

        return self._to_plan(result, opportunity_id, deal)

    def _masker(self, deal) -> ProfileMasker:
        """A masking session seeded with the deal's known people (see drafting adapter)."""
        contacts = [
            {"id": c.crm_id, "name": c.name, "email": c.email} for c in deal.contacts
        ]
        return ProfileMasker().register(contacts=contacts)

    def _mask_context(
        self, context: NextStepDealContext, masker: ProfileMasker
    ) -> NextStepDealContext:
        """Mask the free-text fields the planner prompt renders.

        Opportunity/company names, deal stage and BANT/engagement signals stay
        visible (business context, not PII). Contact names, timeline prose, task
        titles and fact values carry people/emails/phones, so they are masked.
        """
        contacts = [
            contact.model_copy(
                update={"name": masker.mask(contact.name) if contact.name else contact.name}
            )
            for contact in context.contacts
        ]
        timeline = [
            item.model_copy(
                update={
                    "title": masker.mask(item.title) if item.title else item.title,
                    "summary": masker.mask(item.summary) if item.summary else item.summary,
                }
            )
            for item in context.timeline
        ]
        tasks = [
            task.model_copy(
                update={"title": masker.mask(task.title) if task.title else task.title}
            )
            for task in context.tasks
        ]
        facts = [
            fact.model_copy(
                update={"value": masker.mask(fact.value) if fact.value else fact.value}
            )
            for fact in context.active_facts
        ]
        return context.model_copy(
            update={
                "contacts": contacts,
                "timeline": timeline,
                "tasks": tasks,
                "active_facts": facts,
            }
        )

    @staticmethod
    def _unmask_result(
        result: NextStepAgentResult, masker: ProfileMasker
    ) -> NextStepAgentResult:
        """Restore real names in the recommendations the LLM wrote.

        Handles are per-session, so leaving them in the plan would break the
        fresh maskers the downstream note/task/email authors run; we unmask the
        whole result here (ids/enums have no handles, so they pass through).
        """
        unmasked = masker.unmask(result.model_dump())
        return NextStepAgentResult.model_validate(unmasked)

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

        # A deliberate "nothing to do": the email is purely informational and
        # needs no follow-up. Emit a no-step plan (no draft, no meeting) so the
        # rep sees "no action needed" rather than a manufactured task.
        top = min(result.recommended_actions, key=lambda rec: rec.priority)
        if (top.action_type or "").lower() == "no_action":
            return NextStepPlan(
                steps=[],
                headline_action="no_action",
                summary=result.summary_reasoning or top.reasoning or "No follow-up needed.",
                metadata={"opportunity_id": opportunity_id, "source": "next_step_no_action"},
            )

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
