"""Live timing harness for CONCURRENT extraction (``extract_from_emails``).

Runs a batch of seeded inbound emails through the real pipeline (live reader +
real LLM + live ``followup_agent`` Postgres) and prints a wall-clock timeline so
you can SEE the parallelism: emails about different opportunities run at the same
time; emails about the same opportunity still resolve+extract in parallel but
serialize their DB writes (the per-opp lock — correctness is covered by the unit
test ``test_extract_from_emails_parallel_across_opps_serialized_within_opp``).

The default batch is 5 emails across 4 deals, where TWO are about the Airbnb deal
(same sender, distinct source ids) so you can watch same-opp writes serialize
while the four deals run concurrently.

Prerequisites (same as followup_e2e.py): Twenty backend on :3000, the .env
(LLM_* + TWENTY_*), and seeded data (seed_data.py) so the senders resolve.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_batch_extract.py
    .venv/bin/python scripts/followup_batch_extract.py --scenarios airbnb_new_stakeholder,figma_buying_signal
    .venv/bin/python scripts/followup_batch_extract.py --sequential   # also run a one-at-a-time baseline
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

# Make the package importable when run as a script, and load .env before any
# agent import reads os.environ for identity / LLM config.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from scripts.followup_email_scenarios import SCENARIOS, get  # noqa: E402

from followup.profile.dependencies import PipelineDeps  # noqa: E402
from followup.profile.extraction import (  # noqa: E402
    EmailInput,
    _OpportunityWriteLocks,
    _resolve_and_extract,
    extract_from_email,
)
from followup.store.repositories import Database  # noqa: E402

# 5 emails / 4 deals: the Airbnb deal gets two emails (same sender) to exercise
# the same-opportunity write lock. The rest are distinct companies/deals.
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
    # Keep the timeline readable — silence transport/SDK chatter.
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio", "followup", "agent"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _short(value: str | None) -> str:
    return (value or "—")[:8]


def _build_emails(scenario_names: list[str], workspace_id: str) -> list[tuple[str, EmailInput]]:
    """Pair each scenario name with a fresh EmailInput (unique source_id per email)."""
    emails: list[tuple[str, EmailInput]] = []
    for name in scenario_names:
        scenario = get(name)
        source_text = f"Subject: {scenario.subject}\n\n{scenario.body}"
        emails.append(
            (
                name,
                EmailInput(
                    workspace_id=workspace_id,
                    source_type="email",
                    source_id=str(uuid.uuid4()),
                    source_text=source_text,
                    sender_email=scenario.sender,
                ),
            )
        )
    return emails


def _timeline_bar(start: float, end: float, span: float, width: int = 48) -> str:
    """ASCII bar showing [start, end] within the total run window."""
    if span <= 0:
        return "#" * width
    lead = int((start / span) * width)
    length = max(1, int(((end - start) / span) * width))
    length = min(length, width - lead)
    return " " * lead + "#" * length


async def _run_concurrent(
    deps: PipelineDeps, emails: list[tuple[str, EmailInput]]
) -> None:
    # Mirrors extract_from_emails (shared per-opp locks + one gather) but times
    # each email so we can render the overlap. Pre-warm the lazy chat model so the
    # first task doesn't pay the init cost alone.
    deps.get_chat_llm()
    locks = _OpportunityWriteLocks()

    async def timed(name: str, email: EmailInput) -> dict:
        start = perf_counter()
        outcome = await _resolve_and_extract(
            deps,
            workspace_id=email.workspace_id,
            source_type=email.source_type,
            source_id=email.source_id,
            source_text=email.source_text,
            sender_email=email.sender_email,
            opportunity_id=None,
            write_locks=locks,
        )
        end = perf_counter()
        return {"name": name, "outcome": outcome, "start": start, "end": end}

    _rule("CONCURRENT RUN (extract_from_emails path)")
    t0 = perf_counter()
    rows = await asyncio.gather(*(timed(name, email) for name, email in emails))
    total = perf_counter() - t0

    base = min(r["start"] for r in rows)
    span = max(r["end"] for r in rows) - base
    sum_individual = sum(r["end"] - r["start"] for r in rows)

    print(f"\n  {'scenario':<28} {'opp':<9} {'status':<12} {'secs':>6}  timeline")
    print(f"  {'-'*28} {'-'*9} {'-'*12} {'-'*6}  {'-'*48}")
    for r in rows:
        o = r["outcome"]
        bar = _timeline_bar(r["start"] - base, r["end"] - base, span)
        print(
            f"  {r['name']:<28} {_short(o.opportunity_id):<9} {o.status:<12} "
            f"{r['end'] - r['start']:>6.1f}  {bar}"
        )

    # Group by opportunity so same-deal emails are obvious.
    by_opp: dict[str, list[str]] = {}
    for r in rows:
        by_opp.setdefault(r["outcome"].opportunity_id or "(halted)", []).append(r["name"])

    _rule("SUMMARY")
    print(f"  emails               : {len(rows)}")
    print(f"  distinct opportunities: {len([k for k in by_opp if k != '(halted)'])}")
    for opp, names in by_opp.items():
        marker = "  ← same-opp writes serialized" if len(names) > 1 and opp != "(halted)" else ""
        print(f"      {_short(opp)}: {len(names)} email(s){marker}")
    print(f"\n  wall-clock (concurrent): {total:6.1f}s")
    print(f"  sum of per-email times : {sum_individual:6.1f}s")
    if total > 0:
        print(f"  effective speedup      : {sum_individual / total:6.1f}x")
    print(f"  target                 : < 120.0s  →  {'PASS' if total < 120 else 'FAIL'}")


async def _run_sequential(
    deps: PipelineDeps, emails: list[tuple[str, EmailInput]]
) -> None:
    _rule("SEQUENTIAL BASELINE (one extract_from_email at a time)")
    t0 = perf_counter()
    for name, email in emails:
        start = perf_counter()
        outcome = await extract_from_email(
            workspace_id=email.workspace_id,
            source_type=email.source_type,
            source_id=email.source_id,
            source_text=email.source_text,
            sender_email=email.sender_email,
            deps=deps,
        )
        print(f"  {name:<28} {_short(outcome.opportunity_id):<9} "
              f"{outcome.status:<12} {perf_counter() - start:>6.1f}s")
    print(f"\n  wall-clock (sequential): {perf_counter() - t0:6.1f}s")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        default=",".join(DEFAULT_SCENARIOS),
        help="comma-separated scenario names (see --list)",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="also run a one-at-a-time baseline for comparison (slow)",
    )
    args = parser.parse_args()

    if args.list:
        print("Available email scenarios:\n")
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<28} {scenario.sender}  → {scenario.exercises}")
        return

    scenario_names = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    _configure_logging()
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")
    emails = _build_emails(scenario_names, workspace_id)

    _rule("BATCH")
    for name, email in emails:
        print(f"  {name:<28} sender={email.sender_email}  source_id={_short(email.source_id)}")

    db = await Database.connect()
    try:
        deps = PipelineDeps.create(db)
        await _run_concurrent(deps, emails)
        if args.sequential:
            # Fresh ids so the baseline doesn't just hit "identical fact" skips.
            await _run_sequential(deps, _build_emails(scenario_names, workspace_id))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
