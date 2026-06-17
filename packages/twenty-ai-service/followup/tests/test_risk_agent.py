import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from followup.agents.risk.agent import run_risk_notification_agent
from followup.agents.risk.notifications import (
    apply_notification_lifecycle,
    generate_notification_copy,
    should_notify,
)
from followup.agents.risk.rules import (
    compute_risk_score,
    detect_risk_signals,
    evaluate_engagement_drop,
    evaluate_no_activity_7d,
    evaluate_past_expected_close_date,
    evaluate_stalled_stage,
    has_proposal_evidence,
    risk_level_for_score,
    score_to_level,
)
from followup.agents.risk.objections import detect_customer_objections
from followup.agents.risk.schemas import Notification, NotificationDraft
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent, FollowUpEventType
from followup.store.risk_snapshot_store import InMemoryRiskSnapshotStore
from followup.workflows.risk_sweep.compare import (
    compare_score_to_previous,
    needs_re_engagement_draft,
)
from followup.workflows.risk_sweep.sweep import run_daily_risk_sweep

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> DealContext:
    raw = json.loads((FIXTURES_DIR / name).read_text())
    return DealContext.model_validate(raw)


def build_event(
    opportunity_id: str = "opp-001",
    user_id: str = "user-001",
) -> FollowUpEvent:
    return FollowUpEvent(
        event_id="event-001",
        idempotency_key="workspace:event:opp-001:001",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=opportunity_id,
        workspace_id="workspace-001",
        user_id=user_id,
        occurred_at=datetime.now(timezone.utc),
    )


