"""Tests for the Next Step Intelligence Agent.

All LLM calls are mocked via _run_llm_plan — tests never make network calls
and require no API keys. The tool-calling gather phase (Phase 1) and structured
output phase (Phase 2) are replaced by a single function that returns a
NextStepLLMOutput directly.

Planning-skill discovery tools (list_planning_skills, read_planning_skill) are
exercised independently in the tool tests below, with the DB mocked so they fall
back to the bundled defaults.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.llm_client import LLMCallError
from followup.next_step.agents.next_step.next_step_agent import run_next_step_agent
from followup.next_step.agents.next_step.schemas import (
    NextStepLLMActionItem,
    NextStepLLMOutput,
    OrchestratorAction,
    RecommendedAction,
)
from followup.next_step.agents.next_step.scoring import score_recommendations
from followup.next_step.agents.next_step.tools import (
    compute_bant_gaps,
    compute_engagement_signals,
    list_planning_skills,
    planner_catalog_text,
    read_planning_skill,
)
from followup.next_step.context.schemas import DealContext
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType

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

    async def fake_plan(messages, *, model=None):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
    result = await run_next_step_agent(context, event)

    assert result.skipped is False
    assert result.skip_reason is None
    assert 1 <= len(result.recommended_actions) <= 5
    assert result.summary_reasoning


@pytest.mark.asyncio
async def test_every_recommendation_has_reasoning_and_evidence(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_plan(messages, *, model=None):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
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

    async def fake_plan(messages, *, model=None):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
    result = await run_next_step_agent(context, event)

    for action in result.recommended_actions:
        assert context.opportunity.id in action.orchestrator_action.instruction


@pytest.mark.asyncio
async def test_orchestrator_action_has_tool_and_params(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_plan(messages, *, model=None):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
    result = await run_next_step_agent(context, event)

    for action in result.recommended_actions:
        assert action.orchestrator_action.tool
        assert action.orchestrator_action.params.get("opportunity_id") == context.opportunity.id


# ---------------------------------------------------------------------------
# 10. Priority ordering (scoring — direct unit test)
# ---------------------------------------------------------------------------


def test_priority_ordering_boosts_overdue_and_stage_actions():
    context = _load_context("next_step_context_discovery.json")

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
    assert ranked[0].action_type == "resolve_overdue_task"
    assert [a.priority for a in ranked] == sorted(a.priority for a in ranked)


# ---------------------------------------------------------------------------
# 11. LLM failure handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_handled_gracefully(monkeypatch):
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    async def fake_plan(messages, *, model=None):
        raise LLMCallError("provider timeout")

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
    result = await run_next_step_agent(context, event)

    assert result.skipped is False
    assert result.skip_reason is None
    assert result.recommended_actions == []
    assert result.confidence == 0.0
    assert result.summary_reasoning


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

    async def fake_plan(messages, *, model=None):
        return _sample_llm_output(opportunity_id=context.opportunity.id)

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
    result = await run_next_step_agent(context, event)

    assert len(result.recommended_actions) <= 5


# ---------------------------------------------------------------------------
# 14. trigger_context bypasses the event-type gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ineligible_event_bypassed_when_trigger_context_provided(monkeypatch):
    """With trigger_context supplied, the event-type gate is skipped."""
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.TASK_COMPLETED, context.opportunity.id)

    async def fake_plan(messages, *, model=None):
        return _sample_llm_output(context.opportunity.id)

    monkeypatch.setattr(
        "followup.next_step.agents.next_step.next_step_agent._run_llm_plan", fake_plan
    )
    result = await run_next_step_agent(context, event, trigger_context="Inbound email from buyer")

    assert result.skipped is False
    assert len(result.recommended_actions) >= 1


# ---------------------------------------------------------------------------
# 15. Knowledge tools — file-reading (no LLM)
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_db_skills(monkeypatch):
    """Force the bundled-file fallback so planner tests don't depend on the DB."""
    from followup.knowledge import skill_store

    async def _empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(skill_store, "fetch_skills_by_prefix", _empty)
    monkeypatch.setattr(skill_store, "get_skill_content", lambda *_a, **_k: None)


def test_planner_catalog_lists_default_skills(_no_db_skills):
    catalog = planner_catalog_text()
    # Defaults include the stage playbooks plus BANT and best practices.
    assert "followup-playbook-negotiation" in catalog
    assert "followup-bant" in catalog
    assert "followup-best-practices" in catalog


def test_list_planning_skills_tool_returns_catalog(_no_db_skills):
    result = list_planning_skills.invoke({})
    assert "followup-playbook-discovery" in result


def test_read_planning_skill_returns_bundled_content(_no_db_skills):
    result = read_planning_skill.invoke({"name": "followup-bant"})
    assert "Budget" in result
    assert "Authority" in result


def test_read_planning_skill_unknown_lists_available(_no_db_skills):
    result = read_planning_skill.invoke({"name": "followup-planner-nope"})
    assert "No planning skill named" in result
    assert "followup-playbook-discovery" in result


# ---------------------------------------------------------------------------
# 16. Signal functions — deterministic computation
# ---------------------------------------------------------------------------


def test_compute_bant_gaps_missing_all():
    context = _load_context("next_step_context_discovery.json")
    context = context.model_copy(update={"active_facts": [], "contacts": [], "timeline": []})
    signals = compute_bant_gaps(context)

    assert signals.qualification_score == 0
    assert signals.is_fully_qualified is False
    assert all(g.status == "missing" for g in signals.gaps)


def test_compute_engagement_signals_cold():
    context = _load_context("next_step_context_discovery.json")
    signals = compute_engagement_signals(context)

    assert signals.status in ("healthy", "stalled", "cold", "declining", "unknown")
    assert signals.days_since_last is None or isinstance(signals.days_since_last, int)
    assert isinstance(signals.risk_flags, list)


def test_compute_bant_gaps_budget_partial_from_deal_amount():
    """A deal amount (with no budget fact) is a partial Budget signal, not missing."""
    context = _load_context("next_step_context_discovery.json")
    context = context.model_copy(
        update={
            "active_facts": [],
            "opportunity": context.opportunity.model_copy(update={"amount": 62000.0}),
        }
    )
    signals = compute_bant_gaps(context)

    budget = next(g for g in signals.gaps if g.dimension == "Budget")
    assert budget.status == "partial"
    assert "62,000" in budget.detail


def test_compute_engagement_signals_unknown_when_no_activity():
    """No activity on record → status 'unknown', days None, no fabricated sentinel."""
    context = _load_context("next_step_context_discovery.json")
    context = context.model_copy(
        update={
            "engagement": context.engagement.model_copy(
                update={
                    "days_since_last_activity": None,
                    "activity_count_14d": 0,
                    "activity_count_prior_14d": 0,
                }
            ),
            "tasks": [],
        }
    )
    signals = compute_engagement_signals(context)

    assert signals.status == "unknown"
    assert signals.days_since_last is None
    assert not any("999" in flag for flag in signals.risk_flags)
    assert not any("None days" in flag for flag in signals.risk_flags)
