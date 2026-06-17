import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from followup.agents.risk.evaluation import evaluate_all_rules
from followup.agents.risk.rules import compute_risk_score
from followup.context.completeness import (
    ContextCompleteness,
    section_loaded,
    section_partial,
    section_unavailable,
)
from followup.events.schemas import FollowUpEvent, FollowUpEventType
from followup.integration.risk_for_pipeline import run_risk_agent_for_pipeline
from followup.notifications.in_memory_repository import InMemoryNotificationRepository


PLATFORM_MIGRATION_NOW = datetime(2026, 6, 16, 18, 50, 19, tzinfo=timezone.utc)
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    from followup.context.schemas import DealContext

    raw = json.loads((FIXTURES_DIR / name).read_text())
    return DealContext.model_validate(raw)


def build_context(**overrides):
    from followup.tests.test_risk_agent import build_context as _build_context

    return _build_context(**overrides)


def test_rule_evaluations_distinguish_triggered_not_triggered_and_skipped():
    context = load_fixture("risk_context_platform_migration.json")
    evaluations = evaluate_all_rules(context, now=PLATFORM_MIGRATION_NOW)
    by_id = {evaluation.rule_id: evaluation for evaluation in evaluations}

    assert by_id["no_activity_7d"].status == "triggered"
    assert by_id["stalled_stage"].status == "skipped"
    assert "stage_entered_at is unavailable" in by_id["stalled_stage"].reason
    assert by_id["missing_proposal"].status == "not_triggered"
    assert "proposal evidence was found" in by_id["missing_proposal"].reason.lower()
    assert by_id["engagement_drop"].status == "not_triggered"
    assert by_id["no_future_meeting"].status == "skipped"
    assert "meeting data could not be verified" in by_id["no_future_meeting"].reason


def test_platform_migration_score_without_unverified_meeting_rule():
    context = load_fixture("risk_context_platform_migration.json")
    breakdown = compute_risk_score(context, now=PLATFORM_MIGRATION_NOW)
    triggered = {factor.rule_id for factor in breakdown.factors}

    assert triggered == {
        "no_activity_7d",
        "missing_decision_maker",
        "past_expected_close_date",
    }
    assert breakdown.total == 60
    assert breakdown.level == "medium"


@pytest.mark.asyncio
async def test_notification_persistence_end_to_end():
    context = load_fixture("risk_context_platform_migration.json")
    repository = InMemoryNotificationRepository()
    event = FollowUpEvent(
        event_id="persist-1",
        idempotency_key="persist-1",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=context.opportunity.id,
        workspace_id="workspace-001",
        user_id="user-001",
        occurred_at=PLATFORM_MIGRATION_NOW,
    )

    async def list_existing(opportunity_id: str, user_id: str):
        return await repository.list_for_opportunity(
            opportunity_id=opportunity_id,
            user_id=user_id,
        )

    first = await run_risk_agent_for_pipeline(
        event,
        list_existing,
        context=context,
        notification_repository=repository,
        now=PLATFORM_MIGRATION_NOW,
    )
    assert len(first.notifications) == 2
    assert repository.count() == 2

    second = await run_risk_agent_for_pipeline(
        event,
        list_existing,
        context=context,
        notification_repository=repository,
        now=PLATFORM_MIGRATION_NOW,
    )
    assert second.notifications == []
    assert repository.count() == 2

    stored = await repository.list_for_opportunity(
        opportunity_id=context.opportunity.id,
        user_id="user-001",
    )
    dismissed_target = next(
        notification for notification in stored if notification.rule_id == "no_activity_7d"
    )
    await repository.update_status(
        notification_id=dismissed_target.id or "",
        status="dismissed",
        now=PLATFORM_MIGRATION_NOW,
    )

    suppressed = await run_risk_agent_for_pipeline(
        event,
        list_existing,
        context=context,
        notification_repository=repository,
        now=PLATFORM_MIGRATION_NOW + timedelta(days=2),
    )
    assert all(
        notification.rule_id != "no_activity_7d"
        for notification in suppressed.notifications
    )
    assert repository.count() == 3
    assert any(
        notification.rule_id == "missing_decision_maker"
        for notification in suppressed.notifications
    )

    recreated = await run_risk_agent_for_pipeline(
        event,
        list_existing,
        context=context,
        notification_repository=repository,
        now=PLATFORM_MIGRATION_NOW + timedelta(days=8),
    )
    assert any(
        notification.rule_id == "no_activity_7d"
        for notification in recreated.notifications
    )
    assert repository.count() == 4


