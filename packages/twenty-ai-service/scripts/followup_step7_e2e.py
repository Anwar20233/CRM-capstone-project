"""End-to-end live trace for STEP 7 — the full review loop with REAL writes.

Extends the Step-6 orchestrator trace: it drives the real LangGraph pipeline from
a seeded inbound email to a persisted pending action, then **accepts** that action
and runs the accept graph — which routes the write through the real CRM Write
agent (``WriterWorker``). The writer discovers the tool from the live backend
(``get_tool_catalog`` → ``learn_tools`` → ``execute_tool``) and executes it, so
this exercises the actual CRM tools (calendar/notes/email/task), not stubs.

What is real vs. hardcoded:
  * REAL LLM        — reader, extractor, classify, and the writer agent.
  * REAL tools      — calendar + notes reads (load_profile / check_calendar) and
                      the write on accept (send_email / create_note /
                      create_calendar_event / create_task), all via the bridge.
  * HARDCODED (mock)— the P2/P3/P4 subagents (next_step / risk / drafting) — the
                      AgentBundle mocks. The pipeline still CALLS them and uses
                      their output; only their internals are stubbed.

Prerequisites (you start these):
  * Twenty backend on :3000 (the bridge — NODE_BRIDGE_BASE_URL),
  * the twenty-ai-service .env: LLM_* provider + TWENTY_* identity,
  * seeded data (seed_data.py) so the sender resolves to a real person/deal,
  * for send_email to actually send: a connected account + SEND_EMAIL_TOOL
    permission on the configured role (otherwise the writer reports a clean
    permission failure — still a valid trace of the discovery path).

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_step7_e2e.py
    .venv/bin/python scripts/followup_step7_e2e.py --scenario airbnb_new_stakeholder
    .venv/bin/python scripts/followup_step7_e2e.py --list
    .venv/bin/python scripts/followup_step7_e2e.py --no-accept   # stop after pending action
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from tracing import configure_tracing  # noqa: E402

configure_tracing()

# Reuse the Step-6 trace harness verbatim (scenarios, renderers, logging).
from scripts.followup_email_scenarios import SCENARIOS, get  # noqa: E402
from scripts.followup_orchestrator_e2e import (  # noqa: E402
    DEFAULT_SCENARIO,
    _configure_logging,
    _render_node_update,
    _rule,
    _short,
)

from followup.orchestrator import OrchestratorDeps, build_followup_graph  # noqa: E402
from followup.orchestrator.graph import build_accept_graph  # noqa: E402
from followup.store.repositories import SCHEMA, Database  # noqa: E402


def _render_writer_event(event: dict) -> None:
    """Print one progress event from the writer agent (its tool discovery path)."""
    if event.get("type") != "tool_call":
        return
    name = event.get("name")
    args = event.get("args") or {}
    if name == "get_tool_catalog":
        print(f"      · discover  get_tool_catalog(object={args.get('object_name')}, "
              f"operation={args.get('operation')})")
    elif name == "learn_tools":
        print(f"      · learn     learn_tools({args.get('tool_names') or args.get('toolNames')})")
    elif name == "execute_tool":
        print(f"      · EXECUTE   {args.get('tool')}  args={_short(args.get('tool_args'), 240)}")
    else:
        print(f"      · {name}  {_short(args, 160)}")


async def _run_pipeline(deps, graph, scenario, sender, subject, body) -> dict:
    run_id = str(uuid.uuid4())
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")

    _rule(f"INPUT EMAIL — scenario: {scenario.name}")
    print(f"  exercises : {scenario.exercises}")
    print(f"  sender    : {sender}")
    print(f"  subject   : {subject}")
    print(f"  run_id    : {run_id}")
    print("  ---")
    print("  " + body.replace("\n", "\n  "))

    initial_state = {
        "entry_point": "email",
        "trigger": {
            "id": str(uuid.uuid4()),
            "sender_email": sender,
            "subject": subject,
            "body": body,
            "owner_user_id": os.environ.get("TWENTY_USER_ID"),
        },
        "workspace_id": workspace_id,
        "run_id": run_id,
        "status": "running",
        "trace": [],
    }

    _rule("LIVE TRACE — Step 6 pipeline (graph.astream)")
    final_state: dict = dict(initial_state)
    async for chunk in graph.astream(initial_state, stream_mode="updates"):
        for node, update in chunk.items():
            if isinstance(update, dict):
                final_state.update(update)
                _render_node_update(node, update)
    return final_state


async def _accept(deps, accept_graph, action_id: str) -> None:
    from agent.progress import reset_progress_sink, set_progress_sink

    _rule("ACCEPT → orchestrator→writer (real CRM write)")
    print("  The accept graph routes the action through the WriterWorker agent.")
    print("  Below: the writer's live tool-discovery + execution against the backend.\n")

    token = set_progress_sink(_render_writer_event)
    try:
        await accept_graph.ainvoke(
            {
                "action_id": action_id,
                "user_id": os.environ.get("TWENTY_USER_ID", str(uuid.uuid4())),
                "workspace_id": os.environ.get("TWENTY_WORKSPACE_ID", ""),
            }
        )
    finally:
        reset_progress_sink(token)

    updated = await deps.pipeline.pending_actions.get(uuid.UUID(action_id))
    _rule("EXECUTION RESULT")
    print(f"  status           : {updated.status}")
    print(f"  execution_status : {updated.execution_status}")
    print(f"  execution_error  : {_short(updated.execution_error, 400)}")
    print(f"  executed_at      : {updated.executed_at}")

    # Show the commitment fact logged on a completed write.
    async with deps.pipeline.executor.pool.acquire() as conn:
        fact = await conn.fetchrow(
            f"SELECT fact_type, fact_value, source_type FROM {SCHEMA}.profile_facts "
            f"WHERE opportunity_id = $1 AND fact_type = 'commitment' "
            f"ORDER BY extracted_at DESC LIMIT 1",
            updated.opportunity_id,
        )
    if fact:
        _rule("COMMITMENT FACT LOGGED (followup_agent.profile_facts)")
        print(f"  {fact['fact_type']} ({fact['source_type']}): {fact['fact_value']}")
    else:
        print("\n  (no commitment fact — write did not complete)")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO, choices=sorted(SCENARIOS))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--sender", default=None)
    parser.add_argument("--subject", default=None)
    parser.add_argument("--body", default=None)
    parser.add_argument("--no-accept", action="store_true", help="stop after the pending action")
    parser.add_argument("--quiet", action="store_true")
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

    db = await Database.connect()
    try:
        deps = OrchestratorDeps.create(db)
        graph = build_followup_graph(deps)
        accept_graph = build_accept_graph(deps)

        final_state = await _run_pipeline(deps, graph, scenario, sender, subject, body)

        _rule("FINAL PIPELINE STATE")
        print(f"  status : {final_state.get('status')}")
        print(f"  trace  : {' → '.join(final_state.get('trace') or [])}")
        if final_state.get("error"):
            print(f"  error  : {final_state['error']}")

        pending = final_state.get("pending_action")
        if not pending:
            print("\n  No pending action created — cannot accept. Stopping.")
            return

        _rule("PENDING ACTION")
        print(f"  id          : {pending.get('id')}")
        print(f"  action_type : {pending.get('action_type')}")
        print(f"  urgency     : {pending.get('urgency')}")

        if args.no_accept:
            print("\n  --no-accept set; stopping before the write.")
            return

        await _accept(deps, accept_graph, pending["id"])
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
