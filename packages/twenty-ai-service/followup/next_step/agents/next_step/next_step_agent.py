"""Next Step Intelligence Agent — tool-using planning agent.

The agent receives raw deal context and the triggering signal from the
orchestrator. It is shown a catalog of the planning skills available in this
workspace (company-edited skills from the Skills tab plus bundled defaults),
loads the ones relevant to the deal, then produces a structured plan of 1–5
next actions grounded in the deal facts.

Planning loop (two phases):
    Phase 1 — gather: LLM calls read_planning_skill (and may call
              list_planning_skills) to load the guidance relevant to this deal.
              We execute each tool call locally (DB read + file fallback) and
              feed results back as ToolMessages. Up to _MAX_TOOL_ROUNDS rounds.
    Phase 2 — plan: with all tool results in context, LLM produces
              NextStepLLMOutput via structured output.

The orchestrator owns trigger classification; this agent receives only the raw
trigger signal and makes its own judgement about what actions to take.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from agent.llm_client import LLMCallError, get_chat_model
from followup.next_step.agents.next_step.prompts import SYSTEM_PROMPT, build_deal_context_message
from followup.next_step.agents.next_step.schemas import (
    NextStepAgentResult,
    NextStepLLMOutput,
    OrchestratorAction,
    RecommendedAction,
)
from followup.next_step.agents.next_step.scoring import score_recommendations
from followup.next_step.agents.next_step.tools import (
    PLANNER_TOOLS,
    compute_bant_gaps,
    compute_engagement_signals,
    planner_catalog_text,
)
from followup.next_step.context.schemas import DealContext
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType

logger = logging.getLogger(__name__)

_ELIGIBLE_EVENT_TYPES: frozenset[FollowUpEventType] = frozenset({
    FollowUpEventType.OPPORTUNITY_CREATED,
    FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
    FollowUpEventType.MEETING_COMPLETED,
})

_MAX_TOOL_ROUNDS = 3  # safety cap on the gather loop

_LLM_FAILURE_SUMMARY = (
    "The LLM call failed. No recommendations could be generated for this event. "
    "The orchestrator may retry when the provider is available."
)


# ---------------------------------------------------------------------------
# Skip logic
# ---------------------------------------------------------------------------


def _is_closed(stage: str) -> bool:
    return stage.lower().startswith("closed")


def _skip_reason(
    context: DealContext,
    event: FollowUpEvent,
    *,
    require_eligible_event: bool = True,
) -> str | None:
    if _is_closed(context.opportunity.stage):
        return f"Opportunity is closed (stage: {context.opportunity.stage})"
    if context.company is None:
        return "Opportunity has no linked company — insufficient context for recommendations"
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
# Tool execution
# ---------------------------------------------------------------------------


def _execute_tool_call(call: dict[str, Any]) -> str:
    name = call.get("name", "")
    args = call.get("args", {})
    for t in PLANNER_TOOLS:
        if t.name == name:
            try:
                return str(t.invoke(args))
            except Exception as err:  # noqa: BLE001
                return f"Tool {name} failed: {err}"
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Planning phases — mockable in tests
# ---------------------------------------------------------------------------


async def _run_llm_plan(
    messages: list,
    *,
    model: str | None = None,
) -> NextStepLLMOutput:
    """Tool-calling gather phase followed by structured output generation.

    Phase 1: LLM calls tools (read_stage_playbook, read_bant_framework, …) to
    gather the knowledge it needs. We execute each tool call locally (pure file
    reads) and return results as ToolMessages.

    Phase 2: with all tool results in context, LLM produces the structured plan.

    This is the single LLM-touching entry point and is separately mockable in
    tests so the skip and result-assembly logic can be verified without a live
    LLM.
    """
    try:
        llm = get_chat_model(model)
        llm_with_tools = llm.bind_tools(PLANNER_TOOLS)

        for _ in range(_MAX_TOOL_ROUNDS):
            response = await llm_with_tools.ainvoke(messages)
            messages.append(response)

            tool_calls: list[dict[str, Any]] = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                break

            for call in tool_calls:
                result = _execute_tool_call(call)
                messages.append(ToolMessage(content=result, tool_call_id=call["id"]))

        structured_llm = llm.with_structured_output(NextStepLLMOutput)
        messages.append(
            HumanMessage(
                content=(
                    "You have read the relevant knowledge. "
                    "Now produce your structured recommendations.\n\n"
                    "CRITICAL — priority is ABSOLUTE urgency, NOT a ranking. Do "
                    "not reflexively give your top action priority 1. Calibrate "
                    "each action on its own:\n"
                    "- priority 1–2 ONLY for active risk, a competitor displacing "
                    "you, an escalation, or a hard deadline within days.\n"
                    "- priority 3 for normal forward progress (a question to "
                    "answer, a proposal/quote to send, a routine next step).\n"
                    "- priority 4–5 for positive-momentum or routine-cadence "
                    "touches with no time pressure.\n"
                    "A healthy deal moving along well — even one with great news "
                    "like budget approved or a successful pilot — is priority 3 "
                    "unless the buyer attached a near-term deadline."
                )
            )
        )
        result = await structured_llm.ainvoke(messages)
        if isinstance(result, NextStepLLMOutput):
            return result
        return NextStepLLMOutput.model_validate(result)
    except LLMCallError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LLMCallError(f"Planning failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def _ensure_opportunity_id(instruction: str, opp_id: str) -> str:
    return instruction if opp_id in instruction else f"On opportunity {opp_id}: {instruction}"


def _to_recommended_actions(
    llm_output: NextStepLLMOutput, opp_id: str
) -> list[RecommendedAction]:
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
                evidence=item.evidence if item.evidence else ["No specific evidence cited"],
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
    """Run the Next Step Intelligence Agent for the given deal context and trigger.

    Called exclusively by the orchestrator. Receives raw deal context plus the
    raw triggering signal (never pre-classified decisions) and returns a
    structured plan of 1–5 scored next actions.

    The agent reads its own stage playbook and BANT framework via tool calls
    before producing recommendations — it is not told what type of email arrived
    or what urgency the triage classifier assigned.

    Args:
        context: Full deal context assembled by the orchestrator.
        event: The CRM event that triggered this agent run.
        trigger_context: Raw description of what triggered this run — the
            inbound email subject/body or a free-text signal. Never a
            pre-classified label (type/urgency). The agent reasons from the
            raw trigger and its own knowledge.
        model: Optional model alias overriding the env default.

    Returns:
        NextStepAgentResult with recommended_actions sorted by priority (1 = most urgent).
        skipped=True for closed deals, missing company, or ineligible event types.
        skipped=False with empty actions on LLM failure (retriable, not an intentional skip).
    """
    reason = _skip_reason(context, event, require_eligible_event=trigger_context is None)
    if reason:
        logger.info("Next Step Agent skipping %s: %s", context.opportunity.id, reason)
        return _empty_result(reason)

    bant = compute_bant_gaps(context)
    engagement = compute_engagement_signals(context)
    # Discover the planning skills available at run time so the agent can load
    # the ones it deems relevant (DB skills from the Skills tab + bundled defaults).
    planning_catalog = planner_catalog_text()
    context_msg = build_deal_context_message(
        context, trigger_context, bant, engagement, planning_catalog
    )

    messages: list = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=context_msg),
    ]

    try:
        llm_output = await _run_llm_plan(messages, model=model)
    except LLMCallError:
        logger.exception(
            "Next Step Agent LLM call failed for opportunity_id=%s event_id=%s",
            context.opportunity.id,
            event.event_id,
        )
        return NextStepAgentResult(
            recommended_actions=[],
            summary_reasoning=_LLM_FAILURE_SUMMARY,
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
