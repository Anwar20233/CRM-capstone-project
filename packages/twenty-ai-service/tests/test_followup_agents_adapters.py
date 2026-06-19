"""Unit tests for followup/agents/ — the anti-corruption adapter layer.

Fakes only: no Postgres, no bridge, no real LLM. Covers:
* Context mapping (profile.DealContext → next-step / drafting contexts).
* Next-step OUT mapping: tool→kind, evidence/manner preserved, dedupe, skip,
  and the LLM-empty fallback.
* Drafting OUT mapping: draft-type selection, EmailDraft → DraftResult, and the
  mock fallback when the agent yields nothing.
* run_tasks dispatch: the small writers run CONCURRENTLY with calendar → draft
  ordering preserved, and one failing writer is isolated.
* The idempotent email outbox seam (build_send_email_args / send_drafted_email).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Optional

import pytest

from followup.agents.drafting_adapter import OrchestratorDraftingAgent, _draft_type_for
from followup.agents.mapping import to_drafting_context, to_next_step_context
from followup.agents.next_step_adapter import OrchestratorNextStepAgent
from followup.contracts.drafting import DraftRequest
from followup.emailer.agents.drafting.schemas import DraftType, EmailDraft
from followup.next_step.agents.next_step.schemas import (
    NextStepAgentResult,
    OrchestratorAction,
    RecommendedAction,
)
from followup.profile.schemas import ContactSummary, DealContext


def make_deal(*, risk_score: Optional[float] = 0.2, activities=None) -> DealContext:
    return DealContext(
        opportunity_id=str(uuid.uuid4()),
        opportunity_name="Acme Expansion",
        deal_stage="PROPOSAL",
        deal_value=50_000.0,
        company_name="Acme",
        profile_narrative="Acme is evaluating an expansion of their seats.",
        contacts=[
            ContactSummary(
                crm_id=str(uuid.uuid4()),
                name="Dana Buyer",
                role="VP Ops",
                email="dana@acme.com",
                facts=[],
            )
        ],
        recent_activities=activities or [],
        key_relationships=[],
        open_concerns=[{"id": "c1", "content": "Worried about Q3 timeline", "fact_type": "concern"}],
        risk_score=risk_score,
    )


def _recommendation(tool: str, *, priority: int = 1, action_type: str = "outreach") -> RecommendedAction:
    return RecommendedAction(
        action_type=action_type,
        title="Reassure on timeline",
        description="Email Dana to reassure on the Q3 timeline.",
        priority=priority,
        reasoning="They raised a timeline concern that risks the deal.",
        evidence=["Open concern: Q3 timeline", "Stage: PROPOSAL"],
        profile_fact_refs=["c1"],
        orchestrator_action=OrchestratorAction(tool=tool, instruction="On opportunity X: email Dana"),
    )


# ===========================================================================
# Context mapping
# ===========================================================================


def test_to_next_step_context_maps_core_fields_and_company():
    deal = make_deal()
    ctx = to_next_step_context(deal)
    assert ctx.opportunity.id == str(deal.opportunity_id)
    assert ctx.opportunity.stage == "PROPOSAL"
    assert ctx.opportunity.amount == 50_000.0
    # The agent skips deals with no company — the mapper always supplies one.
    assert ctx.company is not None and ctx.company.name == "Acme"
    assert ctx.contacts[0].name == "Dana Buyer"
    # The open concern becomes a RISK-category active fact.
    assert any(f.value.startswith("Worried about Q3") for f in ctx.active_facts)


def test_to_next_step_context_engagement_from_activities():
    deal = make_deal(activities=[{"type": "note", "date": "2020-01-01T00:00:00+00:00", "summary": "Call"}])
    ctx = to_next_step_context(deal)
    # An old activity reads as a large engagement gap, not a crash.
    assert ctx.engagement.days_since_last_activity > 14
    assert ctx.timeline[0].summary == "Call"


def test_to_next_step_context_no_activity_is_unknown_not_sentinel():
    """No activity → days_since_last_activity is None, never a fabricated 999."""
    deal = make_deal(activities=[])
    ctx = to_next_step_context(deal)
    assert ctx.engagement.days_since_last_activity is None


def test_inbound_signal_resets_engagement_clock():
    """The triggering email folds into the timeline so the deal isn't 'cold'."""
    from datetime import datetime, timezone

    deal = make_deal(activities=[])
    signal = {
        "type": "email",
        "date": datetime.now(timezone.utc).isoformat(),
        "summary": "Ready to move forward",
    }
    ctx = to_next_step_context(deal, inbound_signal=signal)

    assert ctx.engagement.days_since_last_activity == 0
    assert ctx.engagement.activity_count_14d == 1
    assert any(item.summary == "Ready to move forward" for item in ctx.timeline)


