from datetime import datetime

from pydantic import BaseModel, Field


class OpportunityContext(BaseModel):
    id: str
    stage: str
    amount: float | None = None
    close_date: str | None = None
    company_id: str | None = None
    owner_id: str | None = None


class CompanyContext(BaseModel):
    id: str | None = None
    name: str
    industry: str | None = None
    company_type: str | None = None


class ContactContext(BaseModel):
    id: str | None = None
    name: str
    email: str | None = None
    title: str | None = None


class MeetingSummary(BaseModel):
    id: str
    title: str
    summary: str | None = None
    completed_at: datetime | None = None
    attendees: list[str] = Field(default_factory=list)


class NoteSummary(BaseModel):
    id: str
    title: str | None = None
    body: str | None = None
    created_at: datetime | None = None


class DealContext(BaseModel):
    opportunity: OpportunityContext
    company: CompanyContext
    contact: ContactContext
    recent_meetings: list[MeetingSummary] = Field(default_factory=list)
    recent_notes: list[NoteSummary] = Field(default_factory=list)
    workspace_id: str | None = None
    user_id: str | None = None
