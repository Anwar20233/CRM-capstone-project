from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from followup.contracts.risk import RiskAssessment, RiskFactor
from followup.risk.daily_sweep import (
    DAILY_SWEEP_TRIGGER_TYPE,
    DailyRiskSweep,
    OpportunityCandidate,
    should_create_risk_alert,
)
from followup.store.repositories import PendingAction, RiskDailyScore


OPPORTUNITY_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class FakeRiskAgent:
    def __init__(self, assessment: RiskAssessment) -> None:
        self.assessment = assessment
        self.calls: list[dict[str, Any]] = []

    async def evaluate_deal_risk(
        self,
        opportunity_id: str,
        workspace_id: str | None = None,
        trigger_type: str | None = None,
    ) -> RiskAssessment:
        self.calls.append(
            {
                "opportunity_id": opportunity_id,
                "workspace_id": workspace_id,
                "trigger_type": trigger_type,
            }
        )
        return self.assessment


class FakeScoreRepository:
    def __init__(self, previous_score: RiskDailyScore | None = None) -> None:
        self.previous_score = previous_score
        self.created: list[dict[str, Any]] = []

    async def get_latest(self, opportunity_id: uuid.UUID) -> RiskDailyScore | None:
        return self.previous_score

    async def create(self, score_data: dict[str, Any]) -> RiskDailyScore:
        self.created.append(score_data)
        return _daily_score(
            risk_level=score_data["risk_level"],
            risk_score=score_data["risk_score"],
            pending_action_id=score_data.get("created_pending_action_id"),
        )


class FakePendingActionRepository:
    def __init__(self, pending: list[PendingAction] | None = None) -> None:
        self.pending = pending or []
        self.created: list[dict[str, Any]] = []

    async def list_pending(
        self, opportunity_id: uuid.UUID, status: str = "pending"
    ) -> list[PendingAction]:
        return self.pending

    async def create(self, action_data: dict[str, Any]) -> PendingAction:
        self.created.append(action_data)
        return PendingAction(
            id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
            **action_data,
        )


class FakeConnection:
    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, args))
        return [
            {
                "id": uuid.UUID(OPPORTUNITY_ID),
                "name": "Needs Attention",
                "stage": "PROPOSAL",
            },
            {
                "id": uuid.UUID("44444444-4444-4444-4444-444444444444"),
                "name": "Already Customer",
                "stage": "CUSTOMER",
            },
        ]

    async def fetchrow(self, query: str, *args: Any) -> None:
        return None


def test_should_create_risk_alert_for_first_medium_score() -> None:
    assert should_create_risk_alert(
        previous_score=None,
        assessment=_assessment(risk_level="medium", risk_score=0.55),
    )


def test_should_not_create_risk_alert_when_level_does_not_increase() -> None:
    assert not should_create_risk_alert(
        previous_score=_daily_score(risk_level="medium", risk_score=0.52),
        assessment=_assessment(risk_level="medium", risk_score=0.65),
    )


def test_should_create_risk_alert_when_medium_crosses_to_high() -> None:
    assert should_create_risk_alert(
        previous_score=_daily_score(risk_level="medium", risk_score=0.52),
        assessment=_assessment(risk_level="high", risk_score=0.83),
    )


def test_should_not_create_risk_alert_when_pending_alert_exists() -> None:
    assert not should_create_risk_alert(
        previous_score=_daily_score(risk_level="low", risk_score=0.2),
        assessment=_assessment(risk_level="high", risk_score=0.9),
        has_pending_risk_alert=True,
    )


@pytest.mark.asyncio
async def test_daily_sweep_scores_and_creates_risk_alert() -> None:
    assessment = _assessment(risk_level="high", risk_score=0.88)
    risk_agent = FakeRiskAgent(assessment)
    score_repository = FakeScoreRepository(
        previous_score=_daily_score(risk_level="low", risk_score=0.2)
    )
    pending_repository = FakePendingActionRepository()
    sweep = DailyRiskSweep(
        executor=object(),
        risk_agent=risk_agent,
        score_repository=score_repository,
        pending_action_repository=pending_repository,
    )

    summary = await sweep.run([_candidate()])

    assert summary.scanned == 1
    assert summary.scored == 1
    assert summary.alerts_created == 1
    assert risk_agent.calls == [
        {
            "opportunity_id": OPPORTUNITY_ID,
            "workspace_id": str(WORKSPACE_ID),
            "trigger_type": DAILY_SWEEP_TRIGGER_TYPE,
        }
    ]
    assert pending_repository.created[0]["trigger_type"] == "risk_alert"
    assert pending_repository.created[0]["action_type"] == "escalate"
    assert pending_repository.created[0]["risk_assessment"]["risk_level"] == "high"
    assert score_repository.created[0]["created_pending_action_id"] == uuid.UUID(
        "33333333-3333-3333-3333-333333333333"
    )


