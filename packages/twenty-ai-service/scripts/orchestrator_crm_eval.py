"""Client-grade evaluation harness for the MAIN orchestrator (agent/orchestrator.py).

Runs every scenario in ``orchestrator_crm_scenarios.py`` through the REAL
``Orchestrator`` (no fakes) and grades whether it reached the right answer the
RIGHT WAY — the metric isn't "did it reply", it's:

  1. AGENTS   — the orchestrator delegated to the correct sub-agents, in the
                correct order (reader before writer when both are needed), and
                did NOT call agents it should not have (never "find" a record it
                is about to CREATE).
  2. READ     — the reader resolved the expected record / ran the expected
                composite read (proves the lookup actually happened, not just
                narrated).
  3. WRITE    — the writer executed the expected CRM tools successfully, and any
                note/task it created is LINKED to every entity it should target
                (an unlinked note floats invisibly = failure).
  4. RESPONSE — the final consolidated reply is non-empty and carries the
                substrings the scenario expects.

How the trace is read: ``Orchestrator.handle`` returns
``{response, tool_calls:[{name,args,result}]}``. Each ``delegate_to_agent`` call
carries the sub-agent's full result (``result.data``) including its own nested
``tool_calls`` — the reader/writer ``execute_tool`` calls. Those logs hold the
REAL (unmasked) tool args + results, so we grade against real ids and tool names.

This is the orchestrator analogue of ``followup_eval.py``. Each scenario gets a
FRESH orchestrator (isolated session + handle map) so runs don't leak state.

Prerequisites:
  * Twenty backend on :3000 (NODE_BRIDGE_BASE_URL),
  * the twenty-ai-service .env (LLM_* + TWENTY_*),
  * seeded data (seed_data.py) so the names resolve.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/orchestrator_crm_eval.py
    .venv/bin/python scripts/orchestrator_crm_eval.py --scenario w_note_person_and_deal --verbose
    .venv/bin/python scripts/orchestrator_crm_eval.py --scenarios r_find_person,w_advance_stage
    .venv/bin/python scripts/orchestrator_crm_eval.py --concurrency 4 --json out/orch_eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import os
import pathlib
import sys
import uuid
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

# This is a batch grader, not a traced run — force LangSmith off before anything
# imports the traceable decorator, so the exporter's 429 spam never appears.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)

from tracing import configure_tracing  # noqa: E402

from scripts.orchestrator_crm_scenarios import (  # noqa: E402
    SCENARIOS,
    OrchExpectations,
    OrchScenario,
    get,
)

from agent.orchestrator import Orchestrator  # noqa: E402

# PII scanners are pure (Presidio + regex backstop, graceful fallback) — reuse the
# email harness's vetted implementations so both harnesses score leaks identically.
from scripts.followup_eval import find_planted_pii, scan_for_pii  # noqa: E402


# ---------------------------------------------------------------------------
# PII capture — at the RAW OpenAI-SDK boundary (not LangChain)
# ---------------------------------------------------------------------------
#
# The reader/writer/orchestrator loops call ``LLMClient.get_openai_client()...
# chat.completions.create(messages=...)`` directly with ALREADY-MASKED messages.
# We wrap that one method so every string that actually crosses the wire to the
# provider is recorded into the per-scenario accumulator routed by a contextvar
# (so concurrent scenarios never cross-contaminate). This is the main-orchestrator
# analogue of followup_eval's LangChain callback capture.

_PII_CAPTURE: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "orch_eval_pii_capture", default=None
)


def _install_openai_capture() -> "callable":
    import agent.llm_client as llm_client

    original = llm_client.LLMClient.get_openai_client

    def patched(self: Any) -> Any:
        client = original(self)
        if getattr(client, "_orch_eval_wrapped", False):
            return client
        completions = client.chat.completions
        inner_create = completions.create

        def wrapped_create(*args: Any, **kwargs: Any) -> Any:
            accumulator = _PII_CAPTURE.get()
            if accumulator is not None:
                for message in kwargs.get("messages", []) or []:
                    if not isinstance(message, dict):
                        continue
                    # Skip the static system prompt: it carries only baked-in
                    # EXAMPLE names (Sarah Kim, Dana, …), never real CRM data, so
                    # scanning it produces phantom "leaks". Real PII that crosses
                    # the wire rides in user / assistant / tool messages.
                    if message.get("role") == "system":
                        continue
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        accumulator.append(content)
            return inner_create(*args, **kwargs)

        try:
            completions.create = wrapped_create  # type: ignore[assignment]
            client._orch_eval_wrapped = True
        except Exception:  # noqa: BLE001 — best effort; some SDK objects freeze attrs
            pass
        return client

    llm_client.LLMClient.get_openai_client = patched  # type: ignore[assignment]

    def undo() -> None:
        llm_client.LLMClient.get_openai_client = original  # type: ignore[assignment]

    return undo


# ---------------------------------------------------------------------------
# Trace extraction
# ---------------------------------------------------------------------------


@dataclass
class Delegation:
    agent: str
    instruction: str
    ok: bool
    data: dict[str, Any]

    @property
    def crm_calls(self) -> list[dict[str, Any]]:
        """The sub-agent's executed `execute_tool` calls: {tool, ok, args}."""
        calls: list[dict[str, Any]] = []
        for tc in self.data.get("tool_calls") or []:
            if tc.get("name") != "execute_tool":
                continue
            args = tc.get("args") or {}
            result = tc.get("result") or {}
            calls.append(
                {
                    "tool": args.get("tool"),
                    "tool_args": args.get("tool_args") or {},
                    "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
                }
            )
        return calls

    @property
    def is_interrupt(self) -> bool:
        return isinstance(self.data, dict) and self.data.get("type") == "interrupt"