def build_context(**overrides) -> DealContext:
    base = load_fixture("risk_context_healthy.json")
    data = base.model_dump()
    for key, value in overrides.items():
        if isinstance(value, dict) and key in data and isinstance(data[key], dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return DealContext.model_validate(data)


# ---------------------------------------------------------------------------
# Scenario 1 — Healthy deal → score < 40, 0 notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_deal_low_score_no_notifications() -> None:
    context = load_fixture("risk_context_healthy.json")
    event = build_event(opportunity_id=context.opportunity.id)

    result = await run_risk_notification_agent(context, event, [])

    assert result.risk_score.score < 40
    assert result.risk_score.level == "low"
    assert result.notifications == []


# ---------------------------------------------------------------------------
# Scenario 2 — No activity 10 days → +25, notification (HIGH severity factor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_activity_triggers_notification() -> None:
    context = build_context(
        engagement={
            "days_since_last_activity": 10,
            "activity_count_14d": 0,
            "activity_count_prior_14d": 0,
            "has_future_meeting": True,
        },
    )
    event = build_event()

    result = await run_risk_notification_agent(context, event, [])

    assert any(
        factor.rule_id == "no_activity_7d" for factor in result.risk_score.factors
    )
    assert result.risk_score.factors[0].points == 25
    assert len(result.notifications) >= 1
    assert result.notifications[0].rule_id == "no_activity_7d"
    assert result.notifications[0].reasoning_summary


# ---------------------------------------------------------------------------
# Scenario 3 — Stalled stage → +20
# ---------------------------------------------------------------------------


def test_stalled_stage_adds_twenty_points() -> None:
    context = build_context(
        opportunity={
            "stage": "PROPOSAL",
            "stage_entered_at": (
                datetime.now(timezone.utc) - timedelta(days=20)
            ).isoformat(),
        },
        pipeline_meta={
            "stages": ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"],
            "stage_sla_days": {"PROPOSAL": 14},
        },
    )
    breakdown = compute_risk_score(context)

    assert any(factor.rule_id == "stalled_stage" for factor in breakdown.factors)
    assert breakdown.total >= 20


# ---------------------------------------------------------------------------
# Scenario 4 — Missing decision maker past Discovery → +15
# ---------------------------------------------------------------------------


def test_missing_decision_maker_past_discovery() -> None:
    context = build_context(
        opportunity={"stage": "PROPOSAL"},
        pipeline_meta={
            "stages": ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"],
            "stage_sla_days": {"PROPOSAL": 14},
        },
        contacts=[
            {
                "id": "contact-002",
                "name": "Bob Smith",
                "role": "Manager",
                "is_decision_maker": False,
            },
        ],
    )
    factors = detect_risk_signals(context)

    decision_maker_factor = next(
        factor for factor in factors if factor.rule_id == "missing_decision_maker"
    )
    assert decision_maker_factor.points == 15


# ---------------------------------------------------------------------------
# Scenario 5 — Overdue tasks → +15
# ---------------------------------------------------------------------------


def test_overdue_tasks_adds_fifteen_points() -> None:
    context = build_context(
        tasks=[
            {
                "id": "task-001",
                "title": "Follow up",
                "status": "TODO",
                "is_overdue": True,
            },
        ],
    )
    breakdown = compute_risk_score(context)

    assert any(factor.rule_id == "overdue_tasks" for factor in breakdown.factors)
    assert breakdown.total >= 15


# ---------------------------------------------------------------------------
# Scenario 6 — Multiple rules stack capped at 100 → HIGH
# ---------------------------------------------------------------------------


def test_multiple_rules_capped_at_one_hundred_high_level() -> None:
    context = build_context(
        opportunity={
            "stage": "PROPOSAL",
            "stage_entered_at": (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat(),
        },
        pipeline_meta={
            "stages": ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"],
            "stage_sla_days": {"PROPOSAL": 14},
        },
        contacts=[
            {
                "id": "contact-002",
                "name": "Bob Smith",
                "is_decision_maker": False,
            },
        ],
        tasks=[
            {
                "id": "task-001",
                "title": "Send proposal",
                "status": "TODO",
                "is_overdue": True,
            },
        ],
        engagement={
            "days_since_last_activity": 14,
            "activity_count_14d": 1,
            "activity_count_prior_14d": 10,
            "has_future_meeting": False,
        },
        timeline=[],
    )
    breakdown = compute_risk_score(context)

    assert breakdown.total == 100
    assert breakdown.level == "high"
    assert score_to_level(breakdown.total) == "high"


# ---------------------------------------------------------------------------
# Scenario 7 — Dismissed notification not re-sent within 7 days
# ---------------------------------------------------------------------------


def test_dismissed_notification_suppressed_within_seven_days() -> None:
    context = load_fixture("risk_context_stale.json")
    event = build_event(opportunity_id=context.opportunity.id)
    breakdown = compute_risk_score(context)
    existing = [
        Notification(
            opportunity_id=context.opportunity.id,
            user_id="user-002",
            title="No recent activity",
            body="Previous alert",
            severity="high",
            status="dismissed",
            rule_id="no_activity_7d",
            reasoning_summary="Previously dismissed",
            dismissed_at=datetime.now(timezone.utc) - timedelta(days=2),
        ),
    ]

    drafts = should_notify(breakdown, event, existing, context=context)

    assert all(draft.rule_id != "no_activity_7d" for draft in drafts)


# ---------------------------------------------------------------------------
# Scenario 8 — Max 2 notifications when 5 rules fire
# ---------------------------------------------------------------------------


def test_max_two_notifications_when_many_rules_fire() -> None:
    context = load_fixture("risk_context_stale.json")
    event = build_event(opportunity_id=context.opportunity.id)
    breakdown = compute_risk_score(context)

    drafts = should_notify(breakdown, event, [], context=context)

    assert len(breakdown.factors) >= 3
    assert len(drafts) <= 2


# ---------------------------------------------------------------------------
# Scenario 9 — Closed Won → score computed, no notifications
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closed_won_computes_score_but_no_notifications() -> None:
    context = build_context(
        opportunity={"stage": "Closed Won"},
        engagement={
            "days_since_last_activity": 30,
            "has_future_meeting": False,
        },
    )
    event = build_event()

    result = await run_risk_notification_agent(context, event, [])

    assert result.risk_score.score > 0
    assert result.notifications == []


# ---------------------------------------------------------------------------
# Scenario 10 — LLM failure → fallback body from template
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_uses_template_fallback() -> None:
    context = load_fixture("risk_context_stale.json")
    draft = NotificationDraft(
        rule_id="no_activity_7d",
        title="No recent activity",
        severity="high",
        template_key="no_activity",
        opportunity_id=context.opportunity.id,
        user_id="user-002",
    )

    async def failing_llm(
        notification_draft: NotificationDraft,
        deal_context: DealContext,
    ) -> tuple[str, str]:
        raise RuntimeError("LLM unavailable")

    notifications = await generate_notification_copy(
        [draft],
        context,
        llm_generator=failing_llm,
    )

    assert len(notifications) == 1
    assert context.opportunity.name in notifications[0].body
    assert notifications[0].reasoning_summary


# ---------------------------------------------------------------------------
# Notification lifecycle dedupe
# ---------------------------------------------------------------------------


def test_apply_notification_lifecycle_dedupes_by_rule() -> None:
    new_notification = Notification(
        opportunity_id="opp-001",
        user_id="user-001",
        title="Overdue tasks",
        body="Body",
        severity="medium",
        rule_id="overdue_tasks",
        reasoning_summary="Tasks overdue",
    )
    existing = [new_notification.model_copy()]

    result = apply_notification_lifecycle([new_notification], existing)

    assert result == []


# ---------------------------------------------------------------------------
# Daily sweep + compare_score_to_previous
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_score_to_previous_detects_threshold_crossing() -> None:
    store = InMemoryRiskSnapshotStore()
    await compare_score_to_previous(
        opportunity_id="opp-001",
        workspace_id="workspace-001",
        new_score=35,
        factors=[],
        snapshot_store=store,
        source="daily_sweep",
    )
    snapshot = await compare_score_to_previous(
        opportunity_id="opp-001",
        workspace_id="workspace-001",
        new_score=55,
        factors=[],
        snapshot_store=store,
        source="daily_sweep",
    )

    assert snapshot.previous_score == 35
    assert snapshot.delta == 20
    assert snapshot.threshold_crossed is True
    assert needs_re_engagement_draft(snapshot) is True


@pytest.mark.asyncio
async def test_daily_risk_sweep_processes_active_opportunities() -> None:
    healthy = load_fixture("risk_context_healthy.json")
    stale = load_fixture("risk_context_stale.json")
    store = InMemoryRiskSnapshotStore()

    async def list_active_opportunities(
        workspace_id: str,
    ) -> list[dict[str, str]]:
        return [
            {"id": healthy.opportunity.id, "owner_id": "user-001"},
            {"id": stale.opportunity.id, "owner_id": "user-002"},
        ]

    contexts = {
        healthy.opportunity.id: healthy,
        stale.opportunity.id: stale,
    }

    async def load_deal_context(
        opportunity_id: str,
        workspace_id: str,
        user_id: str,
    ) -> DealContext:
        return contexts[opportunity_id]

    result = await run_daily_risk_sweep(
        "workspace-001",
        list_active_opportunities=list_active_opportunities,
        load_deal_context_fn=load_deal_context,
        snapshot_store=store,
    )

    assert result.opportunities_processed == 2
    assert len(result.results) == 2
    stale_result = next(
        item
        for item in result.results
        if item.opportunity_id == stale.opportunity.id
    )
    assert stale_result.risk_score.score >= 40
    assert stale_result.snapshot.source == "daily_sweep"


def test_stale_fixture_matches_expected_risk_profile() -> None:
    context = load_fixture("risk_context_stale.json")
    breakdown = compute_risk_score(context)

    assert breakdown.total >= 40
    assert breakdown.level in {"medium", "high"}


PLATFORM_MIGRATION_NOW = datetime(2026, 6, 16, 18, 50, 19, tzinfo=timezone.utc)


def test_platform_migration_fixture_scores_high() -> None:
    context = load_fixture("risk_context_platform_migration.json")
    breakdown = compute_risk_score(context, now=PLATFORM_MIGRATION_NOW)
    triggered = {factor.rule_id for factor in breakdown.factors}

    assert triggered == {
        "no_activity_7d",
        "missing_decision_maker",
        "past_expected_close_date",
    }
    assert "stalled_stage" not in triggered
    assert "missing_proposal" not in triggered
    assert "no_future_meeting" not in triggered
    assert breakdown.total == 60
    assert breakdown.level == "medium"


@pytest.mark.asyncio
async def test_platform_migration_notifications_are_capped_and_ranked() -> None:
    context = load_fixture("risk_context_platform_migration.json")
    event = build_event(opportunity_id=context.opportunity.id)

    result = await run_risk_notification_agent(
        context,
        event,
        [],
        now=PLATFORM_MIGRATION_NOW,
    )

    assert len(result.notifications) <= 2
    assert {notification.rule_id for notification in result.notifications} == {
        "past_expected_close_date",
        "no_activity_7d",
    }


def test_no_activity_none_triggers_rule() -> None:
    context = build_context(
        engagement={
            "days_since_last_activity": None,
            "activity_count_14d": 0,
            "activity_count_prior_14d": 0,
            "has_future_meeting": True,
        },
    )
    assert evaluate_no_activity_7d(context) is not None


def test_no_activity_zero_does_not_trigger() -> None:
    context = build_context(
        engagement={
            "days_since_last_activity": 0,
            "activity_count_14d": 1,
            "activity_count_prior_14d": 0,
            "has_future_meeting": True,
        },
    )
    assert evaluate_no_activity_7d(context) is None


def test_stalled_stage_skips_without_stage_entered_at() -> None:
    context = build_context(
        opportunity={"stage": "PROPOSAL", "stage_entered_at": None},
        pipeline_meta={
            "stages": ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"],
            "stage_sla_days": {"PROPOSAL": 14},
        },
    )
    assert evaluate_stalled_stage(context) is None


def test_past_close_date_triggers_for_open_deal() -> None:
    context = build_context(
        opportunity={
            "stage": "PROPOSAL",
            "close_date": "2026-01-31T16:25:00Z",
        },
    )
    factor = evaluate_past_expected_close_date(context, now=PLATFORM_MIGRATION_NOW)
    assert factor is not None
    assert factor.metadata["days_overdue"] >= 130


def test_objection_detection_finds_privacy_concern() -> None:
    context = load_fixture("risk_context_platform_migration.json")
    objections = detect_customer_objections(context)
    assert any(objection.category == "privacy" for objection in objections)


@pytest.mark.asyncio
async def test_improving_score_does_not_trigger_reengagement() -> None:
    store = InMemoryRiskSnapshotStore()
    await compare_score_to_previous(
        opportunity_id="opp-001",
        workspace_id="workspace-001",
        new_score=70,
        factors=[],
        snapshot_store=store,
        source="daily_sweep",
    )
    snapshot = await compare_score_to_previous(
        opportunity_id="opp-001",
        workspace_id="workspace-001",
        new_score=50,
        factors=[],
        snapshot_store=store,
        source="daily_sweep",
    )

    assert snapshot.delta == -20
    assert needs_re_engagement_draft(snapshot) is False
