"""Follow-Up event contracts owned by Person 1 (Follow-Up Orchestrator).

NOTE: This module is a minimal placeholder covering only what the Next Step
Intelligence Agent (Person 2) needs: `FollowUpEvent` and `FollowUpEventType`.
Person 1 owns this file and may extend it with the full discriminated-union
payload types described in FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md §5.1.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FollowUpEventType(str, Enum):
    """Normalized event types that can trigger the Follow-Up pipeline."""

    OPPORTUNITY_CREATED = "opportunity_created"
    OPPORTUNITY_UPDATED = "opportunity_updated"
    OPPORTUNITY_STAGE_CHANGED = "opportunity_stage_changed"
    MEETING_COMPLETED = "meeting_completed"
    EMAIL_SENT = "email_sent"
    PROPOSAL_SENT = "proposal_sent"
    TASK_COMPLETED = "task_completed"
    ACTIVITY_LOGGED = "activity_logged"


class FollowUpEvent(BaseModel):
    """A normalized event delivered to the Follow-Up Orchestrator."""

    event_id: str
    idempotency_key: str
    event_type: FollowUpEventType
    opportunity_id: str
    workspace_id: str
    user_id: str
    source: str = "manual"
    source_event_id: str | None = None
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