def extract_delegations(result: dict[str, Any]) -> list[Delegation]:
    out: list[Delegation] = []
    for tc in result.get("tool_calls") or []:
        if tc.get("name") != "delegate_to_agent":
            continue
        args = tc.get("args") or {}
        res = tc.get("result") or {}
        data = res.get("data") if isinstance(res, dict) else {}
        out.append(
            Delegation(
                agent=(args.get("agent") or "").lower(),
                instruction=args.get("instruction") or "",
                ok=bool(res.get("ok")) if isinstance(res, dict) else False,
                data=data if isinstance(data, dict) else {},
            )
        )
    return out


def _ok_tools_for(delegations: list[Delegation], agent: str) -> set[str]:
    tools: set[str] = set()
    for d in delegations:
        if d.agent != agent:
            continue
        for call in d.crm_calls:
            if call["ok"] and call["tool"]:
                tools.add(call["tool"])
    return tools


def _all_tools_for(delegations: list[Delegation], agent: str) -> set[str]:
    tools: set[str] = set()
    for d in delegations:
        if d.agent != agent:
            continue
        for call in d.crm_calls:
            if call["tool"]:
                tools.add(call["tool"])
    return tools


def _target_keys_written(delegations: list[Delegation]) -> set[str]:
    """Target columns carried by any successful create_*_target writer call."""
    keys: set[str] = set()
    for d in delegations:
        if d.agent != "writer":
            continue
        for call in d.crm_calls:
            if not call["ok"] or "target" not in (call["tool"] or ""):
                continue
            for key, value in (call["tool_args"] or {}).items():
                if key.startswith("target") and value:
                    keys.add(key)
    return keys


def _reader_resolution(delegations: list[Delegation]) -> tuple[str | None, str]:
    """Parse the reader's structured resolution; return (resolution, raw_text)."""
    raw_parts: list[str] = []
    resolution: str | None = None
    for d in delegations:
        if d.agent != "reader":
            continue
        response = d.data.get("response") if isinstance(d.data, dict) else None
        if not isinstance(response, str):
            continue
        raw_parts.append(response)
        try:
            parsed = json.loads(response)
            if isinstance(parsed, dict) and parsed.get("resolution"):
                resolution = parsed["resolution"]
        except (ValueError, TypeError):
            continue
    return resolution, "\n".join(raw_parts)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    # Soft checks are reported but do NOT fail the scenario. Used for signals that
    # are architecturally noisy here — e.g. a name the user typed as a LOOKUP KEY
    # must reach the reader to search the CRM, so it is not a true "leak".
    soft: bool = False


@dataclass
class ScenarioResult:
    name: str
    message: str
    checks: list[Check] = field(default_factory=list)
    agent_order: list[str] = field(default_factory=list)
    response: str = ""
    error: str | None = None

    def add(self, name: str, passed: bool, detail: str = "", soft: bool = False) -> None:
        self.checks.append(Check(name, passed, detail, soft))

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks if not c.soft)


