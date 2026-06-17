from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FollowUpEventType(str, Enum):
    OPPORTUNITY_CREATED = "opportunity_created"
    OPPORTUNITY_UPDATED = "opportunity_updated"
    OPPORTUNITY_STAGE_CHANGED = "opportunity_stage_changed"
    MEETING_COMPLETED = "meeting_completed"
    EMAIL_SENT = "email_sent"
    PROPOSAL_SENT = "proposal_sent"
    TASK_COMPLETED = "task_completed"
    ACTIVITY_LOGGED = "activity_logged"
    DAILY_RISK_SWEEP = "daily_risk_sweep"


class FollowUpEvent(BaseModel):
    event_id: str
    idempotency_key: str
    event_type: FollowUpEventType
    opportunity_id: str
    workspace_id: str
    user_id: str
    source: Literal[
        "crm_writer",
        "entity_listener",
        "manual",
        "replay",
        "daily_sweep",
    ] = "manual"
    source_event_id: str | None = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
