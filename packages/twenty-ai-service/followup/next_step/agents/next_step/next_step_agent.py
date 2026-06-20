"""Next Step Intelligence Agent — main entry point.

Accepts DealContext + FollowUpEvent from the Orchestrator, runs internal
analysis (via tools.py), calls the LLM once for structured recommendations,
applies internal scoring, and returns NextStepAgentResult to the Orchestrator.

Communication model (strict):
    Orchestrator --> run_next_step_agent() --> NextStepAgentResult --> Orchestrator

This agent:
  - Does NOT call other agents.
  - Does NOT perform RAG retrieval.
  - Does NOT write to the CRM or any database.
  - Does NOT expose scoring logic or intermediate tool outputs.
"""

from __future__ import annotations

import logging

from agent.llm_client import LLMCallError, call_llm_json
from followup.next_step.agents.next_step.prompts import build_next_step_prompt
from followup.next_step.agents.next_step.schemas import (
    NextStepAgentResult,
    NextStepLLMOutput,
    OrchestratorAction,
    RecommendedAction,
)
from followup.next_step.agents.next_step.scoring import score_recommendations
from followup.next_step.context.schemas import DealContext
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ELIGIBLE_EVENT_TYPES: frozenset[FollowUpEventType] = frozenset({
    FollowUpEventType.OPPORTUNITY_CREATED,
    FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
    FollowUpEventType.MEETING_COMPLETED,
})

LLM_FAILURE_SUMMARY = (
    "The LLM call failed. No recommendations could be generated for this event. "
    "The Orchestrator may retry this agent when the provider is available."
)

_LLM_RETRY_SUFFIX = (
    "\n\nREMINDER: Every action object MUST include orchestrator_tool "
    "(one of create_task, schedule_meeting, send_email, update_opportunity, "
    "log_activity, create_reminder) and orchestrator_instruction."
)

# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------


def _is_closed(stage: str) -> bool:
    """Return True for any closed stage variant (Closed, Closed Won, Closed Lost)."""
    return stage.lower().startswith("closed")


def _skip_reason(
    context: DealContext, event: FollowUpEvent, *, require_eligible_event: bool = True
) -> str | None:
    if _is_closed(context.opportunity.stage):
        return f"Opportunity is closed (stage: {context.opportunity.stage})"
    if context.company is None:
        return "Opportunity has no linked company — insufficient context for recommendations"
    # The event-type gate only applies to CRM-lifecycle invocation. When the
    # Orchestrator drives the agent with an explicit trigger description, it has
    # already decided a recommendation is warranted, so the gate is bypassed.
    if require_eligible_event and event.event_type not in _ELIGIBLE_EVENT_TYPES:
        return f"Event type '{event.event_type.value}' is not eligible for Next Step recommendations"
    return None


def _empty_result(reason: str) -> NextStepAgentResult:
    return NextStepAgentResult(
        recommended_actions=[],
        summary_reasoning="",
        confidence=0.0,
        skipped=True,
        skip_reason=reason,
    )


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _ensure_opportunity_id(instruction: str, opp_id: str) -> str:
    if opp_id in instruction:
        return instruction
    return f"On opportunity {opp_id}: {instruction}"


def _ensure_evidence(evidence: list[str]) -> list[str]:
    return evidence if evidence else ["No specific evidence cited"]


def _to_recommended_actions(llm_output: NextStepLLMOutput, opp_id: str) -> list[RecommendedAction]:
    actions: list[RecommendedAction] = []
    for item in llm_output.actions:
        instruction = _ensure_opportunity_id(item.orchestrator_instruction, opp_id)
        actions.append(
            RecommendedAction(
                action_type=item.action_type,
                title=item.title,
                description=item.description,
                priority=item.priority,
                reasoning=item.reasoning,
                evidence=_ensure_evidence(item.evidence),
                profile_fact_refs=item.profile_fact_refs,
                orchestrator_action=OrchestratorAction(
                    tool=item.orchestrator_tool,
                    instruction=instruction,
                    params={"opportunity_id": opp_id},
                ),
            )
        )
    return actions


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def run_next_step_agent(
    context: DealContext,
    event: FollowUpEvent,
    *,
    trigger_context: str | None = None,
    model: str | None = None,
) -> NextStepAgentResult:
    """Run the Next Step Intelligence Agent for the given deal context and event.

    Called exclusively by the Orchestrator. Returns a NextStepAgentResult
    with up to 5 scored recommendations the Orchestrator may choose to execute.

    Args:
        context: Full deal context assembled by the Orchestrator.
        event:   The CRM event that triggered this agent run.
        trigger_context: Optional natural-language description of *what made the
            agent run* (the inbound message / query / signal). The Orchestrator
            owns the raw trigger; it hands the agent a distilled description so
            the agent reasons about the cause, not the raw email.
        model: Optional model alias/slug overriding the env default (the agent
            runs on the orchestrator's chosen subagent model).

    Returns:
        NextStepAgentResult with recommended_actions sorted by priority (1 = most urgent).
        Returns skipped=True when the deal is closed, has no company, or the
        event type is not eligible. Returns an empty result (skipped=False) on
        LLM provider failure so the Orchestrator can distinguish retriable
        failures from intentional skips.
    """
    reason = _skip_reason(
        context, event, require_eligible_event=trigger_context is None
    )
    if reason:
        logger.info(
            "Next Step Agent skipping opportunity_id=%s: %s",
            context.opportunity.id,
            reason,
        )
        return _empty_result(reason)

    prompt = build_next_step_prompt(context, trigger_context=trigger_context)

    llm_output: NextStepLLMOutput | None = None
    for attempt, attempt_prompt in enumerate((prompt, prompt + _LLM_RETRY_SUFFIX)):
        try:
            llm_output = await call_llm_json(
                attempt_prompt, schema=NextStepLLMOutput, model=model
            )
            break
        except LLMCallError:
            if attempt == 0:
                logger.warning(
                    "Next Step Agent LLM call failed (attempt 1); retrying for "
                    "opportunity_id=%s event_id=%s",
                    context.opportunity.id,
                    event.event_id,
                )
                continue
            logger.exception(
                "Next Step Agent LLM call failed for opportunity_id=%s event_id=%s",
                context.opportunity.id,
                event.event_id,
            )
            return NextStepAgentResult(
                recommended_actions=[],
                summary_reasoning=LLM_FAILURE_SUMMARY,
                confidence=0.0,
                skipped=False,
                skip_reason=None,
            )

    if llm_output is None:
        return NextStepAgentResult(
            recommended_actions=[],
            summary_reasoning=LLM_FAILURE_SUMMARY,
            confidence=0.0,
            skipped=False,
            skip_reason=None,
        )

    actions = _to_recommended_actions(llm_output, context.opportunity.id)
    scored = score_recommendations(actions, context)

    return NextStepAgentResult(
        recommended_actions=scored,
        summary_reasoning=llm_output.summary_reasoning,
        confidence=llm_output.confidence,
        skipped=False,
        skip_reason=None,
    )
