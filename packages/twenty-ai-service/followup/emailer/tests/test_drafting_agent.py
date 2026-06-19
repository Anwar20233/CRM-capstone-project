import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from followup.emailer.agents.drafting.agent import run_drafting_agent
from followup.emailer.agents.drafting.resolver import resolve_draft_types
from followup.emailer.agents.drafting.schemas import (
    DraftType,
    EmailDraft,
    ProposalDraft,
    ProposalSection,
)
from followup.emailer.api.accept_builder import (
    build_crm_instruction_for_draft,
    format_proposal_sections,
)
from followup.emailer.tests.fixtures.drafting_fixtures import (
    COMPLETE_EMAIL_BODY,
    COMPLETE_PROPOSAL_SECTIONS,
    MockRetrievalService,
    build_deal_context,
    build_high_risk_score,
    build_low_priority_event,
    build_meeting_completed_event,
    build_stage_changed_event,
)


@pytest.fixture
def retrieval() -> MockRetrievalService:
    return MockRetrievalService()


def _email_draft(draft_type: DraftType) -> EmailDraft:
    return EmailDraft(
        subject=f"Follow up with Acme Corp — {draft_type.value}",
        body=COMPLETE_EMAIL_BODY,
        draft_type=draft_type,
        template_used="follow_up.md",
    )


def _proposal_draft(draft_type: DraftType) -> ProposalDraft:
    return ProposalDraft(
        title="Product solution for Acme Corp — Proposal stage",
        sections=[
            ProposalSection(heading=section["heading"], content=section["content"])
            for section in COMPLETE_PROPOSAL_SECTIONS
        ],
        draft_type=draft_type,
        template_used="product_proposal.md",
    )


async def _mock_call_llm_json(prompt: str, schema: type, model=None):
    if schema is EmailDraft:
        if "meeting_recap_email" in prompt:
            return _email_draft(DraftType.MEETING_RECAP_EMAIL)
        if "re_engagement_email" in prompt:
            return _email_draft(DraftType.RE_ENGAGEMENT_EMAIL)
        return _email_draft(DraftType.FOLLOW_UP_EMAIL)
    return _proposal_draft(DraftType.PRODUCT_PROPOSAL)


def test_meeting_completed_generates_meeting_recap(retrieval: MockRetrievalService):
    context = build_deal_context()
    event = build_meeting_completed_event()

    with patch(
        "followup.emailer.agents.drafting.agent.call_llm_json",
        new=AsyncMock(side_effect=_mock_call_llm_json),
    ):
        result = asyncio.run(run_drafting_agent(context, event, None, retrieval))

    assert result.skipped is False
    assert len(result.email_drafts) == 1
    assert result.email_drafts[0].draft_type == DraftType.MEETING_RECAP_EMAIL
    assert result.email_drafts[0].reasoning


def test_stage_changed_proposal_generates_proposal_draft(
    retrieval: MockRetrievalService,
):
    context = build_deal_context(stage="Proposal")
    event = build_stage_changed_event("Proposal")

    with patch(
        "followup.emailer.agents.drafting.agent.call_llm_json",
        new=AsyncMock(side_effect=_mock_call_llm_json),
    ):
        result = asyncio.run(run_drafting_agent(context, event, None, retrieval))

    assert result.skipped is False
    assert len(result.proposal_drafts) == 1
    assert result.proposal_drafts[0].draft_type in {
        DraftType.PRODUCT_PROPOSAL,
        DraftType.SERVICE_PROPOSAL,
        DraftType.INDUSTRY_PROPOSAL,
    }


def test_high_risk_includes_re_engagement_email(retrieval: MockRetrievalService):
    context = build_deal_context()
    event = build_low_priority_event()
    risk_score = build_high_risk_score()

    with patch(
        "followup.emailer.agents.drafting.agent.call_llm_json",
        new=AsyncMock(side_effect=_mock_call_llm_json),
    ):
        result = asyncio.run(
            run_drafting_agent(
                context,
                event,
                None,
                retrieval,
                risk_score=risk_score,
            )
        )

    assert any(
        draft.draft_type == DraftType.RE_ENGAGEMENT_EMAIL
        for draft in result.email_drafts
    )


