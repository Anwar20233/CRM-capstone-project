"""Dump the full Step 3 profile for one opportunity — what the agent receives.

Read-only (no extraction, no writes besides the synthesis LLM call). Prints, for
a given opportunity id:

  1. RAW TABLES — the actual followup_agent rows behind the profile
     (profile_facts, profile_relationships, shadow_entities, risk_snapshots).
  2. ProfileNarrative — exactly what ProfileService.build_profile_narrative
     returns (the synthesized briefing + the structured graph), as JSON.
  3. DealContext — exactly what build_deal_context returns, as JSON.

This is the downstream view: when the Follow-Up agent asks "what does this
company's deal profile look like?", sections 2–3 are the object it gets.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_profile_dump.py
    .venv/bin/python scripts/followup_profile_dump.py --opportunity <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import asdict
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from followup.profile.dependencies import PipelineDeps  # noqa: E402
from followup.profile.service import ProfileNotFound, ProfileService  # noqa: E402
from followup.store.repositories import SCHEMA, Database  # noqa: E402

# Airbnb — Platform Integration: a seeded deal with facts, a shadow, and risk.
DEFAULT_OPPORTUNITY = "a45a119e-376d-5eb2-8b96-33c2631660ea"


def _json(value) -> str:
    def _default(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    return json.dumps(value, indent=2, default=_default, ensure_ascii=False)


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


async def _dump_raw_tables(db: Database, opportunity_id: str) -> None:
    async with db.pool.acquire() as conn:
        for table, order in (
            ("profile_facts", "extracted_at DESC"),
            ("profile_relationships", "first_seen_at DESC"),
            ("shadow_entities", "mention_count DESC"),
            ("risk_snapshots", "computed_at DESC"),
        ):
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.{table} WHERE opportunity_id = $1 ORDER BY {order}",
                opportunity_id,
            )
            print(f"\n--- {SCHEMA}.{table}  ({len(rows)} row(s)) ---")
            for row in rows:
                print(_json(dict(row)))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opportunity", default=DEFAULT_OPPORTUNITY)
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="skip the synthesis LLM call (narrative will be blank-ish)",
    )
    args = parser.parse_args()

    db = await Database.connect()
    try:
        _rule(f"RAW followup_agent TABLES for opportunity {args.opportunity}")
        await _dump_raw_tables(db, args.opportunity)

        deps = PipelineDeps.create(db)
        service = ProfileService(deps)

        try:
            narrative = await service.build_profile_narrative(args.opportunity)
            context = await service.build_deal_context(args.opportunity)
        except ProfileNotFound as exc:
            _rule("PROFILE NOT FOUND")
            print(f"  {exc}")
            return

        _rule("STEP 3 OUTPUT — ProfileNarrative (what the agent receives)")
        print(_json(asdict(narrative)))

        _rule("STEP 3 OUTPUT — DealContext (what the agent receives)")
        print(_json(asdict(context)))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
