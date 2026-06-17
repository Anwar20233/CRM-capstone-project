#!/usr/bin/env python3
"""Run the Risk Notification Agent against a live or fixture-backed DealContext."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from followup.agents.risk.agent import run_risk_notification_agent
from followup.agents.risk.evaluation import RuleEvaluation, evaluate_all_rules
from followup.context.loader import load_deal_context
from followup.context.schemas import DealContext
from followup.events.schemas import FollowUpEvent, FollowUpEventType


def _bridge_identity() -> dict[str, str]:
    return {
        "workspace_id": os.environ.get("TWENTY_WORKSPACE_ID", ""),
        "user_id": os.environ.get("TWENTY_USER_ID", ""),
        "role_id": (
            os.environ.get("TWENTY_READER_ROLE_ID")
            or os.environ.get("TWENTY_ROLE_ID")
            or ""
        ),
    }


def _load_fixture(name: str) -> DealContext:
    fixture_path = (
        pathlib.Path(__file__).resolve().parent.parent
        / "followup"
        / "tests"
        / "fixtures"
        / name
    )
    return DealContext.model_validate(json.loads(fixture_path.read_text()))


def _build_event(context: DealContext) -> FollowUpEvent:
    return FollowUpEvent(
        event_id=f"try-risk-{context.opportunity.id}",
        idempotency_key=f"try-risk:{context.opportunity.id}",
        event_type=FollowUpEventType.OPPORTUNITY_UPDATED,
        opportunity_id=context.opportunity.id,
        workspace_id=os.environ.get("TWENTY_WORKSPACE_ID", "workspace-001"),
        user_id=context.opportunity.owner_id or os.environ.get("TWENTY_USER_ID", "user-001"),
        occurred_at=datetime.now(timezone.utc),
    )


def _group_evaluations(
    evaluations: list[RuleEvaluation],
) -> tuple[list[dict], list[dict], list[dict]]:
    triggered: list[dict] = []
    not_triggered: list[dict] = []
    skipped: list[dict] = []
    for evaluation in evaluations:
        payload = {
            "rule_id": evaluation.rule_id,
            "reason": evaluation.reason,
        }
        if evaluation.status == "triggered" and evaluation.factor is not None:
            triggered.append(
                {
                    **payload,
                    "points": evaluation.factor.points,
                    "severity": evaluation.factor.severity,
                },
            )
        elif evaluation.status == "not_triggered":
            not_triggered.append(payload)
        else:
            skipped.append(payload)
    return triggered, not_triggered, skipped


def _format_completeness(context: DealContext) -> list[dict]:
    completeness = context.context_completeness
    if completeness is None:
        return []
    sections = [
        "opportunity",
        "company",
        "contacts",
        "timeline",
        "tasks",
        "meetings",
        "pipeline_metadata",
    ]
    formatted: list[dict] = []
    for section in sections:
        status = getattr(completeness, section)
        formatted.append(
            {
                "section": section,
                "status": status.status,
                "reason": status.reason,
                "source": status.source,
            },
        )
    return formatted


async def _run(
    context: DealContext,
    *,
    use_llm: bool,
    as_json: bool,
) -> int:
    evaluation_time = context.loaded_at or datetime.now(timezone.utc)
    evaluations = evaluate_all_rules(context, now=evaluation_time)
    triggered, not_triggered, skipped = _group_evaluations(evaluations)
    event = _build_event(context)

    async def no_llm_copy(draft, deal_context, risk_score=None):
        from followup.agents.risk.notifications import template_notification_copy

        return await template_notification_copy(
            draft,
            deal_context,
            risk_score=risk_score,
        )

    result = await run_risk_notification_agent(
        context,
        event,
        [],
        now=evaluation_time,
        llm_generator=None if use_llm else no_llm_copy,
    )

    payload = {
        "risk_score": result.risk_score.score,
        "risk_level": result.risk_score.level,
        "reasoning_summary": result.reasoning_summary,
        "triggered_factors": triggered,
        "not_triggered_rules": not_triggered,
        "skipped_rules": skipped,
        "context_completeness": _format_completeness(context),
        "notifications": [
            {
                "rule_id": notification.rule_id,
                "title": notification.title,
                "body": notification.body,
                "severity": notification.severity,
                "reasoning_summary": notification.reasoning_summary,
            }
            for notification in result.notifications
        ],
    }

    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"Opportunity: {context.opportunity.name} ({context.opportunity.stage})")
    print(f"Risk score:  {payload['risk_score']} ({payload['risk_level'].upper()})")
    print(f"Summary:     {payload['reasoning_summary']}")
    print("\nTriggered factors:")
    for factor in payload["triggered_factors"]:
        print(
            f"  - {factor['rule_id']} (+{factor['points']}): {factor['reason']}",
        )
    print("\nNot-triggered rules:")
    for rule in payload["not_triggered_rules"]:
        print(f"  - {rule['rule_id']}: {rule['reason']}")
    print("\nSkipped rules:")
    for rule in payload["skipped_rules"]:
        print(f"  - {rule['rule_id']}: {rule['reason']}")
    print("\nContext completeness:")
    for section in payload["context_completeness"]:
        line = f"  {section['section']}: {section['status']}"
        if section.get("reason"):
            line += f" ({section['reason']})"
        print(line)
    print("\nNotifications:")
    for notification in payload["notifications"]:
        print(f"  - [{notification['rule_id']}] {notification['title']}")
        print(f"    {notification['body']}")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Risk Notification Agent")
    parser.add_argument("opportunity_id", nargs="?")
    parser.add_argument("--fixture", help="Fixture filename under followup/tests/fixtures")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.fixture:
        context = _load_fixture(args.fixture)
    else:
        if not args.opportunity_id:
            parser.error("Provide an opportunity_id or --fixture")
        identity = _bridge_identity()
        context = await load_deal_context(
            args.opportunity_id,
            identity["workspace_id"],
            identity["user_id"],
            role_id=identity["role_id"],
            use_llm=False,
        )

    return await _run(context, use_llm=not args.no_llm, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
