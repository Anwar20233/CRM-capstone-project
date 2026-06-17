"""End-to-end live trace for the Follow-Up ORCHESTRATOR (Step 6).

Drives the REAL LangGraph pipeline against running services (no fakes), starting
from a seeded inbound email and streaming every node as it fires:

  extract → load_profile → classify → next_step → run_tasks → create_pending

Each node hits live infrastructure:
  * extract       — reader+extractor resolve the sender → deal and write facts,
  * load_profile  — ProfileService synthesizes the deal picture (real LLM),
  * classify      — the triage LLM classifies the email,
  * next_step     — the P2 mock recommends an action,
  * run_tasks     — the hot-loaded task(s) run (check_calendar / draft_email),
  * create_pending— the pending action + run log are written to followup_agent.

It prints the per-node update stream (the "full trace"), then a final summary of
the recommendation, calendar result, draft, and the persisted pending action.

Prerequisites (the user starts these):
  * Twenty backend on :3000 (the bridge — NODE_BRIDGE_BASE_URL),
  * the twenty-ai-service .env: LLM_* provider + TWENTY_* identity,
  * seeded data (seed_data.py) so the sender resolves to a real person/deal.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_orchestrator_e2e.py
    .venv/bin/python scripts/followup_orchestrator_e2e.py --scenario figma_buying_signal
    .venv/bin/python scripts/followup_orchestrator_e2e.py --list
    .venv/bin/python scripts/followup_orchestrator_e2e.py --quiet   # hide DEBUG logs
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import uuid

# Make the package importable when run as a script, and load .env before any
# agent import reads os.environ for identity / LLM config.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from tracing import configure_tracing  # noqa: E402

configure_tracing()

from scripts.followup_email_scenarios import SCENARIOS, get  # noqa: E402

from followup.orchestrator import OrchestratorDeps, build_followup_graph  # noqa: E402
from followup.store.repositories import SCHEMA, Database  # noqa: E402

DEFAULT_SCENARIO = "airbnb_new_stakeholder"


def _configure_logging(quiet: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s : %(message)s",
        datefmt="%H:%M:%S",
    )
    level = logging.WARNING if quiet else logging.DEBUG
    logging.getLogger("followup").setLevel(level)
    logging.getLogger("agent").setLevel(level)
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _short(value, limit: int = 200) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + " …"


def _render_node_update(node: str, update: dict) -> None:
    """Print what one node produced — the heart of the live trace."""
    print(f"\n  ▶ node: {node}")
    for key, value in update.items():
        if key == "trace":
            continue
        if key == "plan" and value is not None:
            print(f"      plan           : {value.headline_action} — {_short(value.summary)}")
            for step in value.steps:
                print(f"          · {step.kind} ({step.priority}): {_short(step.intent, 120)}")
        elif key == "risk_assessment" and value is not None:
            print(f"      risk_score     : {value.risk_score:.2f} "
                  f"({len(value.factors)} factor(s))")
        elif key == "calendar" and value is not None:
            slots = value.available_slots
            print(f"      calendar       : all_busy={value.all_busy}, "
                  f"{len(slots)} proposed, "
                  f"{len(value.suggested_alternatives)} alternative(s)")
            for slot in slots[:3]:
                print(f"          - {slot.start} → {slot.end}  available={slot.available}")
        elif key == "draft" and value is not None:
            print(f"      draft.subject  : {value.subject}")
            print(f"      draft.to       : {value.recipient_email}")
            print(f"      draft.body     :")
            print("          " + (value.body or "").replace("\n", "\n          "))
        elif key == "deal_context" and value is not None:
            print(f"      deal           : {value.opportunity_name} "
                  f"(stage {value.deal_stage}, ${value.deal_value:,.0f})")
            print(f"      contacts       : {len(value.contacts)}, "
                  f"open_concerns: {len(value.open_concerns)}, "
                  f"risk_score: {value.risk_score}")
        elif key == "classification" and value is not None:
            print(f"      classification : {value}")
        elif key == "opportunity_id":
            print(f"      opportunity_id : {value}")
        elif key == "pending_action" and value is not None:
            print(f"      pending_action : id={value.get('id')} "
                  f"type={value.get('action_type')} status={value.get('status')}")
        elif key in ("status", "error", "profile_narrative", "task_results"):
            print(f"      {key:<15}: {_short(value, 300)}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, choices=sorted(SCENARIOS))
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--sender", default=None)
    parser.add_argument("--subject", default=None)
    parser.add_argument("--body", default=None)
    parser.add_argument("--quiet", action="store_true", help="hide DEBUG logs")
    args = parser.parse_args()

    if args.list:
        print("Available email scenarios:\n")
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<28} {scenario.sender}")
            print(f"  {'':<28} → {scenario.exercises}\n")
        return

    scenario = get(args.scenario)
    sender = args.sender or scenario.sender
    subject = args.subject or scenario.subject
    body = args.body or scenario.body

    _configure_logging(args.quiet)
    run_id = str(uuid.uuid4())
    trigger_id = str(uuid.uuid4())
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")

    _rule(f"INPUT EMAIL — scenario: {scenario.name}")
    print(f"  exercises : {scenario.exercises}")
    print(f"  sender    : {sender}")
    print(f"  subject   : {subject}")
    print(f"  run_id    : {run_id}")
    print(f"  workspace : {workspace_id}")
    print("  ---")
    print("  " + body.replace("\n", "\n  "))

    db = await Database.connect()
    try:
        deps = OrchestratorDeps.create(db)
        graph = build_followup_graph(deps)

        initial_state = {
            "entry_point": "email",
            "trigger": {
                "id": trigger_id,
                "sender_email": sender,
                "subject": subject,
                "body": body,
                # owner_user_id lets check_calendar query the rep's calendar; the
                # configured identity user is the rep here.
                "owner_user_id": os.environ.get("TWENTY_USER_ID"),
            },
            "workspace_id": workspace_id,
            "run_id": run_id,
            "status": "running",
            "trace": [],
        }

        _rule("LIVE TRACE — node-by-node (graph.astream)")
        final_state: dict = dict(initial_state)
        async for chunk in graph.astream(initial_state, stream_mode="updates"):
            # updates mode yields {node_name: partial_update} per completed node.
            for node, update in chunk.items():
                if isinstance(update, dict):
                    final_state.update(update)
                    _render_node_update(node, update)

        _rule("FINAL STATE")
        print(f"  status   : {final_state.get('status')}")
        print(f"  trace    : {' → '.join(final_state.get('trace') or [])}")
        if final_state.get("error"):
            print(f"  error    : {final_state['error']}")

        pending = final_state.get("pending_action")
        if pending:
            _rule("PERSISTED PENDING ACTION (followup_agent.followup_pending_actions)")
            print(f"  id          : {pending.get('id')}")
            print(f"  action_type : {pending.get('action_type')}")
            print(f"  urgency     : {pending.get('urgency')}")
            print(f"  expires_at  : {pending.get('expires_at')}")
            print(f"  reasoning   : {_short(pending.get('reasoning'), 300)}")
            await _print_run_log(db, run_id)
    finally:
        await db.close()


async def _print_run_log(db: Database, run_id: str) -> None:
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT entry_point, status, agents_invoked, pending_action_id "
            f"FROM {SCHEMA}.followup_runs WHERE id = $1",
            uuid.UUID(run_id),
        )
    if row:
        print(f"\n  run log     : entry={row['entry_point']} status={row['status']} "
              f"agents={row['agents_invoked']}")


if __name__ == "__main__":
    asyncio.run(main())
