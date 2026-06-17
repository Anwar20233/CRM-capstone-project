from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from followup.context.completeness import ContextCompleteness


class OpportunitySnapshot(BaseModel):
    id: str
    name: str
    stage: str
    amount: float | None = None
    close_date: datetime | None = None
    company_id: str | None = None
    owner_id: str | None = None
    updated_at: datetime | None = None
    stage_entered_at: datetime | None = None


class CompanySnapshot(BaseModel):
    id: str
    name: str
    industry: str | None = None


class ContactSnapshot(BaseModel):
    id: str
    name: str
    role: str | None = None
    is_decision_maker: bool = False


class TimelineItem(BaseModel):
    type: str
    title: str
    summary: str | None = None
    occurred_at: datetime | None = None
    source: str | None = None
    timestamp_source: str | None = None


class TaskSnapshot(BaseModel):
    id: str
    title: str
    status: str
    due_at: datetime | None = None
    is_overdue: bool = False


class MeetingSnapshot(BaseModel):
    id: str
    title: str
    starts_at: datetime | None = None
    status: str | None = None


class PipelineMeta(BaseModel):
    stages: list[str] = Field(default_factory=list)
    stage_sla_days: dict[str, int] = Field(default_factory=dict)
    source: Literal["crm_metadata", "fallback_defaults"] | None = None


class EngagementMetrics(BaseModel):
    days_since_last_activity: int | None = None
    activity_count_14d: int = 0
    activity_count_prior_14d: int = 0
    has_future_meeting: bool = False


class DealContext(BaseModel):
    opportunity: OpportunitySnapshot
    company: CompanySnapshot | None = None
    contacts: list[ContactSnapshot] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    tasks: list[TaskSnapshot] = Field(default_factory=list)
    meetings: list[MeetingSnapshot] = Field(default_factory=list)
    pipeline_meta: PipelineMeta = Field(default_factory=PipelineMeta) #
    engagement: EngagementMetrics = Field(default_factory=EngagementMetrics)
    context_provenance: Literal[ #
        "profile_primary",
        "crm_fallback",
        "hybrid",
    ] = "crm_fallback"
    context_completeness: ContextCompleteness | None = None
    loaded_at: datetime | None = None