def test_quality_score_above_threshold_for_complete_fixture(
    retrieval: MockRetrievalService,
):
    context = build_deal_context()
    event = build_meeting_completed_event()

    with patch(
        "followup.emailer.agents.drafting.agent.call_llm_json",
        new=AsyncMock(side_effect=_mock_call_llm_json),
    ):
        result = asyncio.run(run_drafting_agent(context, event, None, retrieval))

    assert result.email_drafts[0].quality_score > 0.5


def test_reasoning_is_non_empty(retrieval: MockRetrievalService):
    context = build_deal_context()
    event = build_meeting_completed_event()

    with patch(
        "followup.emailer.agents.drafting.agent.call_llm_json",
        new=AsyncMock(side_effect=_mock_call_llm_json),
    ):
        result = asyncio.run(run_drafting_agent(context, event, None, retrieval))

    assert result.reasoning
    assert result.email_drafts[0].reasoning


def test_max_one_email_and_one_proposal_per_run(retrieval: MockRetrievalService):
    context = build_deal_context(stage="Proposal")
    event = build_stage_changed_event("Proposal")
    risk_score = build_high_risk_score()

    with patch(
        "followup.emailer.agents.drafting.agent.call_llm_json",
        new=AsyncMock(side_effect=_mock_call_llm_json),
    ):
        result = asyncio.run(
            run_drafting_agent(
                context,
                event,
                None,
                retrieval,
                risk_score=risk_score,
            )
        )

    assert len(result.email_drafts) <= 1
    assert len(result.proposal_drafts) <= 1


def test_low_priority_event_skips_when_no_risk(retrieval: MockRetrievalService):
    context = build_deal_context()
    event = build_low_priority_event()

    result = asyncio.run(run_drafting_agent(context, event, None, retrieval))

    assert result.skipped is True
    assert result.reasoning == "No drafts needed"
    assert result.email_drafts == []
    assert result.proposal_drafts == []


def test_resolve_draft_types_caps_at_two():
    context = build_deal_context(stage="Proposal")
    event = build_stage_changed_event("Proposal")
    risk_score = build_high_risk_score()

    types = resolve_draft_types(event, context, risk_score)

    assert len(types) <= 2
    assert DraftType.RE_ENGAGEMENT_EMAIL in types


def test_build_crm_instruction_for_email_draft():
    draft = _email_draft(DraftType.FOLLOW_UP_EMAIL)
    instruction = build_crm_instruction_for_draft(draft, "opp-001")

    assert "Create a note on opportunity opp-001" in instruction
    assert draft.subject in instruction
    assert draft.body in instruction


def test_build_crm_instruction_for_proposal_draft():
    draft = _proposal_draft(DraftType.PRODUCT_PROPOSAL)
    instruction = build_crm_instruction_for_draft(draft, "opp-001")

    assert "proposal draft" in instruction
    assert draft.title in instruction
    assert format_proposal_sections(draft) in instruction


def test_accept_builder_email_fields_align_with_send_args_shape():
    from types import SimpleNamespace

    from followup.api.execution import build_send_email_args

    draft = _email_draft(DraftType.FOLLOW_UP_EMAIL)
    action = SimpleNamespace(
        action_payload={
            "draft": {
                "recipient_email": "jane@acme.com",
                "subject": draft.subject,
                "body": draft.body,
            }
        },
        draft_result=None,
    )
    send_args = build_send_email_args(action)

    assert send_args is not None
    assert send_args["recipients"]["to"] == "jane@acme.com"
    assert send_args["subject"] == draft.subject
    assert draft.body.replace("\n", "<br>") in send_args["body"]
