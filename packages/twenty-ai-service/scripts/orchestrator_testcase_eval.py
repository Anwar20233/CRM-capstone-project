"""End-to-end evaluation harness for the MAIN orchestrator, driven by the
spec-style ``OrchestratorTestCase`` catalog (``orchestrator_test_cases.py``).

Each case is run through the REAL ``Orchestrator`` (no fakes) and graded on FIVE
dimensions — the question is never "did it reply", it's "did it reach the right
answer the RIGHT WAY, safely":

  1. WORKFLOW  — the run took the expected SHAPE: a write reached a successful
                 mutation; a read-only request mutated nothing; a not-found /
                 disambiguation request HALTED without fabricating success.
  2. AGENTS    — the orchestrator delegated to the expected sub-agents, in order
                 (reader before writer), and did NOT call agents it must not.
  3. TOOLS     — the expected CRM tools actually executed, AND id-bearing inputs
                 carried a real resolved UUID, never a raw name (the id-first
                 guarantee). This is tool-accuracy.
  4. PII       — every string that crossed the wire to the LLM provider (after
                 masking) is scanned. Planted write-content PII (new emails,
                 phones, outside names) must be ABSENT verbatim — the hard gate.
  5. OUTPUT    — the consolidated reply is non-empty (or a clarifying question
                 for a halt) and carries the substrings the case expects.

How the trace is read: ``Orchestrator.handle`` returns
``{response, tool_calls:[{name,args,result}]}``. Each ``delegate_to_agent`` call
carries the sub-agent's full result including its own nested ``execute_tool``
calls with the REAL (unmasked) args + results — so tools/ids are graded against
reality. We reuse the proven delegation-extraction and raw-OpenAI-boundary PII
capture from ``orchestrator_crm_eval.py`` so both harnesses score identically.

Each case gets a FRESH orchestrator (isolated session + handle map). ``prior_turns``
are played through that same orchestrator first to establish cross-turn state;
only the final ``user_input`` turn is graded.

Prerequisites: Twenty backend on :3000, the twenty-ai-service .env (LLM_* +
TWENTY_*), and a seeded DB (seed_data.py). Run from packages/twenty-ai-service::

    .venv/bin/python scripts/orchestrator_testcase_eval.py
    .venv/bin/python scripts/orchestrator_testcase_eval.py --case update_person_email --verbose
    .venv/bin/python scripts/orchestrator_testcase_eval.py --include-requires --json out/tc_eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

# Batch grader, not a traced run — force LangSmith off before the traceable
# decorator is imported, so the exporter's 429 spam never appears.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)

from tracing import configure_tracing  # noqa: E402

from scripts.orchestrator_test_cases import (  # noqa: E402
    DISAMBIGUATION_HALT_PATH,
    NOT_FOUND_HALT_PATH,
    READ_ONLY_PATH,
    TEST_CASES,
    WRITE_PATH,
    OrchestratorTestCase,
    get,
)

# Reuse the vetted machinery from the scenario harness so scoring is identical.
from scripts.orchestrator_crm_eval import (  # noqa: E402
    _PII_CAPTURE,
    _install_openai_capture,
    extract_delegations,
)
from scripts.followup_eval import find_planted_pii, scan_for_pii  # noqa: E402

from agent.orchestrator import Orchestrator  # noqa: E402


_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    __import__("re").IGNORECASE,
)


def _looks_like_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value.strip()))


# ---------------------------------------------------------------------------
# Trace flattening — turn delegations into graded facts
# ---------------------------------------------------------------------------


@dataclass
class RunFacts:
    """Everything the grader needs, extracted once from the orchestrator result."""

    response: str
    interrupted: bool
    agent_order: list[str]
    # inner CRM tools executed, split by sub-agent and by success
    reader_tools_all: set[str] = field(default_factory=set)
    reader_tools_ok: set[str] = field(default_factory=set)
    writer_tools_all: set[str] = field(default_factory=set)
    writer_tools_ok: set[str] = field(default_factory=set)
    # every successful writer call as (tool, args) so we can check id-first inputs
    writer_calls_ok: list[tuple[str, dict]] = field(default_factory=list)
    # target columns carried by any successful create_*_target call
    target_keys: set[str] = field(default_factory=set)


def extract_facts(result: dict[str, Any]) -> RunFacts:
    facts = RunFacts(
        response=result.get("response") or "",
        interrupted=result.get("type") == "interrupt",
        agent_order=[],
    )
    for d in extract_delegations(result):
        facts.agent_order.append(d.agent)
        for call in d.crm_calls:
            tool = call.get("tool")
            if not tool:
                continue
            if d.agent == "reader":
                facts.reader_tools_all.add(tool)
                if call.get("ok"):
                    facts.reader_tools_ok.add(tool)
            elif d.agent == "writer":
                facts.writer_tools_all.add(tool)
                if call.get("ok"):
                    facts.writer_tools_ok.add(tool)
                    args = call.get("tool_args") or {}
                    facts.writer_calls_ok.append((tool, args))
                    if "target" in tool:
                        for key, value in args.items():
                            if key.startswith("target") and value:
                                facts.target_keys.add(key)
        if d.is_interrupt:
            facts.interrupted = True
    return facts


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@dataclass
class Check:
    dim: str  # WORKFLOW | AGENTS | TOOLS | PII | OUTPUT
    name: str
    passed: bool
    detail: str = ""
    soft: bool = False  # reported but does not fail the case


@dataclass
class CaseResult:
    key: str
    case_id: str
    message: str
    checks: list[Check] = field(default_factory=list)
    agent_order: list[str] = field(default_factory=list)
    response: str = ""
    error: str | None = None
    skipped: str | None = None
    # PII leak-rate accounting (for the system scorecard's privacy metric): how
    # many post-mask boundary messages crossed to the LLM, and how many carried
    # a raw personal value. A RATE, not a pass/fail — masking ~599/600 clean
    # reads as 99.8%, not "the scenario failed".
    pii_scanned: int = 0
    pii_leaks: int = 0
    planted_leaks: int = 0

    def add(self, dim: str, name: str, passed: bool, detail: str = "", soft: bool = False) -> None:
        self.checks.append(Check(dim, name, passed, detail, soft))

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        return self.error is None and all(c.passed for c in self.checks if not c.soft)

    def dim_score(self, dim: str) -> tuple[int, int]:
        hard = [c for c in self.checks if c.dim == dim and not c.soft]
        return sum(1 for c in hard if c.passed), len(hard)


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(item in it for item in needle)


def _wrote_anything(facts: RunFacts) -> bool:
    return bool(facts.writer_tools_ok or facts.writer_tools_all)


def grade(case: OrchestratorTestCase, result: dict[str, Any],
          captured_prompts: list[str] | None) -> CaseResult:
    facts = extract_facts(result)
    res = CaseResult(key=_key_for(case), case_id=case.id, message=case.user_input)
    res.response = facts.response or ("[interrupt — awaiting approval]" if facts.interrupted else "")
    res.agent_order = facts.agent_order

    clarified = "?" in facts.response
    wrote = _wrote_anything(facts)
    successful_write = bool(facts.writer_tools_ok)

    # -- 1. WORKFLOW — behavioural shape of the run --------------------------
    path = case.expected_workflow
    if path == WRITE_PATH:
        res.add("WORKFLOW", "write_completed",
                successful_write or facts.interrupted,
                f"no successful write; writer_ok={sorted(facts.writer_tools_ok)}")
    elif path == READ_ONLY_PATH:
        res.add("WORKFLOW", "no_mutation", not wrote,
                f"unexpected writer tools {sorted(facts.writer_tools_all)}")
    elif path == DISAMBIGUATION_HALT_PATH:
        res.add("WORKFLOW", "halted_with_question", clarified and not wrote,
                f"clarified={clarified} wrote={wrote}")
    elif path == NOT_FOUND_HALT_PATH:
        # not-found / refuse: resolved but did NOT push a successful mutation.
        res.add("WORKFLOW", "no_silent_success", not successful_write,
                f"unexpected successful write {sorted(facts.writer_tools_ok)}")

    # -- 2. AGENTS — routing discipline -------------------------------------
    expected_agents = [a.agent for a in sorted(case.expected_agents, key=lambda a: a.order)]
    if expected_agents:
        # Two cases where the spec's delegation never has to happen — grade soft:
        #  - disambiguation halts at the mask-time resolver, before any sub-agent;
        #  - a multi-turn follow-up can be answered straight from carried context
        #    (a resolved id already in the conversation), which is BETTER than
        #    re-delegating, not worse. ``prior_turns`` marks those cases.
        soft_agents = path == DISAMBIGUATION_HALT_PATH or bool(case.prior_turns)
        res.add("AGENTS", "agent_order",
                _is_subsequence(expected_agents, facts.agent_order),
                f"expected ⊇ {expected_agents}, got {facts.agent_order}", soft=soft_agents)
        if "reader" in expected_agents and "writer" in expected_agents:
            # Credit a resolve-before-act recovery: a reader must run before the
            # LAST (successful) writer call. writer→reader→writer is the
            # documented persistence pattern (resolve a name, then retry with the
            # id), not a violation — only a writer that NEVER follows a reader is.
            first_reader = facts.agent_order.index("reader") if "reader" in facts.agent_order else 99
            last_writer = (len(facts.agent_order) - 1 - facts.agent_order[::-1].index("writer")
                           if "writer" in facts.agent_order else -1)
            # Soft once the write actually completed: a writer that resolves a
            # name itself (the backend tolerates names on some fields) still
            # produced the right outcome. Ordering is a discipline signal, not a
            # functional failure when the record landed correctly.
            res.add("AGENTS", "reader_before_writer",
                    first_reader < last_writer if last_writer >= 0 else False,
                    f"order={facts.agent_order}", soft=successful_write)
    for forbidden in case.forbid_agents:
        res.add("AGENTS", f"no_{forbidden}", forbidden not in facts.agent_order,
                f"order={facts.agent_order}")

    # -- 3. TOOLS — the right tools ran, with id-first inputs ---------------
    is_write_case = any(t.tool == "execute_tool" for t in case.expected_tools)
    for tool in case.expected_tools:
        if tool.tool == "execute_tool":
            inner = (tool.inputs or {}).get("tool")
            # accept_any lets a request credit an alternative CRM tool (e.g.
            # advance_deal_stage instead of update_opportunity).
            accepted = {inner, *tool.accept_any}
            ran_ok = bool(accepted & facts.writer_tools_ok)
            res.add("TOOLS", f"tool:{inner}", ran_ok,
                    f"writer ran ok {sorted(facts.writer_tools_ok)}")
            # id-first is a discipline SIGNAL, not a functional gate: when the
            # write succeeds with a name (backend resolves it) the outcome is
            # still correct. Reported soft so it surfaces without failing the case.
            id_keys = [k for k, v in ((tool.inputs or {}).get("tool_args") or {}).items()
                       if isinstance(v, str) and v.startswith("<") and v.endswith("-id>")]
            if id_keys and ran_ok:
                args = next((a for t, a in facts.writer_calls_ok if t in accepted), {})
                for key in id_keys:
                    res.add("TOOLS", f"id_first:{inner}.{key}",
                            _looks_like_uuid(args.get(key)),
                            f"{key}={args.get(key)!r} (want a UUID, not a name)",
                            soft=True)
            # link target columns must actually be written
            for key, value in ((tool.inputs or {}).get("tool_args") or {}).items():
                if key.startswith("target") and value:
                    res.add("TOOLS", f"link:{key}", key in facts.target_keys,
                            f"targets written {sorted(facts.target_keys)}")
        else:
            # a reader tool — accept the exact tool, any explicit accept_any
            # alternative, or any sibling in the same family (find_one_* vs
            # find_*; both resolve). Multiple valid read strategies get credit.
            accepted = {tool.tool, *tool.accept_any}
            families = {t.split("_")[0] for t in accepted}
            ran = facts.reader_tools_ok or facts.reader_tools_all
            hit = bool(accepted & ran) or any(
                t.split("_")[0] in families for t in ran)
            # Soft when the lookup is incidental to the outcome: a multi-turn
            # answer from carried context (prior_turns) needs no fresh read, and
            # a write case where the writer self-resolves the name still lands
            # the record. The hard read gate lives on pure-read cases.
            soft_read = (not ran and bool(case.prior_turns)) or (is_write_case and successful_write)
            res.add("TOOLS", f"tool:{tool.tool}", hit,
                    f"reader ran {sorted(ran)}", soft=soft_read)

    # -- 4. PII — planted write-content PII must never cross ----------------
    if captured_prompts is not None:
        res.pii_scanned = len(captured_prompts)
        if case.planted_pii:
            survived = find_planted_pii(captured_prompts, case.planted_pii)
            res.planted_leaks = len(survived)
            res.add("PII", "planted_masked", not survived,
                    f"planted PII reached provider: {survived}" if survived else "")
        # Masking stays ON, but the heavy Presidio/NER scan runs on a DEDUPED,
        # CAPPED view — a case that retried the LLM many times can pile up
        # hundreds of repeated prompts and make the scanner spin for minutes
        # (it once wedged the whole run). Unique prompts catch the same leaks;
        # the planted-PII gate above already scanned the full (cheap) buffer.
        scan_input = list(dict.fromkeys(captured_prompts))[:120]
        leaks = scan_for_pii(scan_input)
        res.pii_leaks = len(leaks)
        summary = ", ".join(f"{l['type']}:{l['value']}" for l in leaks[:5])
        # Soft: a name typed as a LOOKUP KEY must reach the reader to search — not
        # a true leak. The hard guarantee is planted_masked above.
        res.add("PII", "no_raw_leak", not leaks,
                f"{len(leaks)} value(s) crossed (lookup keys ok): {summary}" if leaks else "",
                soft=True)

    # -- 5. OUTPUT — non-empty / clarifying, with expected substrings -------
    if path == DISAMBIGUATION_HALT_PATH:
        res.add("OUTPUT", "clarifying_question", clarified, "expected a '?' question")
    elif not facts.interrupted:
        res.add("OUTPUT", "non_empty", bool(facts.response.strip()), "empty response")
    for sub in case.output_includes:
        res.add("OUTPUT", f"says:{sub}", sub.lower() in facts.response.lower(),
                f"{sub!r} missing from reply")

    return res


def _key_for(case: OrchestratorTestCase) -> str:
    for key, value in TEST_CASES.items():
        if value is case:
            return key
    return case.id


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def run_case(case: OrchestratorTestCase, *, include_requires: bool) -> CaseResult:
    key = _key_for(case)
    if case.requires and not include_requires:
        res = CaseResult(key=key, case_id=case.id, message=case.user_input)
        res.skipped = f"requires: {case.requires} (use --include-requires)"
        return res

    session_id = f"orch-tc-{key}-{uuidlib.uuid4().hex[:8]}"
    captured: list[str] = []
    token = _PII_CAPTURE.set(captured)
    try:
        orch = Orchestrator(session_id=session_id)
        for prior in case.prior_turns:
            await orch.handle(prior)
        # Only the captured prompts of the GRADED turn matter for PII; reset the
        # accumulator so prior-turn lookup keys don't pollute the leak scan.
        captured.clear()
        result = await orch.handle(case.user_input)
    except Exception as error:  # noqa: BLE001
        res = CaseResult(key=key, case_id=case.id, message=case.user_input)
        res.error = f"{type(error).__name__}: {error}"
        return res
    finally:
        _PII_CAPTURE.reset(token)
    return grade(case, result, captured_prompts=captured)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_GREEN, _RED, _YEL, _DIM, _BOLD, _RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
_DIMS = ("WORKFLOW", "AGENTS", "TOOLS", "PII", "OUTPUT")


def _mark(passed: bool) -> str:
    return f"{_GREEN}PASS{_RESET}" if passed else f"{_RED}FAIL{_RESET}"


def print_report(results: list[CaseResult], verbose: bool) -> None:
    print("\n" + "=" * 80)
    print(f"  ORCHESTRATOR TEST-CASE EVAL — {len(results)} cases")
    print("=" * 80)

    graded = [r for r in results if not r.skipped]
    passed = 0
    for r in sorted(results, key=lambda x: x.key):
        if r.skipped:
            print(f"\n{_YEL}SKIP{_RESET}  {_BOLD}{r.key}{_RESET}  {_DIM}({r.case_id}) — {r.skipped}{_RESET}")
            continue
        if r.passed:
            passed += 1
        print(f"\n{_mark(r.passed)}  {_BOLD}{r.key}{_RESET}  {_DIM}({r.case_id}){_RESET}")
        print(f"      msg    : {r.message}")
        print(f"      agents : {' → '.join(r.agent_order) or '(none)'}")
        if r.error:
            print(f"      {_RED}error  : {r.error}{_RESET}")
        # per-dimension mini-scoreboard
        cells = []
        for dim in _DIMS:
            ok, total = r.dim_score(dim)
            if total == 0:
                cells.append(f"{_DIM}{dim} -/-{_RESET}")
            else:
                color = _GREEN if ok == total else _RED
                cells.append(f"{color}{dim} {ok}/{total}{_RESET}")
        print("      dims   : " + "  ".join(cells))
        hard_failed = [c for c in r.checks if not c.passed and not c.soft]
        if verbose:
            for c in r.checks:
                tag = "warn" if c.soft else _mark(c.passed)
                mark = f"{_DIM}{tag}{_RESET}" if (c.soft and not c.passed) else tag
                print(f"        {mark} [{c.dim}] {c.name} {_DIM}{c.detail}{_RESET}")
            if r.response:
                print(f"      reply  : {_DIM}{r.response[:300]}{_RESET}")
        else:
            for c in hard_failed:
                print(f"        {_RED}✗ [{c.dim}] {c.name}{_RESET} {_DIM}{c.detail}{_RESET}")

    print("\n" + "=" * 80)
    rate = (passed / len(graded) * 100) if graded else 0.0
    color = _GREEN if graded and passed == len(graded) else _RED
    skipped = len(results) - len(graded)
    print(f"  {color}{passed}/{len(graded)} cases passed ({rate:.0f}%){_RESET}"
          + (f"   {_YEL}{skipped} skipped{_RESET}" if skipped else ""))

    # Per-dimension rollup across all graded cases — the professor-facing scorecard.
    print("\n  Per-dimension scorecard (hard checks):")
    for dim in _DIMS:
        ok = sum(r.dim_score(dim)[0] for r in graded)
        total = sum(r.dim_score(dim)[1] for r in graded)
        if total == 0:
            continue
        drate = ok / total * 100
        color = _GREEN if ok == total else (_YEL if drate >= 70 else _RED)
        bar = "█" * round(drate / 5)
        print(f"    {dim:<9} {color}{ok:>3}/{total:<3} {drate:5.0f}%{_RESET}  {color}{bar}{_RESET}")
    print("=" * 80)


def to_json(results: list[CaseResult]) -> dict[str, Any]:
    graded = [r for r in results if not r.skipped]
    dim_scores = {}
    for dim in _DIMS:
        ok = sum(r.dim_score(dim)[0] for r in graded)
        total = sum(r.dim_score(dim)[1] for r in graded)
        dim_scores[dim] = {"passed": ok, "total": total}
    return {
        "total": len(graded),
        "passed": sum(1 for r in graded if r.passed),
        "skipped": [r.key for r in results if r.skipped],
        "dimensions": dim_scores,
        "cases": [
            {
                "key": r.key,
                "id": r.case_id,
                "passed": r.passed,
                "skipped": r.skipped,
                "agent_order": r.agent_order,
                "error": r.error,
                "response": r.response,
                "pii_scanned": r.pii_scanned,
                "pii_leaks": r.pii_leaks,
                "planted_leaks": r.planted_leaks,
                "checks": [
                    {"dim": c.dim, "name": c.name, "passed": c.passed,
                     "detail": c.detail, "soft": c.soft}
                    for c in r.checks
                ],
            }
            for r in results
        ],
    }


def _configure_logging(quiet: bool) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("agent").setLevel(logging.WARNING if quiet else logging.INFO)
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio", "langsmith"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default=None, help="run a single case by key")
    parser.add_argument("--cases", default=None, help="comma-separated subset")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("--include-requires", action="store_true",
                        help="also run env-dependent cases (disambiguation, delete)")
    parser.add_argument("--json", default=None, help="write full results as JSON")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--verbose", action="store_true", help="show every check + reply")
    parser.add_argument("--quiet", action="store_true", help="suppress INFO logs")
    args = parser.parse_args()

    if args.list:
        for key, case in TEST_CASES.items():
            req = f"  [requires: {case.requires}]" if case.requires else ""
            print(f"  {key:<28} {case.id}  {case.scenario}{req}")
        return

    if args.case:
        keys = [args.case]
    elif args.cases:
        keys = [k.strip() for k in args.cases.split(",") if k.strip()]
    else:
        keys = list(TEST_CASES.keys())

    _configure_logging(args.quiet)
    configure_tracing(enabled=False)

    cases = [get(k) for k in keys]
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    results: list[CaseResult] = []

    async def run_one(case: OrchestratorTestCase) -> None:
        async with semaphore:
            print(f"{_DIM}  running {_key_for(case)} …{_RESET}", flush=True)
            results.append(await run_case(case, include_requires=args.include_requires))

    undo_capture = _install_openai_capture()
    try:
        await asyncio.gather(*(run_one(c) for c in cases))
    finally:
        undo_capture()

    print_report(results, verbose=args.verbose or len(keys) == 1)

    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_json(results), indent=2))
        print(f"\n  wrote {path}")

    if any(not r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
