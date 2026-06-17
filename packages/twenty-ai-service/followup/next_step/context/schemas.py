"""Deal context contracts owned by Person 1 (Follow-Up Orchestrator).

NOTE: This module is a minimal placeholder so that Person 2 (Next Step
Agent), Person 3 (Risk Agent) and Person 4 (Drafting Agent) can develop and
test against a stable `DealContext` shape ahead of Person 1's full
implementation, per FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md Part A / §8.2.

Person 1 owns this file and may extend it (e.g. `ClientProfile`,
`context_provenance`, `raw_fallback`) without breaking the fields consumed
here. Other agents must only read fields documented in the spec.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class FactCategory(str, Enum):
    """Categories for persistent Client Profile facts (spec Part A)."""

    IDENTITY = "identity"
    COMMUNICATION_BEHAVIOR = "communication_behavior"
    RELATIONSHIP_CONTEXT = "relationship_context"
    DEAL_PROGRESSION = "deal_progression"
    RISK = "risk"
    DERIVED_INSIGHTS = "derived_insights"


class ProfileFact(BaseModel):
    """A single fact from the Client Profile memory layer."""

    fact_id: str
    category: FactCategory
    fact_key: str
    value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class OpportunitySnapshot(BaseModel):
    """Point-in-time snapshot of the opportunity being analyzed."""

    id: str
    name: str
    stage: str
    amount: float | None = None
    close_date: datetime | None = None
    company_id: str | None = None
    owner_id: str | None = None
    updated_at: datetime | None = None


class CompanySnapshot(BaseModel):
    """Point-in-time snapshot of the linked company."""

    id: str
    name: str
    industry: str | None = None


class ContactSnapshot(BaseModel):
    """A contact associated with the opportunity."""

    id: str
    name: str
    role: str | None = None
    is_decision_maker: bool = False


class TimelineItem(BaseModel):
    """A single entry in the merged activity timeline (notes/tasks/meetings)."""

    type: str
    title: str
    summary: str
    occurred_at: datetime


class TaskSnapshot(BaseModel):
    """A task linked to the opportunity."""

    id: str
    title: str
    status: str
    due_at: datetime | None = None
    is_overdue: bool = False


class MeetingSnapshot(BaseModel):
    """A calendar meeting linked to the opportunity."""

    id: str
    title: str
    starts_at: datetime
    status: str


class PipelineMeta(BaseModel):
    """Pipeline stage metadata used for SLA and stage-order comparisons."""

    stages: list[str] = Field(default_factory=list)
    stage_sla_days: dict[str, int] = Field(default_factory=dict)


class EngagementMetrics(BaseModel):
    """Derived engagement signals used by Next Step and Risk agents."""

    days_since_last_activity: int
    activity_count_14d: int
    activity_count_prior_14d: int
    has_future_meeting: bool


class DealContext(BaseModel):
    """Profile-first context object passed to all Follow-Up agents.

    Only the fields consumed by the Next Step Intelligence Agent (Person 2)
    are documented in detail here; see FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md
    §8.2 for the full v2 shape (profile, context_provenance, raw_fallback, ...).
    """

    opportunity: OpportunitySnapshot
    company: CompanySnapshot | None = None
    contacts: list[ContactSnapshot] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    tasks: list[TaskSnapshot] = Field(default_factory=list)
    meetings: list[MeetingSnapshot] = Field(default_factory=list)
    pipeline_meta: PipelineMeta = Field(default_factory=PipelineMeta)
    engagement: EngagementMetrics
    active_facts: list[ProfileFact] = Field(default_factory=list)
    loaded_at: datetime | None = None