def _is_subsequence(needle: tuple[str, ...], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(item in it for item in needle)


def grade(
    scenario: OrchScenario,
    result: dict[str, Any],
    captured_prompts: list[str] | None = None,
) -> ScenarioResult:
    exp: OrchExpectations = scenario.expectations
    res = ScenarioResult(name=scenario.name, message=scenario.message)

    if result.get("type") == "interrupt":
        # Top-level interrupt: the writer paused for approval before the
        # orchestrator finished. Record it; the AGENTS/WRITE checks below still
        # read whatever delegations completed.
        res.response = "[interrupt — awaiting approval]"
    res.response = result.get("response") or res.response

    delegations = extract_delegations(result)
    order = [d.agent for d in delegations]
    res.agent_order = order

    # 1. AGENTS — required subsequence + reader-before-writer + forbids.
    if exp.agents:
        res.add(
            "agents",
            _is_subsequence(exp.agents, order),
            f"expected order ⊇ {exp.agents}, got {order}",
        )
        if "reader" in exp.agents and "writer" in exp.agents:
            r = order.index("reader") if "reader" in order else 99
            w = order.index("writer") if "writer" in order else -1
            res.add("reader_before_writer", r < w if w >= 0 else False,
                    f"order={order}")
    for forbidden in exp.forbid_agents:
        res.add(f"no_{forbidden}", forbidden not in order, f"order={order}")

    # 2. READ — resolution + composite/relational tool choice + entity match.
    resolution, reader_text = _reader_resolution(delegations)
    if exp.read_resolution:
        res.add("read_resolution", resolution == exp.read_resolution,
                f"expected {exp.read_resolution}, got {resolution!r}")
    if exp.read_entity:
        hay = (reader_text + "\n" + res.response).lower()
        res.add("read_entity", exp.read_entity.lower() in hay,
                f"{exp.read_entity!r} not found in reader output")
    if exp.read_tools:
        ran = _ok_tools_for(delegations, "reader") or _all_tools_for(delegations, "reader")
        for tool in exp.read_tools:
            res.add(f"read_tool:{tool}", tool in ran, f"reader ran {sorted(ran)}")

    # 3. WRITE — required tools all ran ok; any_of at least one; targets linked.
    writer_ok = _ok_tools_for(delegations, "writer")
    if exp.write_tools:
        for tool in exp.write_tools:
            res.add(f"write:{tool}", tool in writer_ok, f"writer ran ok {sorted(writer_ok)}")
    if exp.write_any_of:
        res.add("write_any_of", bool(set(exp.write_any_of) & writer_ok),
                f"need one of {exp.write_any_of}, writer ran ok {sorted(writer_ok)}")
    if exp.link_targets:
        written = _target_keys_written(delegations)
        for key in exp.link_targets:
            res.add(f"link:{key}", key in written, f"targets written {sorted(written)}")
    if exp.expect_interrupt:
        interrupted = result.get("type") == "interrupt" or any(d.is_interrupt for d in delegations)
        res.add("interrupt", interrupted, "no approval pause detected")
    if exp.no_write:
        wrote = _all_tools_for(delegations, "writer")
        res.add("no_write", not wrote, f"unexpected writer tools {sorted(wrote)}")

    # 4. RESPONSE.
    if exp.clarify:
        res.add("clarify", "?" in res.response, "expected a clarifying question")
    elif not exp.expect_interrupt:
        res.add("response_nonempty", bool(res.response.strip()), "empty response")
    for substring in exp.response_includes:
        res.add(f"says:{substring}", substring.lower() in res.response.lower(),
                f"{substring!r} missing from reply")

    # 5. PII SAFETY — every string that crossed the wire to the provider (after
    # masking) is scanned. A raw person/email/phone is a hard leak. The scenario's
    # planted, un-seeded PII must be absent verbatim.
    if captured_prompts is not None:
        leaks = scan_for_pii(captured_prompts)
        leak_summary = ", ".join(f"{leak['type']}:{leak['value']}" for leak in leaks[:5])
        # Soft: a name/email the user typed as a LOOKUP KEY must reach the reader
        # to search the CRM — that is not a true leak. The hard guarantee is
        # pii_planted_masked below (write-content PII that never needs to cross).
        res.add("pii_no_leak", not leaks,
                f"{len(leaks)} value(s) crossed (lookup keys ok): {leak_summary}" if leaks else "",
                soft=True)
        if exp.pii_must_mask:
            survived = find_planted_pii(captured_prompts, exp.pii_must_mask)
            res.add("pii_planted_masked", not survived,
                    f"planted PII reached provider: {survived}" if survived else "")

    return res


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def run_scenario(scenario: OrchScenario) -> ScenarioResult:
    session_id = f"orch-eval-{scenario.name}-{uuid.uuid4().hex[:8]}"
    captured: list[str] = []
    token = _PII_CAPTURE.set(captured)
    try:
        orch = Orchestrator(session_id=session_id)
        result = await orch.handle(scenario.message)
    except Exception as error:  # noqa: BLE001
        res = ScenarioResult(name=scenario.name, message=scenario.message)
        res.error = f"{type(error).__name__}: {error}"
        return res
    finally:
        _PII_CAPTURE.reset(token)
    return grade(scenario, result, captured_prompts=captured)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_GREEN, _RED, _DIM, _BOLD, _RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def _mark(passed: bool) -> str:
    return f"{_GREEN}PASS{_RESET}" if passed else f"{_RED}FAIL{_RESET}"


def print_report(results: list[ScenarioResult], verbose: bool) -> None:
    print("\n" + "=" * 78)
    print(f"  ORCHESTRATOR CRM EVAL — {len(results)} scenarios")
    print("=" * 78)
    passed = 0
    for r in sorted(results, key=lambda x: x.name):
        if r.passed:
            passed += 1
        status = _mark(r.passed)
        print(f"\n{status}  {_BOLD}{r.name}{_RESET}")
        print(f"      msg    : {r.message}")
        print(f"      agents : {' → '.join(r.agent_order) or '(none)'}")
        if r.error:
            print(f"      {_RED}error  : {r.error}{_RESET}")
        hard_failed = [c for c in r.checks if not c.passed and not c.soft]
        soft_failed = [c for c in r.checks if not c.passed and c.soft]
        if verbose:
            for c in r.checks:
                tag = "warn" if c.soft else _mark(c.passed)
                mark = f"{_DIM}{tag}{_RESET}" if (c.soft and not c.passed) else tag
                print(f"        {mark} {c.name} {_DIM}{c.detail}{_RESET}")
        else:
            for c in hard_failed:
                print(f"        {_RED}✗ {c.name}{_RESET} {_DIM}{c.detail}{_RESET}")
            for c in soft_failed:
                print(f"        {_DIM}~ {c.name} (soft) {c.detail}{_RESET}")
        if verbose and r.response:
            print(f"      reply  : {_DIM}{r.response[:300]}{_RESET}")

    print("\n" + "=" * 78)
    rate = (passed / len(results) * 100) if results else 0.0
    color = _GREEN if passed == len(results) else _RED
    print(f"  {color}{passed}/{len(results)} scenarios passed ({rate:.0f}%){_RESET}")
    print("=" * 78)

    # Gap rollup: which check kinds fail most — drives the next orchestrator fix.
    gaps: dict[str, int] = {}
    for r in results:
        for c in r.checks:
            if not c.passed and not c.soft:
                kind = c.name.split(":")[0]
                gaps[kind] = gaps.get(kind, 0) + 1
    if gaps:
        print("\n  Top failing check kinds (where the orchestrator leaks):")
        for kind, count in sorted(gaps.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>3}  {kind}")


def to_json(results: list[ScenarioResult]) -> dict[str, Any]:
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "scenarios": [
            {
                "name": r.name,
                "passed": r.passed,
                "agent_order": r.agent_order,
                "error": r.error,
                "response": r.response,
                "checks": [
                    {"name": c.name, "passed": c.passed, "detail": c.detail, "soft": c.soft}
                    for c in r.checks
                ],
            }
            for r in results
        ],
    }


