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
    python scripts/followup_orchestrator_e2e.py
    python scripts/followup_orchestrator_e2e.py --scenario champion_leaving_internal_transfer
    python scripts/followup_orchestrator_e2e.py --list
    python scripts/followup_orchestrator_e2e.py --quiet   # hide DEBUG logs

Run all 30 scenarios with a summary table:
    python scripts/followup_batch_e2e.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import pathlib
import sys
import uuid
from dataclasses import dataclass, replace

# Make the package importable when run as a script.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.followup_email_scenarios import (  # noqa: E402
    DEFAULT_SCENARIO,
    SCENARIOS,
    EmailScenario,
    get,
)
from scripts.followup_scenario_scoring import BehavioralScore, plan_used_fallback  # noqa: E402


@dataclass(frozen=True)
class ScenarioRunResult:
    name: str
    sender: str
    exercises: str
    run_id: str
    status: str
    action_type: str | None
    urgency: str | None
    risk_score: float | None
    has_draft: bool
    has_calendar: bool
    opportunity_id: str | None
    trace: str
    error: str | None
    plan_fallback: bool = False
    behavioral: BehavioralScore | None = None

    @property
    def ok(self) -> bool:
        return (
            self.status == "completed"
            and not self.error
            and not self.plan_fallback
        )


def _bootstrap_runtime() -> None:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

    from tracing import configure_tracing  # noqa: E402

    configure_tracing()


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


def _result_from_state(
    scenario: EmailScenario,
    run_id: str,
    final_state: dict,
    *,
    behavioral: BehavioralScore | None = None,
) -> ScenarioRunResult:
    pending = final_state.get("pending_action") or {}
    risk = final_state.get("risk_assessment")
    return ScenarioRunResult(
        name=scenario.name,
        sender=scenario.sender,
        exercises=scenario.exercises,
        run_id=run_id,
        status=str(final_state.get("status") or "unknown"),
        action_type=pending.get("action_type"),
        urgency=pending.get("urgency"),
        risk_score=risk.risk_score if risk is not None else None,
        has_draft=final_state.get("draft") is not None,
        has_calendar=final_state.get("calendar") is not None,
        opportunity_id=final_state.get("opportunity_id"),
        trace=" → ".join(final_state.get("trace") or []),
        error=final_state.get("error"),
        plan_fallback=plan_used_fallback(final_state),
        behavioral=behavioral,
    )


async def run_email_scenario(
    scenario: EmailScenario,
    *,
    graph,
    verbose: bool = False,
) -> ScenarioRunResult:
    run_id = str(uuid.uuid4())
    trigger_id = str(uuid.uuid4())
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")

    if verbose:
        _rule(f"INPUT EMAIL — scenario: {scenario.name}")
        print(f"  exercises : {scenario.exercises}")
        print(f"  sender    : {scenario.sender}")
        print(f"  subject   : {scenario.subject}")
        print(f"  run_id    : {run_id}")
        print(f"  workspace : {workspace_id}")
        print("  ---")
        print("  " + scenario.body.replace("\n", "\n  "))

    initial_state = {
        "entry_point": "email",
        "trigger": {
            "id": trigger_id,
            "sender_email": scenario.sender,
            "subject": scenario.subject,
            "body": scenario.body,
            "owner_user_id": os.environ.get("TWENTY_USER_ID"),
        },
        "workspace_id": workspace_id,
        "run_id": run_id,
        "status": "running",
        "trace": [],
    }

    final_state: dict = dict(initial_state)
    if verbose:
        _rule("LIVE TRACE — node-by-node (graph.astream)")

    async for chunk in graph.astream(initial_state, stream_mode="updates"):
        for node, update in chunk.items():
            if isinstance(update, dict):
                final_state.update(update)
                if verbose:
                    _render_node_update(node, update)

    if verbose:
        _rule("FINAL STATE")
        print(f"  status   : {final_state.get('status')}")
        print(f"  trace    : {' → '.join(final_state.get('trace') or [])}")
        if final_state.get("error"):
            print(f"  error    : {final_state['error']}")

    return _result_from_state(scenario, run_id, final_state)


