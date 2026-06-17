#!/usr/bin/env python3
"""Interactive demo for the Follow-Up Risk Agent using dummy deal data.

Runs the real ``run_risk_notification_agent`` pipeline (rules + notifications)
without CRM or database dependencies. Notification copy uses template text by
default — pass ``--use-llm`` to call OpenRouter/OpenAI for prose.

Usage (from packages/twenty-ai-service)::

    .venv/bin/python scripts/demo_risk_agent.py
    .venv/bin/python scripts/demo_risk_agent.py --scenario stale
    .venv/bin/python scripts/demo_risk_agent.py --all
    .venv/bin/python scripts/demo_risk_agent.py --sweep
    .venv/bin/python scripts/demo_risk_agent.py --use-llm --scenario stale

Scenarios: healthy | stale | critical | closed_won | engagement_drop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from followup.agents.risk.agent import run_risk_notification_agent
from followup.agents.risk.rules import compute_risk_score
from followup.agents.risk.schemas import Notification
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent, FollowUpEventType
from followup.store.risk_snapshot_store import InMemoryRiskSnapshotStore
from followup.workflows.risk_sweep.compare import (
    compare_score_to_previous,
    needs_re_engagement_draft,
)
from followup.workflows.risk_sweep.sweep import run_daily_risk_sweep

FIXTURES_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "followup" / "tests" / "fixtures"
)

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _header(title: str) -> None:
    line = "─" * len(title)
    print(f"\n{_c('1;36', title)}")
    print(_c('2', line))


def _level_color(level: str) -> str:
    return {"low": "32", "medium": "33", "high": "31"}.get(level, "37")


def load_fixture(name: str) -> DealContext:
    raw = json.loads((FIXTURES_DIR / name).read_text())
    return DealContext.model_validate(raw)


def build_event(context: DealContext, event_type: FollowUpEventType) -> FollowUpEvent:
    return FollowUpEvent(
        event_id=f"demo-{context.opportunity.id}",
        idempotency_key=f"demo:{event_type.value}:{context.opportunity.id}",
        event_type=event_type,
        opportunity_id=context.opportunity.id,
        workspace_id="demo-workspace",
        user_id=context.opportunity.owner_id or "demo-user",
        source="manual",
        occurred_at=datetime.now(timezone.utc),
    )


def build_critical_context() -> DealContext:
    stale = load_fixture("risk_context_stale.json")
    data = stale.model_dump()
    data["opportunity"]["name"] = "Critical Deal — All Signals"
    data["opportunity"]["stage"] = "Negotiation"
    data["engagement"]["days_since_last_activity"] = 21
    data["engagement"]["activity_count_14d"] = 0
    data["engagement"]["activity_count_prior_14d"] = 12
    data["engagement"]["has_future_meeting"] = False
    data["contacts"] = [
        {
            "id": "contact-003",
            "name": "Intern",
            "role": "Coordinator",
            "is_decision_maker": False,
        },
    ]
    data["tasks"] = [
        {
            "id": "task-001",
            "title": "Legal review",
            "status": "TODO",
            "is_overdue": True,
        },
        {
            "id": "task-002",
            "title": "Send contract",
            "status": "TODO",
            "is_overdue": True,
        },
    ]
    data["timeline"] = []
    data["opportunity"]["stage_entered_at"] = (
        datetime.now(timezone.utc) - timedelta(days=45)
    ).isoformat()
    return DealContext.model_validate(data)


def build_closed_won_context() -> DealContext:
    healthy = load_fixture("risk_context_healthy.json")
    data = healthy.model_dump()
    data["opportunity"]["stage"] = "Closed Won"
    data["engagement"]["days_since_last_activity"] = 30
    data["engagement"]["has_future_meeting"] = False
    return DealContext.model_validate(data)


def build_engagement_drop_context() -> DealContext:
    healthy = load_fixture("risk_context_healthy.json")
    data = healthy.model_dump()
    data["opportunity"]["name"] = "Engagement Drop Co"
    data["engagement"]["days_since_last_activity"] = 3
    data["engagement"]["activity_count_14d"] = 2
    data["engagement"]["activity_count_prior_14d"] = 10
    data["engagement"]["has_future_meeting"] = True
    return DealContext.model_validate(data)


SCENARIOS: dict[str, tuple[str, DealContext, FollowUpEventType]] = {
    "healthy": (
        "Low-risk deal — active engagement, future meeting, decision maker",
        load_fixture("risk_context_healthy.json"),
        FollowUpEventType.OPPORTUNITY_UPDATED,
    ),
    "stale": (
        "At-risk deal — no activity, stalled stage, overdue tasks",
        load_fixture("risk_context_stale.json"),
        FollowUpEventType.ACTIVITY_LOGGED,
    ),
    "critical": (
        "Critical deal — many rules fire, score capped at 100",
        build_critical_context(),
        FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
    ),
    "closed_won": (
        "Closed Won — score computed but no notifications",
        build_closed_won_context(),
        FollowUpEventType.OPPORTUNITY_UPDATED,
    ),
    "engagement_drop": (
        "Engagement drop only — activity fell >50% vs prior 14 days",
        build_engagement_drop_context(),
        FollowUpEventType.ACTIVITY_LOGGED,
    ),
}


def print_breakdown(context: DealContext) -> None:
    breakdown = compute_risk_score(context)
    print(f"  Deal:     {context.opportunity.name}")
    print(f"  Stage:    {context.opportunity.stage}")
    print(f"  Company:  {context.company.name if context.company else '—'}")
    print(
        f"  Score:    {_c(_level_color(breakdown.level), str(breakdown.total))}"
        f" / 100  ({breakdown.level.upper()})",
    )
    if not breakdown.factors:
        print(f"  Factors:  {_c('32', 'none — deal looks healthy')}")
        return
    print("  Factors:")
    for factor in breakdown.factors:
        severity = _c(_level_color(factor.severity), factor.severity.upper())
        print(
            f"    • [{severity}] {factor.rule_id} (+{factor.points}): "
            f"{factor.detail}",
        )


def print_result(result, *, label: str = "Agent result") -> None:
    _header(label)
    score = result.risk_score
    print(
        f"  Final score: {_c(_level_color(score.level), str(score.score))}"
        f" ({score.level.upper()})",
    )
    print(f"  Reasoning:   {score.reasoning_summary}")
    if not result.notifications:
        print(f"  Notifications: {_c('32', 'none')}")
        return
    print(f"  Notifications ({len(result.notifications)}):")
    for index, notification in enumerate(result.notifications, start=1):
        severity = _c(_level_color(notification.severity), notification.severity)
        print(f"\n    [{index}] {notification.title}  ({severity})")
        print(f"        Rule: {notification.rule_id}")
        print(f"        Body: {notification.body}")
        print(f"        Why:  {notification.reasoning_summary}")


async def run_scenario(
    name: str,
    context: DealContext,
    event_type: FollowUpEventType,
    *,
    use_llm: bool,
    existing_notifications: list[Notification] | None = None,
) -> None:
    description, _, _ = SCENARIOS[name]
    _header(f"Scenario: {name}")
    print(f"  {description}")
    print_breakdown(context)

    event = build_event(context, event_type)
    from followup.agents.risk.notifications import template_notification_copy

    llm_generator = None if use_llm else template_notification_copy

    result = await run_risk_notification_agent(
        context,
        event,
        existing_notifications or [],
        llm_generator=llm_generator,
    )

    print_result(result)


async def demo_score_trend() -> None:
    _header("Score trend — compare_score_to_previous")
    store = InMemoryRiskSnapshotStore()
    opportunity_id = "opp-trend-001"
    workspace_id = "demo-workspace"

    snapshots = [
        ("Week 1 — healthy", 25),
        ("Week 2 — slipping", 45),
        ("Week 3 — at risk", 72),
    ]
    for label, score in snapshots:
        snapshot = await compare_score_to_previous(
            opportunity_id=opportunity_id,
            workspace_id=workspace_id,
            new_score=score,
            factors=[],
            snapshot_store=store,
            source="daily_sweep",
        )
        re_engagement = needs_re_engagement_draft(snapshot)
        delta = snapshot.delta if snapshot.delta is not None else "—"
        print(
            f"  {label}: score={score}, delta={delta}, "
            f"level={snapshot.level}, "
            f"re_engagement={re_engagement}",
        )


async def demo_daily_sweep(*, use_llm: bool) -> None:
    _header("Daily risk sweep — 3 dummy opportunities")
    store = InMemoryRiskSnapshotStore()
    from followup.agents.risk.notifications import template_notification_copy

    llm_generator = None if use_llm else template_notification_copy
    contexts = {
        "opp-healthy": load_fixture("risk_context_healthy.json"),
        "opp-stale": load_fixture("risk_context_stale.json"),
        "opp-critical": build_critical_context(),
    }

    async def list_active_opportunities(
        workspace_id: str,
    ) -> list[dict[str, str]]:
        return [
            {
                "id": opportunity_id,
                "owner_id": context.opportunity.owner_id or "demo-user",
            }
            for opportunity_id, context in contexts.items()
        ]

    async def load_deal_context(
        opportunity_id: str,
        workspace_id: str,
        user_id: str,
    ) -> DealContext:
        return contexts[opportunity_id]

    result = await run_daily_risk_sweep(
        "demo-workspace",
        list_active_opportunities=list_active_opportunities,
        load_deal_context_fn=load_deal_context,
        snapshot_store=store,
        llm_generator=llm_generator,
    )

    print(f"  Processed: {result.opportunities_processed}")
    print(f"  Skipped:   {result.opportunities_skipped}")
    print(f"  Notifications created: {result.notifications_created}")
    print(f"  Re-engagement triggers: {result.re_engagement_triggers}")
    print()
    for item in result.results:
        score = item.risk_score
        print(
            f"  • {item.opportunity_id}: "
            f"score={score.score} ({score.level}), "
            f"notifications={len(item.notifications)}, "
            f"re_engagement={item.needs_re_engagement_draft}",
        )


async def demo_dismissed_suppression(*, use_llm: bool) -> None:
    _header("Dismissed notification suppression")
    context = load_fixture("risk_context_stale.json")
    event = build_event(context, FollowUpEventType.ACTIVITY_LOGGED)
    from followup.agents.risk.notifications import template_notification_copy

    llm_generator = None if use_llm else template_notification_copy
    existing = [
        Notification(
            opportunity_id=context.opportunity.id,
            user_id="user-002",
            title="No recent activity",
            body="Old alert",
            severity="high",
            status="dismissed",
            rule_id="no_activity_7d",
            reasoning_summary="User dismissed 2 days ago",
            dismissed_at=datetime.now(timezone.utc) - timedelta(days=2),
        ),
    ]
    result = await run_risk_notification_agent(
        context,
        event,
        existing,
        llm_generator=llm_generator,
    )
    suppressed = not any(
        notification.rule_id == "no_activity_7d"
        for notification in result.notifications
    )
    print(f"  Score: {result.risk_score.score} ({result.risk_score.level})")
    print(f"  no_activity_7d suppressed: {suppressed}")
    if result.notifications:
        print(f"  Other alerts still fire: {[n.rule_id for n in result.notifications]}")
    else:
        print("  No new notifications (all suppressed or below threshold)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Demo the Follow-Up Risk Agent")
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="stale",
        help="Which dummy deal to analyze (default: stale)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every built-in scenario",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Demo daily risk sweep over 3 deals",
    )
    parser.add_argument(
        "--trend",
        action="store_true",
        help="Demo score snapshots and re-engagement triggers",
    )
    parser.add_argument(
        "--dismissed",
        action="store_true",
        help="Demo 7-day dismiss suppression",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use LLM for notification prose (requires LLM_* env vars)",
    )
    args = parser.parse_args()

    print(_c("1;35", "Follow-Up Risk Agent — Live Demo"))
    print("Using dummy deal data — no CRM connection required.")
    if args.use_llm:
        print(_c("33", "LLM mode ON — notification bodies generated by model"))
    else:
        print(_c("2", "Template mode — pass --use-llm for AI-written notification copy"))

    if args.sweep:
        await demo_daily_sweep(use_llm=args.use_llm)
    if args.trend:
        await demo_score_trend()
    if args.dismissed:
        await demo_dismissed_suppression(use_llm=args.use_llm)
    if args.all:
        for name, (_, context, event_type) in SCENARIOS.items():
            await run_scenario(
                name,
                context,
                event_type,
                use_llm=args.use_llm,
            )
    elif not args.sweep and not args.trend and not args.dismissed:
        description, context, event_type = SCENARIOS[args.scenario]
        await run_scenario(
            args.scenario,
            context,
            event_type,
            use_llm=args.use_llm,
        )

    print(f"\n{_c('32', 'Done.')}\n")


if __name__ == "__main__":
    asyncio.run(main())