def _configure_logging(quiet: bool) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    level = logging.WARNING if quiet else logging.INFO
    logging.getLogger("agent").setLevel(level)
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio", "langsmith"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=None, help="run a single scenario by name")
    parser.add_argument("--scenarios", default=None, help="comma-separated subset")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("--json", default=None, help="write full results as JSON")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--verbose", action="store_true", help="show every check + reply")
    parser.add_argument("--quiet", action="store_true", help="suppress INFO logs")
    args = parser.parse_args()

    if args.list:
        for s in SCENARIOS.values():
            print(f"  {s.name:<28} {s.exercises}")
        return

    if args.scenario:
        names = [args.scenario]
    elif args.scenarios:
        names = [n.strip() for n in args.scenarios.split(",") if n.strip()]
    else:
        names = list(SCENARIOS.keys())

    _configure_logging(args.quiet)
    configure_tracing(enabled=False)

    scenarios = [get(n) for n in names]
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    results: list[ScenarioResult] = []

    async def run_one(scenario: OrchScenario) -> None:
        async with semaphore:
            print(f"{_DIM}  running {scenario.name} …{_RESET}", flush=True)
            results.append(await run_scenario(scenario))

    undo_capture = _install_openai_capture()
    try:
        await asyncio.gather(*(run_one(s) for s in scenarios))
    finally:
        undo_capture()

    print_report(results, verbose=args.verbose or len(names) == 1)

    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_json(results), indent=2))
        print(f"\n  wrote {path}")

    # Non-zero exit if any hard failure, so CI / loops can gate on it.
    if any(not r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
