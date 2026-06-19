"""Unit tests for followup/contracts/ — agent contracts and mock implementations.

Tests that:
* The next-step mock returns a NextStepPlan of valid PlannedStep intents.
* The risk mock re-scores from the facts + narrative it is handed.
* The drafting mock builds a draft from intent + context and owns its tone.
* asdict(result) is json.dumps-able (PendingAction column-ready).
* isinstance(MockXAgent(), XAgent) holds for each runtime-checkable Protocol.
* AgentBundle wires defaults correctly.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from followup.contracts import (
    AgentBundle,
    DraftingAgent,
    DraftRequest,
    EmailSignalEvent,
    MockDraftingAgent,
    MockNextStepAgent,
    MockRiskAgent,
    NextStepAgent,
    NextStepPlan,
    NextStepRequest,
    PlannedStep,
    RiskAgent,
    RiskAssessment,
    RiskAssessmentRequest,
    DRAFT_MODES,
    DRAFT_TONE_TYPES,
    NEXT_STEP_MODES,
    NEXT_STEP_TYPES,
    PRIORITY_LEVELS,
    RISK_FACTOR_TYPES,
    RISK_LEVELS,
    RISK_MODES,
    SIGNAL_TYPES,
    SEVERITY_LEVELS,
    STEP_KINDS,
    mock_run_draft,
    mock_run_next_step,
    mock_run_risk_assessment,
    run_draft,
    run_next_step,
    run_risk_assessment,
)
from followup.profile.schemas import ContactSummary, DealContext


# ===========================================================================
# Fixtures — minimal and populated DealContext + extraction facts
# ===========================================================================


def _make_context(
    *,
    opportunity_id: str = "opp-001",
    contacts: list[ContactSummary] | None = None,
    activities: list[dict] | None = None,
    concerns: list[dict] | None = None,
    risk_score: float | None = 0.4,
) -> DealContext:
    return DealContext(
        opportunity_id=opportunity_id,
        opportunity_name="Acme Q3 Expansion",
        deal_stage="PROPOSAL",
        deal_value=50000.0,
        company_name="Acme Corp",
        profile_narrative="Briefing text.",
        contacts=contacts or [],
        recent_activities=activities or [],
        key_relationships=[],
        open_concerns=concerns or [],
        risk_score=risk_score,
    )


def _contact(
    crm_id: str = "crm-1",
    name: str = "Alice Smith",
    email: str = "alice@acme.com",
    role: str | None = "VP Sales",
) -> ContactSummary:
    return ContactSummary(crm_id=crm_id, name=name, role=role, email=email, facts=[])


def _rich_context() -> DealContext:
    return _make_context(
        contacts=[_contact(), _contact("crm-2", "Bob Jones", "bob@acme.com", "CTO")],
        activities=[{"type": "note", "date": "2026-06-10T10:00:00Z", "summary": "Demo call"}],
        concerns=[{"fact_type": "concern", "content": "Budget freeze mentioned"}],
        risk_score=0.65,
    )


def _empty_context() -> DealContext:
    """No contacts, no activities, no concerns — valid but minimal."""
    return _make_context(contacts=[], activities=[], concerns=[], risk_score=None)


def _signal() -> EmailSignalEvent:
    return EmailSignalEvent(
        sender_email="alice@acme.com",
        subject="Re: Q3 Expansion",
        body="Hi, just checking in.",
        received_at="2026-06-15T09:00:00Z",
        opportunity_id="opp-001",
    )


def _classification(type_: str = "objection", urgency: str = "high") -> dict:
    return {"type": type_, "urgency": urgency, "requires_calendar": False}


# ===========================================================================
# EmailSignalEvent
# ===========================================================================


def test_signal_type_in_frozenset() -> None:
    assert _signal().signal_type in SIGNAL_TYPES


def test_signal_event_asdict_json_safe() -> None:
    json.dumps(asdict(_signal()))


# ===========================================================================
# Protocol conformance
# ===========================================================================


def test_mock_next_step_agent_satisfies_protocol() -> None:
    assert isinstance(MockNextStepAgent(), NextStepAgent)


def test_mock_risk_agent_satisfies_protocol() -> None:
    assert isinstance(MockRiskAgent(), RiskAgent)


def test_mock_drafting_agent_satisfies_protocol() -> None:
    assert isinstance(MockDraftingAgent(), DraftingAgent)


# ===========================================================================
# NextStepAgent — returns a plan of intent steps
# ===========================================================================


@pytest.mark.asyncio
async def test_mock_next_step_returns_plan() -> None:
    req = NextStepRequest(deal_context=_rich_context(), trigger_type="email_signal", trigger=_signal())
    result = await MockNextStepAgent().run(req)
    assert isinstance(result, NextStepPlan)
    assert result.steps and all(isinstance(s, PlannedStep) for s in result.steps)


@pytest.mark.asyncio
async def test_mock_next_step_fields_valid() -> None:
    req = NextStepRequest(deal_context=_rich_context(), trigger_type="email_signal", mode="single")
    result = await MockNextStepAgent().run(req)
    assert result.headline_action in NEXT_STEP_TYPES
    assert req.mode in NEXT_STEP_MODES
    assert req.trigger_type in SIGNAL_TYPES
    for step in result.steps:
        assert step.kind in STEP_KINDS
        assert step.priority in PRIORITY_LEVELS
        assert isinstance(step.intent, str) and step.intent


@pytest.mark.asyncio
async def test_mock_next_step_high_risk_is_multi_step() -> None:
    # A high-risk deal escalates with a multi-step plan (email + note + task).
    ctx = _make_context(risk_score=0.85, concerns=[{"fact_type": "concern", "content": "stalled"}])
    result = await MockNextStepAgent().run(
        NextStepRequest(deal_context=ctx, trigger_type="email_signal")
    )
    assert result.headline_action == "escalate"
    kinds = {s.kind for s in result.steps}
    assert {"draft_email", "write_note", "create_task"} <= kinds


@pytest.mark.asyncio
async def test_mock_next_step_json_safe() -> None:
    req = NextStepRequest(deal_context=_rich_context(), trigger_type="email_signal")
    result = await MockNextStepAgent().run(req)
    json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_run_next_step_defaults_to_mock() -> None:
    req = NextStepRequest(deal_context=_rich_context(), trigger_type="scheduled")
    assert isinstance(await run_next_step(req), NextStepPlan)


@pytest.mark.asyncio
async def test_mock_run_next_step_helper() -> None:
    req = NextStepRequest(deal_context=_rich_context(), trigger_type="email_signal")
    assert isinstance(await mock_run_next_step(req), NextStepPlan)


# ===========================================================================
# RiskAgent — minimal identifiers only
# ===========================================================================


@pytest.mark.asyncio
async def test_mock_risk_returns_correct_type() -> None:
    req = RiskAssessmentRequest(opportunity_id="opp-001")
    result = await MockRiskAgent().run(req)
    assert isinstance(result, RiskAssessment)


@pytest.mark.asyncio
async def test_mock_risk_accepts_minimal_identifiers() -> None:
    req = RiskAssessmentRequest(
        opportunity_id="opp-001",
        workspace_id="workspace-001",
        trigger_type="risk_sweep",
    )
    result = await MockRiskAgent().run(req)
    assert req.mode in RISK_MODES
    assert result.opportunity_id == "opp-001"
    assert result.risk_level in RISK_LEVELS
    for factor in result.factors:
        assert factor.factor_type in RISK_FACTOR_TYPES
        assert factor.severity in SEVERITY_LEVELS


@pytest.mark.asyncio
async def test_mock_risk_does_not_require_previous_score() -> None:
    req = RiskAssessmentRequest(opportunity_id="opp-001")
    result = await MockRiskAgent().run(req)
    assert 0 <= result.risk_score <= 100
    assert result.previous_score is None


@pytest.mark.asyncio
async def test_mock_risk_returns_notification_payload() -> None:
    req = RiskAssessmentRequest(opportunity_id="opp-001")
    result = await MockRiskAgent().run(req)
    assert "should_notify" in result.recommended_notification
    assert "recommended_action" in result.recommended_notification


@pytest.mark.asyncio
async def test_mock_risk_json_safe() -> None:
    req = RiskAssessmentRequest(opportunity_id="opp-001")
    result = await MockRiskAgent().run(req)
    json.dumps(asdict(result))  # recurses into list[RiskFactor]


@pytest.mark.asyncio
async def test_run_risk_assessment_defaults_to_mock() -> None:
    req = RiskAssessmentRequest(opportunity_id="opp-001")
    assert isinstance(await run_risk_assessment(req, agent=MockRiskAgent()), RiskAssessment)


@pytest.mark.asyncio
async def test_mock_run_risk_assessment_helper() -> None:
    req = RiskAssessmentRequest(opportunity_id="opp-001")
    assert isinstance(await mock_run_risk_assessment(req), RiskAssessment)


# ===========================================================================
# DraftingAgent — content + context in, the drafter owns tone
# ===========================================================================


def _draft_req(ctx: DealContext, **kwargs) -> DraftRequest:
    base = {"deal_context": ctx, "intent": "Follow up on the proposal", "classification": _classification()}
    base.update(kwargs)
    return DraftRequest(**base)


@pytest.mark.asyncio
async def test_mock_draft_returns_correct_type_and_fields() -> None:
    ctx = _rich_context()
    result = await MockDraftingAgent().run(_draft_req(ctx))
    assert isinstance(result.subject, str) and result.subject
    assert isinstance(result.body, str) and result.body
    assert result.opportunity_id == ctx.opportunity_id
    assert result.tone in DRAFT_TONE_TYPES


@pytest.mark.asyncio
async def test_mock_draft_owns_tone_from_classification() -> None:
    ctx = _rich_context()
    # high-urgency objection → urgent; buying_signal → consultative.
    urgent = await MockDraftingAgent().run(_draft_req(ctx, classification=_classification("objection", "high")))
    assert urgent.tone == "urgent"
    consult = await MockDraftingAgent().run(
        _draft_req(ctx, classification=_classification("buying_signal", "low"))
    )
    assert consult.tone == "consultative"


@pytest.mark.asyncio
async def test_mock_draft_intent_appears_in_body() -> None:
    ctx = _rich_context()
    result = await MockDraftingAgent().run(_draft_req(ctx, intent="Reassure on the Q3 timeline"))
    assert "Reassure on the Q3 timeline" in result.body


@pytest.mark.asyncio
async def test_mock_draft_json_safe() -> None:
    result = await MockDraftingAgent().run(_draft_req(_rich_context()))
    json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_mock_draft_uses_first_contact_email() -> None:
    result = await MockDraftingAgent().run(_draft_req(_rich_context()))
    assert result.recipient_email == "alice@acme.com"


@pytest.mark.asyncio
async def test_mock_draft_empty_context_fallback_recipient() -> None:
    result = await MockDraftingAgent().run(_draft_req(_empty_context()))
    assert isinstance(result.recipient_email, str) and result.recipient_email


@pytest.mark.asyncio
async def test_mock_draft_explicit_recipient_wins() -> None:
    result = await MockDraftingAgent().run(
        _draft_req(_rich_context(), recipient_email="override@example.com")
    )
    assert result.recipient_email == "override@example.com"


@pytest.mark.asyncio
async def test_mock_draft_reply_context_addresses_sender() -> None:
    reply = {"sender_email": "john@airbnb.com", "sender_name": "John", "subject": "Re: Deal", "body": "Concerns"}
    result = await MockDraftingAgent().run(_draft_req(_rich_context(), reply_context=reply))
    assert result.recipient_email == "john@airbnb.com"
    assert result.subject.startswith("Re: Re: Deal")


@pytest.mark.asyncio
async def test_run_draft_defaults_to_mock() -> None:
    ctx = _rich_context()
    result = await run_draft(_draft_req(ctx))
    assert result.opportunity_id == ctx.opportunity_id


@pytest.mark.asyncio
async def test_mock_run_draft_helper() -> None:
    result = await mock_run_draft(_draft_req(_rich_context(), mode="single"))
    assert isinstance(result.drafted_at, str) and result.drafted_at
    assert "single" in DRAFT_MODES


# ===========================================================================
# AgentBundle
# ===========================================================================


def test_agent_bundle_defaults_are_mocks() -> None:
    bundle = AgentBundle()
    assert isinstance(bundle.next_step, NextStepAgent)
    assert isinstance(bundle.risk, RiskAgent)
    assert isinstance(bundle.drafting, DraftingAgent)


@pytest.mark.asyncio
async def test_agent_bundle_mocks_run_end_to_end() -> None:
    bundle = AgentBundle()
    ctx = _rich_context()

    plan = await bundle.next_step.run(
        NextStepRequest(deal_context=ctx, trigger_type="email_signal", classification=_classification())
    )
    assessment = await bundle.risk.run(
        RiskAssessmentRequest(opportunity_id=ctx.opportunity_id)
    )
    draft = await bundle.drafting.run(
        DraftRequest(deal_context=ctx, intent=plan.steps[0].intent, classification=_classification())
    )

    assert plan.headline_action in NEXT_STEP_TYPES
    assert 0 <= assessment.risk_score <= 100
    assert draft.subject and draft.body
