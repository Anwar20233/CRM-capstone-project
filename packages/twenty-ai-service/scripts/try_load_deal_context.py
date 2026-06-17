"""Load live deal context from Twenty and print a summary.

Run from packages/twenty-ai-service (with .env configured):

  .venv/bin/python scripts/try_load_deal_context.py <opportunity_id>
  .venv/bin/python scripts/try_load_deal_context.py <opportunity_id> --no-llm
  .venv/bin/python scripts/try_load_deal_context.py --list

Requires:
  - twenty-server running (agent-bridge on :3000)
  - TWENTY_WORKSPACE_ID, TWENTY_USER_ID, TWENTY_READER_ROLE_ID (or TWENTY_ROLE_ID)
  - LLM_API_KEY when using LLM mapping (default)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bridge_client import forward  # noqa: E402
from followup.context.bridge_parse import (  # noqa: E402
    build_find_opportunities_args,
    format_bridge_result_for_debug,
    format_opportunity_stage,
    parse_opportunity_nodes_from_bridge_result,
)
from followup.context.errors import ContextLoadError  # noqa: E402
from followup.context.loader import load_deal_context  # noqa: E402


def _bridge_identity() -> dict[str, str]:
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")
    user_id = os.environ.get("TWENTY_USER_ID", "")
    role_id = (
        os.environ.get("TWENTY_READER_ROLE_ID")
        or os.environ.get("TWENTY_ROLE_ID")
        or ""
    )
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role_id": role_id,
    }


async def list_opportunities(limit: int = 10) -> None:
    identity = _bridge_identity()
    missing = [key for key, value in identity.items() if not value]
    if missing:
        print("Missing env:", ", ".join(missing))
        print("Copy .env.example → .env and fill in Twenty identity vars.")
        sys.exit(1)

    result = await forward(
        "execute",
        {
            "tool": "find_opportunities",
            "args": build_find_opportunities_args(limit),
            "workspaceId": identity["workspace_id"],
            "roleId": identity["role_id"],
            "userId": identity["user_id"],
        },
    )
    if not result.get("ok"):
        error = result.get("error") or {}
        print("Bridge error:", error.get("message", result))
        sys.exit(1)

    opportunities, status = parse_opportunity_nodes_from_bridge_result(result)

    if status == "no_data":
        print("Bridge returned no data.")
        print("Bridge response:")
        print(format_bridge_result_for_debug(result))
        return

    if status == "unrecognized":
        print("Bridge response succeeded, but the response shape was not recognized.")
        print("Bridge response:")
        print(format_bridge_result_for_debug(result))
        return

    if status == "empty":
        print("No opportunities found in this workspace.")
        return

    print(f"{'ID':<38}  {'STAGE':<12}  NAME")
    print("-" * 80)
    for opportunity in opportunities:
        print(
            f"{opportunity.get('id', '?'):<38}  "
            f"{format_opportunity_stage(opportunity.get('stage')):<12}  "
            f"{opportunity.get('name', '?')}",
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Try load_deal_context against live CRM")
    parser.add_argument(
        "opportunity_id",
        nargs="?",
        help="Twenty opportunity UUID (from Opportunities table URL or --list)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List recent opportunities and exit",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM mapping; use deterministic fallback only",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full DealContext JSON",
    )
    args = parser.parse_args()

    if args.list:
        await list_opportunities()
        return

    if not args.opportunity_id:
        parser.print_help()
        sys.exit(1)

    identity = _bridge_identity()
    missing = [key for key, value in identity.items() if not value]
    if missing:
        print("Missing env:", ", ".join(missing))
        sys.exit(1)

    print(f"Loading context for {args.opportunity_id} ...")
    print(f"  bridge: {os.environ.get('NODE_BRIDGE_BASE_URL', 'http://localhost:3000/agent-bridge')}")
    print(f"  use_llm: {not args.no_llm}")
    print()

    try:
        context = await load_deal_context(
            args.opportunity_id,
            identity["workspace_id"],
            identity["user_id"],
            role_id=identity["role_id"],
            use_llm=not args.no_llm,
        )
    except ContextLoadError as error:
        print(f"FAILED [{error.code}]: {error.message}")
        sys.exit(1)

    if args.json:
        print(context.model_dump_json(indent=2))
        return

    print("OK — DealContext loaded")
    print(f"  provenance:     {context.context_provenance}")
    print(f"  opportunity:    {context.opportunity.name} ({context.opportunity.stage})")
    print(f"  amount:         {context.opportunity.amount}")
    print(f"  company:        {context.company.name if context.company else '—'}")
    print(f"  contacts:       {len(context.contacts)}")
    print(f"  timeline items: {len(context.timeline)}")
    print(f"  tasks:          {len(context.tasks)}")
    print(f"  meetings:       {len(context.meetings)}")
    print(f"  engagement:")
    print(f"    days_since_last_activity: {context.engagement.days_since_last_activity}")
    print(f"    activity_count_14d:       {context.engagement.activity_count_14d}")
    print(f"    has_future_meeting:       {context.engagement.has_future_meeting}")
    print()
    print("Tip: pass --json for the full payload, or --no-llm to test bridge-only mapping.")


if __name__ == "__main__":
    asyncio.run(main())
