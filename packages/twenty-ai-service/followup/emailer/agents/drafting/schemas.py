from enum import Enum

from pydantic import BaseModel, Field


class DraftType(str, Enum):
    FOLLOW_UP_EMAIL = "follow_up_email"
    MEETING_RECAP_EMAIL = "meeting_recap_email"
    PROPOSAL_DELIVERY_EMAIL = "proposal_delivery_email"
    RE_ENGAGEMENT_EMAIL = "re_engagement_email"
    REMINDER_EMAIL = "reminder_email"
    PRODUCT_PROPOSAL = "product_proposal"
    SERVICE_PROPOSAL = "service_proposal"
    INDUSTRY_PROPOSAL = "industry_proposal"


EMAIL_DRAFT_TYPES: frozenset[DraftType] = frozenset(
    {
        DraftType.FOLLOW_UP_EMAIL,
        DraftType.MEETING_RECAP_EMAIL,
        DraftType.PROPOSAL_DELIVERY_EMAIL,
        DraftType.RE_ENGAGEMENT_EMAIL,
        DraftType.REMINDER_EMAIL,
    }
)

PROPOSAL_DRAFT_TYPES: frozenset[DraftType] = frozenset(
    {
        DraftType.PRODUCT_PROPOSAL,
        DraftType.SERVICE_PROPOSAL,
        DraftType.INDUSTRY_PROPOSAL,
    }
)


class ProposalSection(BaseModel):
    heading: str
    content: str


class EmailDraft(BaseModel):
    subject: str
    body: str
    draft_type: DraftType
    template_used: str = ""
    quality_score: float = 0.0
    reasoning: str = ""


class ProposalDraft(BaseModel):
    title: str
    sections: list[ProposalSection]
    draft_type: DraftType
    template_used: str = ""
    quality_score: float = 0.0
    reasoning: str = ""


class DraftingAgentResult(BaseModel):
    email_drafts: list[EmailDraft] = Field(default_factory=list)
    proposal_drafts: list[ProposalDraft] = Field(default_factory=list)
    reasoning: str = ""
    skipped: bool = False
