"""Run all follow-up email scenarios through the live orchestrator pipeline.

Sequentially drives each scenario in ``followup_email_scenarios.py`` through the
real LangGraph pipeline, scores actual outputs against tagged expectations, and
prints reliability + behavioral scorecards for LangSmith review.

Prerequisites: same as ``followup_orchestrator_e2e.py`` (backend, .env, seed data).

Run from packages/twenty-ai-service:
    python scripts/followup_batch_e2e.py
    python scripts/followup_batch_e2e.py --verbose
    python scripts/followup_batch_e2e.py --scenario pricing_above_approved_budget

Tracing (LangSmith):
    1. Ensure .env has LANGSMITH_API_KEY + LANGCHAIN_PROJECT=twenty-ai-service
    2. Run this script — each scenario prints run=<uuid-prefix>
    3. Open https://smith.langchain.com → project twenty-ai-service
    4. Sort runs by time; match run_id from the batch table to the trace
    5. Drill into spans: extract_from_email, next_step_agent, drafting_agent
    6. For behavioral FAIL lines, open that run_id trace to see why
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.followup_email_scenarios import (  # noqa: E402
    EXPECTATIONS,
    SCENARIOS,
    get,
    list_scenario_names,
)
from scripts.followup_orchestrator_e2e import (  # noqa: E402
    ScenarioRunResult,
    _bootstrap_runtime,
    _configure_logging,
    _rule,
    run_scenario_batch,
)


def _run_preflight() -> None:
    from scripts.check_batch_prereqs import main as preflight_main  # noqa: E402

    print("Running preflight checks...\n")
    asyncio.run(preflight_main())
    print()
from scripts.followup_scenario_scoring import (  # noqa: E402
    aggregate_behavioral_scores,
    normalize_risk_score,
)


def _pct(passed: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{passed}/{total} ({100 * passed // total}%)"


def _print_summary(results: list[ScenarioRunResult]) -> None:
    passed = sum(1 for result in results if result.ok)
    failed = len(results) - passed
    plan_fallbacks = sum(1 for result in results if result.plan_fallback)

    _rule(
        f"RELIABILITY — {passed}/{len(results)} pipelines OK "
        f"({plan_fallbacks} next-step fallbacks excluded)"
    )
    header = (
        f"{'scenario':<42} {'status':<10} {'action':<14} "
        f"{'urgency':<8} {'risk':>6}  dr  cal  beh  run_id"
    )
    print(header)
    print("-" * len(header))

    for result in results:
        risk_value = normalize_risk_score(result.risk_score)
        risk = f"{risk_value:5.0f}" if risk_value is not None else "    -"
        draft = "Y" if result.has_draft else "N"
        calendar = "Y" if result.has_calendar else "N"
        behavior = "-"
        if result.behavioral is not None:
            behavior = "Y" if result.behavioral.behavioral_ok else "N"
        action = (result.action_type or "-")[:14]
        print(
            f"{result.name:<42} {result.status:<10} {action:<14} "
            f"{(result.urgency or '-'):<8} {risk}  {draft}   {calendar}   "
            f"{behavior}    {result.run_id[:8]}..."
        )
        if result.error:
            print(f"  error: {result.error[:120]}")
        if result.behavioral and not result.behavioral.behavioral_ok:
            for mismatch in result.behavioral.mismatches:
                print(f"  expected mismatch: {mismatch}")

    print(f"\n  completed: {passed}   failed: {failed}")

    behavioral_scores = [
        result.behavioral for result in results if result.behavioral is not None
    ]
    if not behavioral_scores:
        return

    totals = aggregate_behavioral_scores(behavioral_scores)
    _rule("BEHAVIORAL SCORECARD (actual vs EXPECTATIONS)")
    print(f"  overall pass     : {_pct(*totals['behavioral'])}")
    print(f"  pipeline match   : {_pct(*totals['pipeline'])}")
    if "plan" in totals:
        print(f"  real next-step     : {_pct(*totals['plan'])}")
    if "action" in totals:
        print(f"  action_type      : {_pct(*totals['action'])}")
    if "urgency" in totals:
        print(f"  urgency          : {_pct(*totals['urgency'])}")
    if "draft" in totals:
        print(f"  draft produced   : {_pct(*totals['draft'])}")
    if "calendar" in totals:
        print(f"  calendar check   : {_pct(*totals['calendar'])}")
    if "risk" in totals:
        print(f"  risk band        : {_pct(*totals['risk'])}")
    print("\n  LangSmith: https://smith.langchain.com")
    print("  Project  : twenty-ai-service (LANGCHAIN_PROJECT in .env)")
    print("  Match traces by run_id prefix in the table above.")


def _print_expectations_list() -> None:
    print("Scenarios with behavioral expectations:\n")
    for name in list_scenario_names():
        scenario = get(name)
        expectations = scenario.expectations or EXPECTATIONS.get(name)
        if expectations is None:
            continue
        action = ",".join(sorted(expectations.action_types)) or "-"
        urgency = ",".join(sorted(expectations.urgency)) or "-"
        draft = expectations.expect_draft
        calendar = expectations.expect_calendar
        risk = expectations.risk_band or "-"
        pipeline = "ok" if expectations.expect_pipeline_ok else "halt"
        print(f"  {name}")
        print(
            f"    pipeline={pipeline}  actions={action}  urgency={urgency}  "
            f"draft={draft}  calendar={calendar}  risk_band={risk}"
        )
        print(f"    exercises: {scenario.exercises}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        metavar="NAME",
        help="run only these scenario(s); repeat flag for multiple",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list scenarios with expectations and exit",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print per-node trace for each scenario (very noisy)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip DB/seed preflight checks",
    )
    args = parser.parse_args()

    if args.list:
        _print_expectations_list()
        return

    names = args.scenarios or list_scenario_names()
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}")

    _bootstrap_runtime()
    _configure_logging(quiet=not args.verbose)

    if not args.skip_preflight:
        _run_preflight()

    _rule(f"BATCH RUN — {len(names)} scenario(s)")
    results = await run_scenario_batch(names, verbose=args.verbose)
    _print_summary(results)

    behavioral_scores = [result.behavioral for result in results if result.behavioral]
    behavior_failed = any(
        score is not None and not score.behavioral_ok for score in behavioral_scores
    )
    reliability_failed = any(not result.ok for result in results)

    if reliability_failed or behavior_failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