def test_to_drafting_context_picks_recipient_contact():
    deal = make_deal()
    ctx = to_drafting_context(deal, recipient_email="dana@acme.com")
    assert ctx.contact.email == "dana@acme.com"
    assert ctx.company.name == "Acme"
    assert ctx.opportunity.stage == "PROPOSAL"


# ===========================================================================
# Next-step OUT mapping (no LLM — call _to_plan directly)
# ===========================================================================


def test_next_step_out_mapping_tool_to_kind_and_grounding():
    deal = make_deal()
    agent = OrchestratorNextStepAgent()
    result = NextStepAgentResult(
        recommended_actions=[_recommendation("send_email", priority=1)],
        summary_reasoning="Reassure to protect the deal.",
        confidence=0.8,
    )
    plan = agent._to_plan(result, str(deal.opportunity_id), deal)
    assert plan.steps[0].kind == "draft_email"  # send_email → draft_email
    # Evidence + manner preserved for rep review / downstream writers.
    assert plan.steps[0].metadata["approach"].startswith("They raised")
    assert plan.steps[0].metadata["evidence"]
    assert plan.metadata["recipient_email"] == "dana@acme.com"


def test_next_step_out_mapping_dedupes_by_kind_keeps_top_priority():
    deal = make_deal()
    agent = OrchestratorNextStepAgent()
    result = NextStepAgentResult(
        recommended_actions=[
            _recommendation("send_email", priority=4),
            _recommendation("send_email", priority=1),  # same kind, higher priority
            _recommendation("schedule_meeting", priority=2),
        ],
        summary_reasoning="x",
        confidence=0.5,
    )
    plan = agent._to_plan(result, str(deal.opportunity_id), deal)
    kinds = [s.kind for s in plan.steps]
    assert kinds.count("draft_email") == 1
    assert "book_meeting" in kinds
    # The kept draft_email step is the priority-1 one.
    draft_step = next(s for s in plan.steps if s.kind == "draft_email")
    assert draft_step.metadata["source_priority"] == 1


def test_next_step_out_mapping_skip_yields_no_action():
    deal = make_deal()
    agent = OrchestratorNextStepAgent()
    result = NextStepAgentResult(recommended_actions=[], skipped=True, skip_reason="Opportunity is closed")
    plan = agent._to_plan(result, str(deal.opportunity_id), deal)
    assert plan.headline_action == "no_action"
    assert plan.steps == []


def test_next_step_out_mapping_empty_falls_back_to_draft():
    deal = make_deal()
    agent = OrchestratorNextStepAgent()
    result = NextStepAgentResult(recommended_actions=[], skipped=False, summary_reasoning="LLM hiccup")
    plan = agent._to_plan(result, str(deal.opportunity_id), deal)
    assert plan.steps and plan.steps[0].kind == "draft_email"


# ===========================================================================
# Drafting OUT mapping
# ===========================================================================


