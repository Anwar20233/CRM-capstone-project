from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


FollowUpEventType = Literal[
    "opportunity_created",
    "opportunity_updated",
    "opportunity_stage_changed",
    "meeting_completed",
    "email_sent",
    "proposal_sent",
    "task_completed",
    "activity_logged",
]

FollowUpEventSource = Literal["crm_writer", "entity_listener", "manual", "replay"]


class OpportunityStageChangedPayload(BaseModel):
    previous_stage: str
    new_stage: str
    changed_fields: list[str] = Field(default_factory=list)


class MeetingCompletedPayload(BaseModel):
    meeting_id: str
    summary: str | None = None
    attendees: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None


class TaskCompletedPayload(BaseModel):
    task_id: str
    title: str
    completed_at: datetime | None = None


class ActivityLoggedPayload(BaseModel):
    activity_type: str
    activity_id: str
    summary: str | None = None


class GenericOpportunityPayload(BaseModel):
    changed_fields: list[str] = Field(default_factory=list)
    snapshot: dict[str, Any] = Field(default_factory=dict)


FollowUpEventPayload = (
    OpportunityStageChangedPayload
    | MeetingCompletedPayload
    | TaskCompletedPayload
    | ActivityLoggedPayload
    | GenericOpportunityPayload
)


class FollowUpEvent(BaseModel):
    event_id: str
    idempotency_key: str
    event_type: FollowUpEventType
    opportunity_id: str
    workspace_id: str
    user_id: str
    source: FollowUpEventSource = "entity_listener"
    source_event_id: str | None = None
    occurred_at: datetime
    payload: FollowUpEventPayload = Field(default_factory=GenericOpportunityPayload)
    metadata: dict[str, Any] = Field(default_factory=dict)