async def run_scenario_batch(
    names: list[str],
    *,
    verbose: bool = False,
) -> list[ScenarioRunResult]:
    from followup.orchestrator import OrchestratorDeps, build_followup_graph  # noqa: E402
    from followup.store.repositories import Database  # noqa: E402
    from scripts.followup_scenario_scoring import score_behavior  # noqa: E402

    db = await Database.connect()
    results: list[ScenarioRunResult] = []
    try:
        deps = OrchestratorDeps.create(db)
        graph = build_followup_graph(deps)
        for index, name in enumerate(names, start=1):
            scenario = get(name)
            print(f"\n[{index}/{len(names)}] {scenario.name} ({scenario.sender})")
            try:
                raw = await run_email_scenario(scenario, graph=graph, verbose=verbose)
            except Exception as exc:  # noqa: BLE001 — keep batch alive per scenario
                raw = ScenarioRunResult(
                    name=scenario.name,
                    sender=scenario.sender,
                    exercises=scenario.exercises,
                    run_id="",
                    status="failed",
                    action_type=None,
                    urgency=None,
                    risk_score=None,
                    has_draft=False,
                    has_calendar=False,
                    opportunity_id=None,
                    trace="",
                    error=str(exc),
                    plan_fallback=False,
                )
                print(f"  -> CRASH  {_short(str(exc), 200)}")
            behavioral = score_behavior(scenario.expectations, raw)
            result = replace(raw, behavioral=behavioral)
            results.append(result)
            status = "OK" if result.ok else "FAIL"
            behavior = ""
            if behavioral is not None:
                behavior = "  BEHAVIOR=" + ("PASS" if behavioral.behavioral_ok else "FAIL")
            plan_note = "  PLAN=fallback" if result.plan_fallback else ""
            print(
                f"  -> {status}{behavior}{plan_note}  status={result.status}  "
                f"action={result.action_type or '-'}  "
                f"urgency={result.urgency or '-'}  run={result.run_id[:8]}..."
            )
            if behavioral and not behavioral.behavioral_ok:
                for mismatch in behavioral.mismatches:
                    print(f"  -> expected: {mismatch}")
            if result.error:
                print(f"  -> error: {_short(result.error, 160)}")
    finally:
        await db.close()
    return results


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
            print(f"  {scenario.name:<42} {scenario.sender}")
            print(f"  {'':<42} -> {scenario.exercises}\n")
        return

    _bootstrap_runtime()
    _configure_logging(args.quiet)

    scenario = get(args.scenario)
    if args.sender or args.subject or args.body:
        scenario = EmailScenario(
            name=scenario.name,
            sender=args.sender or scenario.sender,
            subject=args.subject or scenario.subject,
            body=args.body or scenario.body,
            exercises=scenario.exercises,
        )

    from followup.orchestrator import OrchestratorDeps, build_followup_graph  # noqa: E402
    from followup.store.repositories import SCHEMA, Database  # noqa: E402

    db = await Database.connect()
    try:
        deps = OrchestratorDeps.create(db)
        graph = build_followup_graph(deps)
        result = await run_email_scenario(scenario, graph=graph, verbose=True)

        if result.action_type:
            _rule("PERSISTED PENDING ACTION (followup_agent.followup_pending_actions)")
            print(f"  action_type : {result.action_type}")
            print(f"  urgency     : {result.urgency}")
            print(f"  run_id      : {result.run_id}")
            await _print_run_log(db, result.run_id)
    finally:
        await db.close()

    if not result.ok:
        raise SystemExit(1)


async def _print_run_log(db, run_id: str) -> None:
    from followup.store.repositories import SCHEMA  # noqa: E402

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