def test_completeness_distinguishes_loaded_empty_from_unavailable():
    loaded_empty = build_context(
        tasks=[],
        context_completeness=ContextCompleteness(
            tasks=section_loaded(source="agent_bridge"),
            meetings=section_loaded(source="agent_bridge"),
        ),
    )
    unavailable = build_context(
        tasks=[],
        context_completeness=ContextCompleteness(
            tasks=section_unavailable(
                "Reader tool does not expose the required opportunity relationship filter.",
            ),
            meetings=section_unavailable(
                "No supported meeting query is currently configured.",
            ),
        ),
    )

    loaded_evaluations = {
        evaluation.rule_id: evaluation.status
        for evaluation in evaluate_all_rules(loaded_empty)
    }
    unavailable_evaluations = {
        evaluation.rule_id: evaluation.status
        for evaluation in evaluate_all_rules(unavailable)
    }

    assert loaded_evaluations["overdue_tasks"] == "not_triggered"
    assert unavailable_evaluations["overdue_tasks"] == "skipped"
    assert unavailable_evaluations["no_future_meeting"] == "skipped"


def test_partial_timeline_allows_no_activity_rule():
    context = build_context(
        engagement={
            "days_since_last_activity": None,
            "activity_count_14d": 0,
            "activity_count_prior_14d": 0,
            "has_future_meeting": False,
        },
        context_completeness=ContextCompleteness(
            timeline=section_partial(
                "Opportunity scalar emailText/notes loaded, but linked CRM activity records were unavailable.",
                source="opportunity_scalar_fields",
            ),
            meetings=section_unavailable(
                "No supported meeting query is currently configured.",
            ),
            tasks=section_unavailable(
                "Reader tool does not expose the required opportunity relationship filter.",
            ),
        ),
    )
    evaluations = evaluate_all_rules(context)
    assert evaluations[0].rule_id == "no_activity_7d"
    assert evaluations[0].status == "triggered"


@pytest.mark.asyncio
async def test_pipeline_uses_preloaded_context_without_refetch():
    context = load_fixture("risk_context_platform_migration.json")
    event = FollowUpEvent(
        event_id="preload-1",
        idempotency_key="preload-1",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=context.opportunity.id,
        workspace_id="workspace-001",
        user_id="user-001",
        occurred_at=PLATFORM_MIGRATION_NOW,
    )

    with patch(
        "followup.integration.risk_for_pipeline.load_deal_context",
        new=AsyncMock(side_effect=AssertionError("context should not be reloaded")),
    ):
        result = await run_risk_agent_for_pipeline(
            event,
            AsyncMock(return_value=[]),
            context=context,
            now=PLATFORM_MIGRATION_NOW,
        )

    assert result.risk_score.score == 60


@pytest.mark.asyncio
async def test_pipeline_auto_loads_context_when_missing():
    context = load_fixture("risk_context_platform_migration.json")
    event = FollowUpEvent(
        event_id="autoload-1",
        idempotency_key="autoload-1",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=context.opportunity.id,
        workspace_id="workspace-001",
        user_id="user-001",
        occurred_at=PLATFORM_MIGRATION_NOW,
    )
    loader = AsyncMock(return_value=context)

    with patch(
        "followup.integration.risk_for_pipeline.load_deal_context",
        loader,
    ):
        await run_risk_agent_for_pipeline(
            event,
            AsyncMock(return_value=[]),
            now=PLATFORM_MIGRATION_NOW,
        )

    loader.assert_awaited_once_with(
        context.opportunity.id,
        "workspace-001",
        "user-001",
        use_llm=True,
    )


@pytest.mark.asyncio
async def test_closed_opportunity_persists_no_notifications():
    context = build_context(
        opportunity={"stage": "CLOSED_WON"},
    )
    repository = InMemoryNotificationRepository()
    event = FollowUpEvent(
        event_id="closed-1",
        idempotency_key="closed-1",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=context.opportunity.id,
        workspace_id="workspace-001",
        user_id="user-001",
        occurred_at=PLATFORM_MIGRATION_NOW,
    )

    result = await run_risk_agent_for_pipeline(
        event,
        AsyncMock(return_value=[]),
        context=context,
        notification_repository=repository,
        now=PLATFORM_MIGRATION_NOW,
    )

    assert result.notifications == []
    assert repository.count() == 0