@pytest.mark.asyncio
async def test_daily_sweep_persists_score_without_duplicate_alert() -> None:
    pending = PendingAction(
        id=uuid.uuid4(),
        opportunity_id=uuid.UUID(OPPORTUNITY_ID),
        workspace_id=WORKSPACE_ID,
        trigger_type="risk_alert",
        action_type="escalate",
        action_payload={},
    )
    score_repository = FakeScoreRepository(
        previous_score=_daily_score(risk_level="low", risk_score=0.2)
    )
    pending_repository = FakePendingActionRepository(pending=[pending])
    sweep = DailyRiskSweep(
        executor=object(),
        risk_agent=FakeRiskAgent(_assessment(risk_level="high", risk_score=0.9)),
        score_repository=score_repository,
        pending_action_repository=pending_repository,
    )

    summary = await sweep.run([_candidate()])

    assert summary.alerts_created == 0
    assert summary.skipped == 1
    assert summary.results[0].skipped_reason == "pending_risk_alert_exists"
    assert pending_repository.created == []
    assert score_repository.created[0]["risk_level"] == "high"


@pytest.mark.asyncio
async def test_discover_active_opportunities_uses_schema_override(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setenv(
        "FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE",
        "workspace_c4en9trdpordobem3offy83aa",
    )
    monkeypatch.setenv("FOLLOWUP_RISK_WORKSPACE_ID_OVERRIDE", str(WORKSPACE_ID))
    sweep = DailyRiskSweep(executor=conn, risk_agent=FakeRiskAgent(_assessment()))

    candidates = await sweep.discover_active_opportunities()

    assert [candidate.opportunity_id for candidate in candidates] == [OPPORTUNITY_ID]
    assert candidates[0].workspace_id == WORKSPACE_ID
    assert candidates[0].workspace_schema == "workspace_c4en9trdpordobem3offy83aa"


def _candidate() -> OpportunityCandidate:
    return OpportunityCandidate(
        opportunity_id=OPPORTUNITY_ID,
        workspace_id=WORKSPACE_ID,
        workspace_schema="workspace_c4en9trdpordobem3offy83aa",
        name="Needs Attention",
        stage="PROPOSAL",
    )


def _assessment(
    *, risk_level: str = "medium", risk_score: float = 0.55
) -> RiskAssessment:
    return RiskAssessment(
        opportunity_id=OPPORTUNITY_ID,
        risk_score=risk_score,
        risk_level=risk_level,
        factors=[
            RiskFactor(
                factor_type="engagement_gap",
                description="Opportunity has not been updated for 47 days.",
                severity="high",
                evidence="updatedAt=old",
                source="opportunity",
                confidence=0.85,
            )
        ],
        previous_score=None,
        assessed_at=datetime.now(timezone.utc).isoformat(),
        reasoning_summary="Daily sweep risk assessment.",
        recommended_notification={
            "should_notify": risk_level in {"medium", "high"},
            "urgency": "high" if risk_level == "high" else "medium",
            "title": "Deal at risk",
            "message": "Needs Attention is at risk.",
            "recommended_action": "Follow up with the champion.",
        },
        metadata={
            "trigger_type": DAILY_SWEEP_TRIGGER_TYPE,
            "used_previous_risk_snapshot": False,
            "used_pending_action_risk_assessment": False,
        },
    )


def _daily_score(
    *,
    risk_level: str,
    risk_score: float,
    pending_action_id: uuid.UUID | None = None,
) -> RiskDailyScore:
    return RiskDailyScore(
        id=uuid.uuid4(),
        opportunity_id=uuid.UUID(OPPORTUNITY_ID),
        workspace_id=WORKSPACE_ID,
        risk_score=risk_score,
        risk_level=risk_level,
        top_factors=[],
        assessment={},
        trigger_type=DAILY_SWEEP_TRIGGER_TYPE,
        assessed_at=datetime.now(timezone.utc),
        created_pending_action_id=pending_action_id,
    )
