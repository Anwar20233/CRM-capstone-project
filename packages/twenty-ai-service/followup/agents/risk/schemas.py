from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, computed_field


RiskLevel = Literal["low", "medium", "high"]
NotificationSeverity = Literal["low", "medium", "high"]
NotificationStatus = Literal["unread", "read", "dismissed", "acted_on"]
RiskSnapshotSource = Literal["event", "daily_sweep"]


class RiskFactor(BaseModel):
    rule_id: str
    points: int
    severity: NotificationSeverity
    title: str
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def detail(self) -> str:
        return self.reason


class RiskScoreBreakdown(BaseModel):
    total: int
    level: RiskLevel
    factors: list[RiskFactor] = Field(default_factory=list)


class RiskScore(BaseModel):
    score: int
    level: RiskLevel
    factors: list[RiskFactor] = Field(default_factory=list)
    computed_at: datetime
    reasoning_summary: str = ""


class NotificationDraft(BaseModel):
    rule_id: str
    title: str
    severity: NotificationSeverity
    template_key: str
    opportunity_id: str
    user_id: str


class Notification(BaseModel):
    id: str | None = None
    opportunity_id: str
    user_id: str
    title: str
    body: str
    severity: NotificationSeverity
    status: NotificationStatus = "unread"
    rule_id: str
    reasoning_summary: str
    dismissed_at: datetime | None = None


class RiskNotificationAgentResult(BaseModel):
    risk_score: RiskScore
    notifications: list[Notification] = Field(default_factory=list)
    reasoning_summary: str


class RiskScoreSnapshot(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    opportunity_id: str
    workspace_id: str
    score: int
    level: RiskLevel
    previous_score: int | None = None
    delta: int | None = None
    level_crossed_up: bool = False
    threshold_crossed: bool = False
    source: RiskSnapshotSource = "event"
    factors: list[RiskFactor] = Field(default_factory=list)
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class RiskScoreComparison(BaseModel):
    previous_score: int | None = None
    current_score: int
    delta: int | None = None
    previous_level: RiskLevel | None = None
    current_level: RiskLevel
    crossed_threshold: bool = False
    significant_delta: bool = False
    should_trigger_reengagement: bool = False
    reason: str = ""


class ProfileFactUpdateSuggestion(BaseModel):
    fact_key: str
    value: str
    category: str = "risk"


class RiskSweepOpportunityResult(BaseModel):
    opportunity_id: str
    risk_score: RiskScore
    snapshot: RiskScoreSnapshot
    notifications: list[Notification] = Field(default_factory=list)
    needs_re_engagement_draft: bool = False
    skipped: bool = False
    skip_reason: str | None = None


class RiskSweepError(BaseModel):
    opportunity_id: str
    message: str


class RiskSweepResult(BaseModel):
    workspace_id: str
    started_at: datetime
    completed_at: datetime | None = None
    evaluated_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    notifications_created: int = 0
    snapshot_count: int = 0
    re_engagement_triggers: int = 0
    opportunities_processed: int = 0
    opportunities_skipped: int = 0
    results: list[RiskSweepOpportunityResult] = Field(default_factory=list)
    errors: list[RiskSweepError] = Field(default_factory=list)