def test_draft_type_selection_from_classification():
    assert _draft_type_for({"type": "buying_signal"}) == DraftType.PROPOSAL_DELIVERY_EMAIL
    assert _draft_type_for({"type": "re_engagement"}) == DraftType.RE_ENGAGEMENT_EMAIL
    # Most inbound types are an active follow-up, not a re-engagement.
    assert _draft_type_for({"type": "objection"}) == DraftType.FOLLOW_UP_EMAIL
    assert _draft_type_for({"type": "unknown"}) == DraftType.FOLLOW_UP_EMAIL


def test_slot_lines_formats_available_slots():
    from followup.agents.drafting_adapter import _slot_lines

    slots = [
        {"start": "2026-06-18T14:00:00+00:00", "end": "2026-06-18T14:30:00+00:00", "available": True},
        {"start": "2026-06-18T15:00:00+00:00", "end": "2026-06-18T15:30:00+00:00", "available": False},
    ]
    lines = _slot_lines(slots)
    assert "Proposed meeting times" in lines
    assert "2026-06-18T14:00:00+00:00" in lines
    assert "2026-06-18T15:00:00+00:00" not in lines


def test_drafting_adapter_injects_calendar_slots_into_prompt_context():
    async def _run():
        from followup.agents.drafting_adapter import OrchestratorDraftingAgent
        from followup.contracts.drafting import DraftRequest
        from followup.emailer.agents.drafting.schemas import DraftingAgentResult, DraftType, EmailDraft

        captured: dict = {}

        async def fake_run(*, context, **kwargs):
            captured["notes"] = context.recent_notes
            return DraftingAgentResult(
                email_drafts=[
                    EmailDraft(
                        subject="Re: meeting",
                        body="Tuesday 2pm works.",
                        draft_type=DraftType.FOLLOW_UP_EMAIL,
                        quality_score=0.9,
                        reasoning="offered slot",
                    )
                ],
                proposal_drafts=[],
                reasoning="ok",
                skipped=False,
            )

        import followup.agents.drafting_adapter as mod

        original = mod.run_drafting_agent
        mod.run_drafting_agent = fake_run
        try:
            deal = make_deal()
            request = DraftRequest(
                deal_context=deal,
                intent="Offer meeting times",
                classification={"type": "meeting_request", "urgency": "medium"},
                recipient_email="dana@acme.com",
                available_slots=[
                    {
                        "start": "2026-06-18T14:00:00+00:00",
                        "end": "2026-06-18T14:30:00+00:00",
                        "available": True,
                    }
                ],
                reply_context={"sender_email": "dana@acme.com", "subject": "Schedule?", "body": "Can we meet?"},
            )
            result = await OrchestratorDraftingAgent().run(request)
        finally:
            mod.run_drafting_agent = original

        assert result.body
        assert captured["notes"]
        assert "2026-06-18T14:00:00+00:00" in captured["notes"][0].body

    asyncio.run(_run())


def test_run_tasks_meeting_request_includes_calendar_in_state():
    async def _run():
        from followup.contracts.next_step import NextStepPlan, PlannedStep
        from followup.orchestrator import FollowupTaskRegistry, FollowupTaskSpec
        from followup.orchestrator.nodes import build_nodes

        calendar_payload = {
            "available_slots": [
                {"start": "2026-06-18T14:00:00+00:00", "end": "2026-06-18T14:30:00+00:00", "available": True},
            ],
            "all_busy": False,
            "suggested_alternatives": [],
        }

        async def calendar_handler(ctx):
            return {"calendar": SimpleNamespace(**calendar_payload)}

        async def draft_handler(ctx):
            assert ctx.calendar is not None
            return {
                "draft": SimpleNamespace(
                    subject="Re: Analytics Suite",
                    body="How about Tuesday at 2pm?",
                    recipient_email="alex.rivera@stripe.com",
                )
            }

        registry = FollowupTaskRegistry()
        registry.register(
            FollowupTaskSpec(
                name="check_calendar", role="r", when_to_use="w", instructions="i",
                input_schema={}, handler=calendar_handler,
            )
        )
        registry.register(
            FollowupTaskSpec(
                name="draft_email", role="r", when_to_use="w", instructions="i",
                input_schema={}, handler=draft_handler,
            )
        )

        state = _make_state(
            NextStepPlan(
                steps=[PlannedStep(kind="book_meeting", intent="schedule call")],
                headline_action="schedule_meeting",
                summary="meeting",
            )
        )
        state["classification"] = {"type": "meeting_request", "urgency": "medium", "requires_calendar": True}

        deps = SimpleNamespace(task_registry=registry)
        nodes = build_nodes(deps)  # type: ignore[arg-type]
        out = await nodes["run_tasks"](state)

        assert out["calendar"].available_slots
        assert "2pm" in out["draft"].body or "14:00" in out["draft"].body

    asyncio.run(_run())


