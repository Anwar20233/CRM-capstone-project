"""Live timing harness for the FULL Follow-Up pipeline, run in parallel.

Runs the whole orchestrator graph (extraction → profile → risk → classify → plan
→ tasks → create_pending) for a batch of seeded inbound emails CONCURRENTLY, and
prints a wall-clock timeline + the pending action each run produced.

Parallelism model (the user's requirement): emails about DIFFERENT opportunities
run fully in parallel; emails about the SAME opportunity run their reader+LLM work
in parallel too, but serialize their extraction WRITES via a shared per-opp lock
(``deps.pipeline.write_locks``). Facts are the only read-modify-write hazard
(conflict resolution re-reads inside the lock); the risk snapshot and pending
action are independent inserts, safe under concurrency. Correctness of the write
ordering is covered by ``test_extract_from_emails_parallel_across_opps_...``.

This is the same pipeline the ``POST /followup/events`` endpoint runs, just driven
in-process for N emails at once instead of one request at a time.

Prerequisites (same as followup_e2e.py): Twenty backend on :3000, the .env
(LLM_* + TWENTY_*), and seeded data (seed_data.py) so the senders resolve.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_pipeline_batch.py
    .venv/bin/python scripts/followup_pipeline_batch.py --scenarios figma_buying_signal,notion_pricing_risk
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import uuid
from time import perf_counter
from typing import Any

# Make the package importable when run as a script, and load .env before any
# agent import reads os.environ for identity / LLM config.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from scripts.followup_email_scenarios import SCENARIOS, get  # noqa: E402

from followup.orchestrator.deps import OrchestratorDeps  # noqa: E402
from followup.orchestrator.graph import build_followup_graph  # noqa: E402
from followup.profile.extraction import _OpportunityWriteLocks  # noqa: E402
from followup.store.repositories import Database  # noqa: E402

# 5 emails / 4 deals: the Airbnb deal gets two emails (same sender) to exercise
# the per-opportunity write lock; the rest are distinct companies/deals.
DEFAULT_SCENARIOS = [
    "airbnb_new_stakeholder",
    "airbnb_new_stakeholder",  # second email about the SAME deal
    "stripe_champion_transition",
    "notion_pricing_risk",
    "figma_buying_signal",
]


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s : %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio", "followup", "agent"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _short(value: Any) -> str:
    return (str(value) if value else "—")[:8]


def _initial_state(scenario_name: str, workspace_id: str) -> dict[str, Any]:
    """The state POST /followup/events builds for one inbound email."""
    scenario = get(scenario_name)
    return {
        "entry_point": "email",
        "trigger": {
            "id": str(uuid.uuid4()),
            "sender_email": scenario.sender,
            "subject": scenario.subject,
            "body": scenario.body,
            "urgency": "medium",
        },
        "workspace_id": workspace_id,
        "run_id": str(uuid.uuid4()),
        "trace": [],
    }


def _timeline_bar(start: float, end: float, span: float, width: int = 48) -> str:
    if span <= 0:
        return "#" * width
    lead = int((start / span) * width)
    length = max(1, int(((end - start) / span) * width))
    length = min(length, width - lead)
    return " " * lead + "#" * length


def _pending_id(result: dict[str, Any]) -> Any:
    pending = result.get("pending_action")
    if isinstance(pending, dict):
        return pending.get("id")
    return getattr(pending, "id", None)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        default=",".join(DEFAULT_SCENARIOS),
        help="comma-separated scenario names (see --list)",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args()

    if args.list:
        print("Available email scenarios:\n")
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<28} {scenario.sender}  → {scenario.exercises}")
        return

    scenario_names = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    _configure_logging()
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")

    _rule("BATCH — full pipeline per email")
    for name in scenario_names:
        print(f"  {name:<28} sender={get(name).sender}")

    db = await Database.connect()
    try:
        deps = OrchestratorDeps.create(db)
        # Shared per-opportunity write locks: this is what makes same-deal emails
        # serialize their extraction writes while everything else runs parallel.
        deps.pipeline.write_locks = _OpportunityWriteLocks()
        deps.pipeline.get_chat_llm()  # pre-warm the lazy chat model

        graph = build_followup_graph(deps)
        states = [(name, _initial_state(name, workspace_id)) for name in scenario_names]

        async def run_one(name: str, state: dict[str, Any]) -> dict[str, Any]:
            start = perf_counter()
            try:
                result = await graph.ainvoke(state)
            except Exception as exc:  # noqa: BLE001
                result = {"status": "failed", "error": str(exc)}
            return {"name": name, "result": result, "start": start, "end": perf_counter()}

        _rule("RUNNING (concurrent full pipelines)")
        t0 = perf_counter()
        rows = await asyncio.gather(*(run_one(name, state) for name, state in states))
        total = perf_counter() - t0

        base = min(r["start"] for r in rows)
        span = max(r["end"] for r in rows) - base
        sum_individual = sum(r["end"] - r["start"] for r in rows)

        _rule("RESULTS")
        print(f"\n  {'scenario':<28} {'opp':<9} {'status':<10} {'pending':<9} {'secs':>6}  timeline")
        print(f"  {'-'*28} {'-'*9} {'-'*10} {'-'*9} {'-'*6}  {'-'*48}")
        by_opp: dict[str, int] = {}
        for r in rows:
            res = r["result"]
            opp = res.get("opportunity_id") or "(none)"
            by_opp[opp] = by_opp.get(opp, 0) + 1
            bar = _timeline_bar(r["start"] - base, r["end"] - base, span)
            print(
                f"  {r['name']:<28} {_short(opp):<9} {str(res.get('status')):<10} "
                f"{_short(_pending_id(res)):<9} {r['end'] - r['start']:>6.1f}  {bar}"
            )
            if res.get("error"):
                print(f"  {'':<28} └─ error: {res['error']}")

        _rule("SUMMARY")
        print(f"  emails                 : {len(rows)}")
        real_opps = {k for k in by_opp if k != '(none)'}
        print(f"  distinct opportunities : {len(real_opps)}")
        for opp, count in by_opp.items():
            marker = "  ← same-opp writes serialized" if count > 1 and opp != "(none)" else ""
            print(f"      {_short(opp)}: {count} email(s){marker}")
        print(f"\n  wall-clock (concurrent): {total:6.1f}s")
        print(f"  sum of per-email times : {sum_individual:6.1f}s")
        if total > 0:
            print(f"  effective speedup      : {sum_individual / total:6.1f}x")
        print(f"  target                 : < 120.0s  →  {'PASS' if total < 120 else 'FAIL'}")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
