from datetime import datetime, timezone

from followup.emailer.agents.risk.schemas import RiskScore
from followup.emailer.context.schemas import (
    CompanyContext,
    ContactContext,
    DealContext,
    MeetingSummary,
    NoteSummary,
    OpportunityContext,
)
from followup.emailer.events.schemas import (
    FollowUpEvent,
    GenericOpportunityPayload,
    MeetingCompletedPayload,
    OpportunityStageChangedPayload,
)
from followup.emailer.rag.collections import CollectionName
from followup.emailer.rag.service import RetrievedChunk

FIXTURE_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def build_deal_context(
    *,
    stage: str = "Discovery",
    industry: str = "SaaS",
    company_type: str | None = None,
) -> DealContext:
    return DealContext(
        opportunity=OpportunityContext(
            id="opp-001",
            stage=stage,
            amount=50000.0,
            close_date="2026-09-30",
        ),
        company=CompanyContext(
            id="company-001",
            name="Acme Corp",
            industry=industry,
            company_type=company_type,
        ),
        contact=ContactContext(
            id="contact-001",
            name="Jane Smith",
            email="jane@acme.com",
            title="VP Sales",
        ),
        recent_meetings=[
            MeetingSummary(
                id="meeting-001",
                title="Discovery Call",
                summary="Discussed pipeline automation needs and timeline.",
                completed_at=FIXTURE_NOW,
                attendees=["Jane Smith", "John Rep"],
            )
        ],
        recent_notes=[
            NoteSummary(
                id="note-001",
                title="Follow-up notes",
                body="Customer requested proposal by end of month.",
                created_at=FIXTURE_NOW,
            )
        ],
        workspace_id="ws-001",
        user_id="user-001",
    )


def build_meeting_completed_event() -> FollowUpEvent:
    return FollowUpEvent(
        event_id="evt-meeting-001",
        idempotency_key="ws-001:meeting_completed:opp-001:meeting-001",
        event_type="meeting_completed",
        opportunity_id="opp-001",
        workspace_id="ws-001",
        user_id="user-001",
        occurred_at=FIXTURE_NOW,
        payload=MeetingCompletedPayload(
            meeting_id="meeting-001",
            summary="Discussed pipeline automation needs and timeline.",
            attendees=["Jane Smith", "John Rep"],
            completed_at=FIXTURE_NOW,
        ),
    )


def build_stage_changed_event(new_stage: str) -> FollowUpEvent:
    return FollowUpEvent(
        event_id="evt-stage-001",
        idempotency_key="ws-001:opportunity_stage_changed:opp-001:stage-001",
        event_type="opportunity_stage_changed",
        opportunity_id="opp-001",
        workspace_id="ws-001",
        user_id="user-001",
        occurred_at=FIXTURE_NOW,
        payload=OpportunityStageChangedPayload(
            previous_stage="Discovery",
            new_stage=new_stage,
            changed_fields=["stage"],
        ),
    )


def build_low_priority_event() -> FollowUpEvent:
    return FollowUpEvent(
        event_id="evt-update-001",
        idempotency_key="ws-001:opportunity_updated:opp-001:update-001",
        event_type="opportunity_updated",
        opportunity_id="opp-001",
        workspace_id="ws-001",
        user_id="user-001",
        occurred_at=FIXTURE_NOW,
        payload=GenericOpportunityPayload(changed_fields=["amount"]),
    )


def build_high_risk_score() -> RiskScore:
    return RiskScore(level="HIGH", score=85, reasoning="No activity in 14 days.")


def _long_body(prefix: str) -> str:
    filler = (
        "We reviewed your goals for pipeline automation and agreed on next steps. "
        "Our team will prepare materials aligned with your Discovery stage priorities. "
        "Please let us know if any details need adjustment before we proceed. "
    )
    return prefix + " " + (filler * 8)


COMPLETE_EMAIL_BODY = _long_body(
    "Hi Jane Smith, thank you for meeting with us at Acme Corp. "
    "Following our Discovery Call, I wanted to recap key points from the Discovery stage discussion."
)


COMPLETE_PROPOSAL_SECTIONS = [
    {
        "heading": "Executive Summary",
        "content": _long_body(
            "Acme Corp is evaluating a SaaS solution during the Proposal stage. "
            "This outline summarizes recommended capabilities for Jane Smith and team."
        ),
    },
    {
        "heading": "Proposed Solution",
        "content": _long_body(
            "The platform supports pipeline management and AI-assisted follow-up for Acme Corp."
        ),
    },
]


class MockRetrievalService:
    async def retrieve_documents(
        self,
        query: str,
        collection: CollectionName,
        top_k: int = 3,
    ) -> list[RetrievedChunk]:
        if collection == CollectionName.EMAIL_TEMPLATES:
            return [
                RetrievedChunk(
                    document_id="follow_up",
                    content="# Email template\nProfessional follow-up guidance.",
                    score=1.0,
                    metadata={"file_name": "follow_up.md"},
                )
            ]
        if collection == CollectionName.PROPOSAL_TEMPLATES:
            return [
                RetrievedChunk(
                    document_id="product_proposal",
                    content="# Proposal template\nStructured product proposal guidance.",
                    score=1.0,
                    metadata={"file_name": "product_proposal.md"},
                )
            ]
        return [
            RetrievedChunk(
                document_id="catalog",
                content="# Catalog\nProduct capabilities and outcomes.",
                score=1.0,
                metadata={"file_name": "saas_platform.md"},
            )
        ]