def test_drafting_email_to_draft_result_fills_recipient_and_tone():
    deal = make_deal()
    agent = OrchestratorDraftingAgent()
    request = DraftRequest(
        deal_context=deal,
        intent="Reassure on timeline",
        classification={"type": "objection", "urgency": "high"},
        recipient_email="dana@acme.com",
    )
    email = EmailDraft(subject="Re: timeline", body="Hi Dana, ...", draft_type=DraftType.RE_ENGAGEMENT_EMAIL)
    result = agent._to_draft_result(email, request)
    assert result.recipient_email == "dana@acme.com"
    assert result.subject == "Re: timeline"
    assert result.tone == "urgent"  # high urgency → urgent
    assert result.metadata["draft_type"] == "re_engagement_email"


@pytest.mark.asyncio
async def test_drafting_adapter_falls_back_to_mock_on_failure():
    deal = make_deal()
    agent = OrchestratorDraftingAgent()

    async def boom(*args, **kwargs):
        raise RuntimeError("drafter down")

    # Force the real drafter to fail → the adapter returns the mock's draft.
    import followup.agents.drafting_adapter as mod

    original = mod.run_drafting_agent
    mod.run_drafting_agent = boom
    try:
        request = DraftRequest(deal_context=deal, intent="x", recipient_email="dana@acme.com")
        result = await agent.run(request)
    finally:
        mod.run_drafting_agent = original
    assert result.recipient_email == "dana@acme.com"
    assert result.body  # mock produced a body


# ===========================================================================
# run_tasks — concurrent dispatch + ordering + isolation
# ===========================================================================


def _make_state(plan):
    return {
        "entry_point": "email",
        "trigger": {"sender_email": "dana@acme.com"},
        "workspace_id": "ws",
        "classification": {"type": "objection", "urgency": "high"},
        "plan": plan,
        "deal_context": make_deal(),
        "trace": [],
        "status": "running",
    }


@pytest.mark.asyncio
async def test_run_tasks_runs_writers_concurrently_with_calendar_first():
    from followup.contracts.next_step import NextStepPlan, PlannedStep
    from followup.orchestrator import FOLLOWUP_SCOPE, FollowupTaskRegistry, FollowupTaskSpec
    from followup.orchestrator.nodes import build_nodes

    order: list[str] = []
    concurrent: dict[str, int] = {"draft_email": 0, "write_note": 0}
    active = {"n": 0, "max": 0}

    async def make_handler(name: str):
        async def handler(ctx):
            order.append(f"{name}:start")
            if name in concurrent:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                await asyncio.sleep(0.02)
                active["n"] -= 1
            order.append(f"{name}:end")
            if name == "check_calendar":
                return {"calendar": SimpleNamespace(available_slots=[], all_busy=False, suggested_alternatives=[])}
            if name == "draft_email":
                # draft must see the calendar result produced first.
                assert ctx.calendar is not None
                return {"draft": SimpleNamespace(subject="s", body="b")}
            return {"task_results": {name: {"ok": True}}}
        return handler

    registry = FollowupTaskRegistry()
    for name in ("check_calendar", "draft_email", "write_note"):
        registry.register(
            FollowupTaskSpec(
                name=name, role="r", when_to_use="w", instructions="i",
                input_schema={}, handler=await make_handler(name),
            )
        )

    deps = SimpleNamespace(task_registry=registry)
    nodes = build_nodes(deps)  # type: ignore[arg-type]
    plan = NextStepPlan(
        steps=[
            PlannedStep(kind="book_meeting", intent="meet"),
            PlannedStep(kind="draft_email", intent="email"),
            PlannedStep(kind="write_note", intent="note"),
        ],
        headline_action="schedule_meeting",
        summary="s",
    )
    out = await nodes["run_tasks"](_make_state(plan))

    # calendar finished before the draft started (ordering edge preserved).
    assert order.index("check_calendar:end") < order.index("draft_email:start")
    # draft + note overlapped (ran concurrently).
    assert active["max"] >= 2
    assert out["draft"].subject == "s"
    assert out["task_results"]["write_note"] == {"ok": True}


