"""Deep tool-chain tracer for the MAIN orchestrator.

Where ``orchestrator_crm_eval.py`` grades pass/fail, this prints the FULL chain so
you can see exactly WHERE a request breaks: every delegation the orchestrator
makes (agent + the instruction it sent), and inside each, every ``execute_tool``
the reader/writer ran — tool name, the args, and whether the bridge returned
ok/error (+ the error). Supports MULTI-TURN sequences on ONE orchestrator so
cross-turn reference bugs ("update IT") reproduce faithfully.

Read-only by default — the built-in probes are all lookups, so running them does
NOT mutate the CRM. Add your own write turns only when you accept DB changes.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/orchestrator_trace.py                      # all probes
    .venv/bin/python scripts/orchestrator_trace.py --probe poc_xturn
    .venv/bin/python scripts/orchestrator_trace.py --turns "Who works at Notion?||What opportunity does Kevin Cho handle?||Tell me more about it"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)

from tracing import configure_tracing  # noqa: E402

configure_tracing(enabled=False)

from agent.orchestrator import Orchestrator  # noqa: E402
from scripts.orchestrator_crm_eval import extract_delegations  # noqa: E402

_DIM, _BOLD, _RED, _GREEN, _CYAN, _YEL, _RESET = (
    "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[36m", "\033[33m", "\033[0m",
)


# Read-only multi-turn probes that stress the exact failure shapes seen in the
# wild: relational opportunity-by-point-of-contact, and cross-turn pronouns.
PROBES: dict[str, list[str]] = {
    # The transcript bug, read-only: find the deal a person handles, then refer
    # to it with a pronoun the next turn (no write, so safe to run repeatedly).
    "poc_xturn": [
        "What opportunity does John Park handle?",
        "Tell me more about it.",
        "Who is the point of contact on it?",
    ],
    # Same relational lookup across several POCs — is it deterministic per person?
    "poc_each": [
        "What opportunity does John Park handle?",
        "What opportunity does Alex Rivera handle?",
        "What opportunity does Kevin Cho handle?",
        "What opportunity does Emma Larsen handle?",
        "What opportunity does Rachel Kim handle?",
    ],
    # Run the SAME lookup three times on one session — does it flip-flop
    # (worked-then-empty like the transcript)?
    "poc_repeat": [
        "What opportunity does John Park handle?",
        "What opportunity does John Park handle?",
        "What opportunity does John Park handle?",
    ],
    # Relational person<-company then cross-turn pronoun.
    "people_xturn": [
        "Who works at Notion?",
        "What's the job title of the first one?",
        "What deal is Kevin Cho the point of contact on?",
    ],
    # Resolve a deal, then ask about its fields across turns.
    "deal_fields_xturn": [
        "Show me the Airbnb deal.",
        "What stage is it in?",
        "Who is its point of contact and what's their email?",
    ],
}


def _short(value: object, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …"


def _render_turn(turn_index: int, message: str, result: dict) -> None:
    print(f"\n{_BOLD}{_CYAN}── turn {turn_index}: {message}{_RESET}")
    if result.get("type") == "interrupt":
        print(f"  {_YEL}[interrupt — write paused for approval]{_RESET}")

    delegations = extract_delegations(result)
    if not delegations:
        print(f"  {_DIM}(no delegations — orchestrator answered directly){_RESET}")
    for d in delegations:
        status = f"{_GREEN}ok{_RESET}" if d.ok else f"{_RED}FAIL{_RESET}"
        print(f"\n  {_BOLD}▶ delegate → {d.agent}{_RESET} [{status}]")
        print(f"      instruction: {_DIM}{_short(d.instruction, 260)}{_RESET}")
        crm = d.crm_calls
        if not crm:
            # Show the sub-agent's own answer when it ran no CRM tools (e.g. a
            # reader that returned 'none' or redirected without querying).
            resp = d.data.get("response") if isinstance(d.data, dict) else None
            print(f"      {_DIM}no execute_tool calls; sub-agent said: {_short(resp)}{_RESET}")
        for call in crm:
            mark = f"{_GREEN}✓{_RESET}" if call["ok"] else f"{_RED}✗{_RESET}"
            print(f"        {mark} {call['tool']}  {_DIM}{_short(call['tool_args'], 200)}{_RESET}")
            if not call["ok"]:
                # Surface the bridge error — this is usually the root cause.
                err = _find_error(d, call)
                if err:
                    print(f"            {_RED}error: {_short(err, 260)}{_RESET}")
    print(f"\n  {_BOLD}reply:{_RESET} {_short(result.get('response') or '', 400)}")


def _find_error(delegation, target_call) -> str | None:
    """Pull the error envelope for a failed execute_tool from the raw trace."""
    for tc in (delegation.data.get("tool_calls") or []):
        if tc.get("name") != "execute_tool":
            continue
        if (tc.get("args") or {}).get("tool") != target_call["tool"]:
            continue
        result = tc.get("result") or {}
        if isinstance(result, dict) and not result.get("ok"):
            return json.dumps(result.get("error") or result, default=str)
    return None


async def run_probe(name: str, turns: list[str]) -> None:
    print("\n" + "=" * 80)
    print(f"  PROBE: {name}  ({len(turns)} turn(s), one shared orchestrator)")
    print("=" * 80)
    orch = Orchestrator(session_id=f"trace-{name}")
    for i, message in enumerate(turns, start=1):
        try:
            result = await orch.handle(message)
        except Exception as error:  # noqa: BLE001
            print(f"\n  {_RED}turn {i} raised: {type(error).__name__}: {error}{_RESET}")
            continue
        _render_turn(i, message, result)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", default=None, help="run one named probe")
    parser.add_argument("--turns", default=None, help="custom turns, '||'-separated")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, turns in PROBES.items():
            print(f"  {name:<18} {len(turns)} turns: {turns[0]} …")
        return

    if args.turns:
        await run_probe("custom", [t.strip() for t in args.turns.split("||") if t.strip()])
        return

    names = [args.probe] if args.probe else list(PROBES.keys())
    for name in names:
        await run_probe(name, PROBES[name])


if __name__ == "__main__":
    asyncio.run(main())
