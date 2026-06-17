"""Tests for the Next Step Intelligence Agent (Person 2).

All external dependencies (call_llm_json) are mocked — these tests
never make network calls and require no API keys.

No RAG/retrieval — the agent uses internal tools (tools.py) instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.llm_client import LLMCallError
from followup.agents.next_step.next_step_agent import run_next_step_agent
from followup.agents.next_step.schemas import (
    NextStepLLMActionItem,
    NextStepLLMOutput,
    OrchestratorAction,
    RecommendedAction,
)
from followup.agents.next_step.scoring import score_recommendations
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent, FollowUpEventType

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_context(filename: str) -> DealContext:
    with open(FIXTURES_DIR / filename, encoding="utf-8") as handle:
        return DealContext.model_validate(json.load(handle))


def _make_event(
    event_type: FollowUpEventType,
    opportunity_id: str,
    workspace_id: str = "workspace-1",
) -> FollowUpEvent:
    return FollowUpEvent(
        event_id=str(uuid4()),
        idempotency_key=f"{workspace_id}:{event_type.value}:{opportunity_id}:{uuid4()}",
        event_type=event_type,
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        user_id="user-1",
        occurred_at=datetime.now(timezone.utc),
    )


def _sample_llm_output(opportunity_id: str = "opp-1001") -> NextStepLLMOutput:
    return NextStepLLMOutput(
        actions=[
            NextStepLLMActionItem(
                action_type="qualify_authority",
                title="Identify the decision maker",
                description="No contact is flagged as decision maker.",
                priority=1,
                reasoning="BANT Authority gap blocks move to Proposal.",
                evidence=["No contact has is_decision_maker=true.", "BANT: Authority unconfirmed."],
                profile_fact_refs=["fact-1"],
                orchestrator_tool="create_task",
                orchestrator_instruction=f"Create a task on opportunity {opportunity_id}: Identify economic buyer",
            ),
            NextStepLLMActionItem(
                action_type="schedule_demo",
                title="Schedule a product demo",
                description="Set up a demo focused on inventory reconciliation.",
                priority=2,
                reasoning="No demo has occurred yet; buyer has described a clear pain point.",
                evidence=["Timeline: Jordan described pain with manual inventory reconciliation."],
                profile_fact_refs=[],
                orchestrator_tool="schedule_meeting",
                orchestrator_instruction=f"Schedule a demo meeting on opportunity {opportunity_id}",
            ),
            NextStepLLMActionItem(
                action_type="resolve_overdue_task",
                title="Resolve overdue discovery call task",
                description="The discovery call task is overdue — reschedule it.",
                priority=3,
                reasoning="Overdue task signals stalled momentum in Discovery.",
                evidence=["Task 'Schedule discovery call with operations team' is overdue."],
                profile_fact_refs=[],
                orchestrator_tool="create_task",
                orchestrator_instruction=f"Reschedule overdue task on opportunity {opportunity_id}",
            ),
        ],
        summary_reasoning="Three blockers in Discovery: authority gap, no demo, overdue task.",
        confidence=0.78,
    )


def _action(
    action_type: str,
    title: str,
    description: str,
    priority: int,
    evidence: list[str] | None = None,
) -> RecommendedAction:
    return RecommendedAction(
        action_type=action_type,
        title=title,
        description=description,
        priority=priority,
        reasoning="reasoning",
        evidence=evidence or ["evidence"],
        profile_fact_refs=[],
        orchestrator_action=OrchestratorAction(
            tool="create_task",
            instruction=f"Create a task: {title}",
            params={},
        ),
    )


# ---------------------------------------------------------------------------
# 1-3. Skip logic — closed stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_on_closed_won():
    context = _load_context("next_step_context_proposal.json")
    context = context.model_copy(update={"opportunity": context.opportunity.model_copy(update={"stage": "Closed Won"})})
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    result = await run_next_step_agent(context, event)

    assert result.skipped is True
    assert result.skip_reason is not None
    assert "closed" in result.skip_reason.lower()
    assert result.recommended_actions == []


@pytest.mark.asyncio
async def test_skip_on_closed_lost():
    context = _load_context("next_step_context_proposal.json")
    context = context.model_copy(update={"opportunity": context.opportunity.model_copy(update={"stage": "Closed Lost"})})
    event = _make_event(FollowUpEventType.MEETING_COMPLETED, context.opportunity.id)

    result = await run_next_step_agent(context, event)

    assert result.skipped is True
    assert "closed" in result.skip_reason.lower()
    assert result.recommended_actions == []


@pytest.mark.asyncio
async def test_skip_on_unified_closed_stage():
    """The canonical 'Closed' stage (unified status) must also be skipped."""
    context = _load_context("next_step_context_proposal.json")
    context = context.model_copy(update={"opportunity": context.opportunity.model_copy(update={"stage": "Closed"})})
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    result = await run_next_step_agent(context, event)

    assert result.skipped is True
    assert "closed" in result.skip_reason.lower()


# ---------------------------------------------------------------------------
# 4-5. Skip logic — missing company and ineligible event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_company_missing():
    context = _load_context("next_step_context_discovery.json")
    context = context.model_copy(update={"company": None})
    event = _make_event(FollowUpEventType.OPPORTUNITY_CREATED, context.opportunity.id)

    result = await run_next_step_agent(context, event)

    assert result.skipped is True
    assert "company" in result.skip_reason.lower()


@pytest.mark.asyncio
async def test_skip_on_ineligible_event_type():
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.TASK_COMPLETED, context.opportunity.id)

    result = await run_next_step_agent(context, event)

    assert result.skipped is True
    assert "task_completed" in result.skip_reason


# ---------------------------------------------------------------------------
# 6-7. Happy path — recommendations are well-formed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_stage_changed_returns_recommendations(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_llm(prompt: str, schema):
        assert schema is NextStepLLMOutput
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr("followup.agents.next_step.next_step_agent.call_llm_json", fake_llm)
    result = await run_next_step_agent(context, event)

    assert result.skipped is False
    assert result.skip_reason is None
    assert 1 <= len(result.recommended_actions) <= 5
    assert result.summary_reasoning


@pytest.mark.asyncio
async def test_every_recommendation_has_reasoning_and_evidence(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_llm(prompt: str, schema):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr("followup.agents.next_step.next_step_agent.call_llm_json", fake_llm)
    result = await run_next_step_agent(context, event)

    assert result.recommended_actions, "expected at least one recommendation"
    for action in result.recommended_actions:
        assert action.reasoning.strip()
        assert len(action.evidence) > 0


# ---------------------------------------------------------------------------
# 8-9. OrchestratorAction — tool and instruction correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_instruction_contains_opportunity_id(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_llm(prompt: str, schema):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr("followup.agents.next_step.next_step_agent.call_llm_json", fake_llm)
    result = await run_next_step_agent(context, event)

    for action in result.recommended_actions:
        assert context.opportunity.id in action.orchestrator_action.instruction


@pytest.mark.asyncio
async def test_orchestrator_action_has_tool_and_params(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_llm(prompt: str, schema):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr("followup.agents.next_step.next_step_agent.call_llm_json", fake_llm)
    result = await run_next_step_agent(context, event)

    for action in result.recommended_actions:
        assert action.orchestrator_action.tool
        assert action.orchestrator_action.params.get("opportunity_id") == context.opportunity.id


# ---------------------------------------------------------------------------
# 10. Priority ordering (scoring — direct unit test)
# ---------------------------------------------------------------------------


def test_priority_ordering_boosts_overdue_and_stage_actions():
    context = _load_context("next_step_context_discovery.json")  # Discovery stage, has overdue task

    actions = [
        _action("send_proposal", "Send proposal", "Send a proposal to the buyer", priority=2),
        _action(
            "resolve_overdue_task",
            "Resolve overdue Discovery task",
            "Address the overdue task for this Discovery deal",
            priority=3,
            evidence=["Task is overdue"],
        ),
        _action("schedule_demo", "Schedule a demo", "Generic demo scheduling", priority=3),
    ]

    ranked = score_recommendations(actions, context)

    assert len(ranked) <= 5
    # "resolve_overdue_task": mentions "Discovery" (stage match) + "overdue" → 2 boosts → priority 1
    assert ranked[0].action_type == "resolve_overdue_task"
    assert [a.priority for a in ranked] == sorted(a.priority for a in ranked)


# ---------------------------------------------------------------------------
# 11. LLM failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_handled_gracefully(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_llm(prompt: str, schema):
        raise LLMCallError("provider timeout")

    monkeypatch.setattr("followup.agents.next_step.next_step_agent.call_llm_json", fake_llm)
    result = await run_next_step_agent(context, event)

    assert result.skipped is False      # retriable failure, not an intentional skip
    assert result.skip_reason is None
    assert result.recommended_actions == []
    assert result.confidence == 0.0
    assert result.summary_reasoning     # non-empty fallback message


# ---------------------------------------------------------------------------
# 12-13. Maximum 5 recommendations cap
# ---------------------------------------------------------------------------


def test_scoring_caps_at_five_recommendations():
    context = _load_context("next_step_context_proposal.json")

    actions = [
        _action("a1", "Action 1", "First action", priority=1),
        _action("a2", "Action 2", "Second action", priority=2),
        _action("a3", "Action 3", "Third action", priority=3),
        _action("a4", "Action 4", "Fourth action", priority=4),
        _action("a5", "Action 5", "Fifth action", priority=5),
        _action("a6", "Action 6", "Sixth action", priority=5),
    ]

    ranked = score_recommendations(actions, context)

    assert len(ranked) == 5


@pytest.mark.asyncio
async def test_meeting_completed_returns_at_most_five_recommendations(monkeypatch):
    context = _load_context("next_step_context_proposal.json")
    event = _make_event(FollowUpEventType.MEETING_COMPLETED, context.opportunity.id)

    async def fake_llm(prompt: str, schema):
        return _sample_llm_output(opportunity_id=context.opportunity.id)

    monkeypatch.setattr("followup.agents.next_step.next_step_agent.call_llm_json", fake_llm)
    result = await run_next_step_agent(context, event)

    assert len(result.recommended_actions) <= 5