@pytest.mark.asyncio
async def test_run_tasks_isolates_one_failing_writer():
    from followup.contracts.next_step import NextStepPlan, PlannedStep
    from followup.orchestrator import FollowupTaskRegistry, FollowupTaskSpec
    from followup.orchestrator.nodes import build_nodes

    async def good(ctx):
        return {"task_results": {"write_note": {"ok": True}}}

    async def bad(ctx):
        raise RuntimeError("draft exploded")

    registry = FollowupTaskRegistry()
    registry.register(FollowupTaskSpec(name="write_note", role="r", when_to_use="w", instructions="i", input_schema={}, handler=good))
    registry.register(FollowupTaskSpec(name="draft_email", role="r", when_to_use="w", instructions="i", input_schema={}, handler=bad))

    deps = SimpleNamespace(task_registry=registry)
    nodes = build_nodes(deps)  # type: ignore[arg-type]
    plan = NextStepPlan(
        steps=[PlannedStep(kind="draft_email", intent="e"), PlannedStep(kind="write_note", intent="n")],
        headline_action="follow_up_call",
        summary="s",
    )
    out = await nodes["run_tasks"](_make_state(plan))
    assert out.get("status") != "failed" and "error" not in out  # run survives
    assert out["task_results"]["write_note"] == {"ok": True}  # the other still produced
    assert "draft" not in out  # the failing writer produced nothing


# ===========================================================================
# Email outbox seam — idempotency
# ===========================================================================


def test_build_send_email_args_shapes_html_payload():
    from followup.api.execution import build_send_email_args

    action = SimpleNamespace(
        action_payload={"draft": {"recipient_email": "dana@acme.com", "subject": "Hi", "body": "line1\nline2"}},
        draft_result=None,
    )
    args = build_send_email_args(action)
    assert args["recipients"] == {"to": "dana@acme.com"}
    assert args["body"] == "line1<br>line2"


def test_build_send_email_args_none_without_recipient():
    from followup.api.execution import build_send_email_args

    action = SimpleNamespace(action_payload={"draft": {"subject": "Hi", "body": "x"}}, draft_result=None)
    assert build_send_email_args(action) is None


@pytest.mark.asyncio
async def test_send_drafted_email_skips_when_already_sent():
    from followup.api.execution import send_drafted_email

    action = SimpleNamespace(
        id="a1", execution_status="completed",
        action_payload={"draft": {"recipient_email": "dana@acme.com", "subject": "Hi", "body": "x"}},
        draft_result=None,
    )
    result = await send_drafted_email(action)
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_send_drafted_email_skips_when_no_draft():
    from followup.api.execution import send_drafted_email

    action = SimpleNamespace(id="a2", execution_status=None, action_payload={}, draft_result=None)
    result = await send_drafted_email(action)
    assert result["status"] == "skipped"
