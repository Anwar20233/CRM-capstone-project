#!/usr/bin/env python3
"""Run the daily risk sweep using production sweep functions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from followup.context.stage_normalization import is_closed_stage
from followup.notifications.in_memory_repository import InMemoryNotificationRepository
from followup.store.risk_snapshot_store import InMemoryRiskSnapshotStore
from followup.workflows.risk_sweep.env import require_sweep_env
from followup.workflows.risk_sweep.sweep import (
    build_sweep_opportunity_record,
    run_daily_risk_sweep,
)


async def _list_active_opportunities(
    workspace_id: str,
    *,
    authenticated_user_id: str,
) -> list[dict[str, str]]:
    from bridge_client import forward
    from followup.context.bridge_parse import (
        build_find_opportunities_args,
        parse_opportunity_nodes_from_bridge_result,
    )

    result = await forward(
        "execute",
        {
            "tool": "find_opportunities",
            "args": build_find_opportunities_args(limit=50),
            "workspaceId": os.environ.get("TWENTY_WORKSPACE_ID", ""),
            "roleId": (
                os.environ.get("TWENTY_READER_ROLE_ID")
                or os.environ.get("TWENTY_ROLE_ID")
                or ""
            ),
            "userId": os.environ.get("TWENTY_USER_ID", ""),
        },
    )
    opportunities, status = parse_opportunity_nodes_from_bridge_result(result)
    if status != "ok":
        return []

    active: list[dict[str, str]] = []
    for opportunity in opportunities:
        stage = str(opportunity.get("stage", ""))
        if is_closed_stage(stage):
            continue
        active.append(
            build_sweep_opportunity_record(
                opportunity_id=str(opportunity["id"]),
                owner_id=str(opportunity.get("ownerId") or ""),
                authenticated_user_id=authenticated_user_id,
                stage=stage,
                name=str(opportunity.get("name") or opportunity["id"]),
            ),
        )
    return active


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daily risk sweep")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    workspace_id, authenticated_user_id, _role_id = require_sweep_env()

    snapshot_store = InMemoryRiskSnapshotStore()
    notification_repository = InMemoryNotificationRepository()
    active = await _list_active_opportunities(
        workspace_id,
        authenticated_user_id=authenticated_user_id,
    )

    async def list_active(_workspace_id: str) -> list[dict[str, str]]:
        return active

    result = await run_daily_risk_sweep(
        workspace_id,
        list_active_opportunities=list_active,
        snapshot_store=snapshot_store,
        notification_repository=notification_repository,
        use_llm_context=not args.no_llm,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
        return 0

    print(f"Workspace: {result.workspace_id}")
    print(
        f"Evaluated: {result.evaluated_count} | "
        f"Succeeded: {result.succeeded_count} | "
        f"Failed: {result.failed_count}"
    )
    print(f"Notifications created: {result.notifications_created}")
    print(f"Snapshots saved: {result.snapshot_count}")
    for item in result.results:
        if item.skipped:
            print(f"- {item.opportunity_id}: SKIPPED ({item.skip_reason})")
            continue
        factor_ids = [factor.rule_id for factor in item.risk_score.factors]
        print(
            f"- {item.opportunity_id}: score={item.risk_score.score} "
            f"level={item.risk_score.level} factors={factor_ids} "
            f"notifications={len(item.notifications)} delta={item.snapshot.delta}"
        )
    for error in result.errors:
        print(f"ERROR {error.opportunity_id}: {error.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
