#!/usr/bin/env python3
"""Interactive terminal harness for the CRM agents.

Send a message, watch the full workflow trace (model → LLM steps → each tool
call + result → final answer), then get a fresh prompt for the next message.

Usage (from packages/twenty-ai-service)::

    .venv/bin/python scripts/chat.py                 # Writer agent, env model
    .venv/bin/python scripts/chat.py --model gemma-free
    .venv/bin/python scripts/chat.py --agent reader

Type 'exit', 'quit', ':q', or Ctrl-D to leave. Ctrl-C aborts the current turn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

# Make `agent` importable regardless of where the script is launched from.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.tool_scope import READER_SCOPE  # noqa: E402
from agent.workers import BaseWorker, WriterWorker  # noqa: E402


# ── ANSI styling (auto-disabled when not a TTY) ────────────────────────────
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def cyan(t: str) -> str:
    return _c("36", t)


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def blue(t: str) -> str:
    return _c("34", t)


def _truncate(value: object, limit: int = 280) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + dim(" …(truncated)")


# ── Event printer — renders the live trace ─────────────────────────────────
def make_printer() -> "callable":
    def printer(event: dict) -> None:
        kind = event.get("type")
        if kind == "model":
            print(dim(f"   model: {event['model']}"))
        elif kind == "llm_call":
            print(dim(f"   ↻ step {event['step']}: asking the model…"))
        elif kind == "tool_call":
            args = _truncate(event.get("args") or {})
            print(f"   {cyan('🔧 ' + event['name'])}{dim('(')}{args}{dim(')')}")
        elif kind == "tool_result":
            result = event.get("result") or {}
            ok = isinstance(result, dict) and result.get("ok")
            if ok:
                data = _truncate(result.get("data"))
                print(f"      {green('✓ ok')} {dim(data)}")
            else:
                err = result.get("error", {}) if isinstance(result, dict) else {}
                code = err.get("code", "ERROR")
                message = err.get("message", "")
                if code == "CONFIRMATION_REQUIRED":
                    print(f"      {yellow('⏸ confirmation required')} "
                          f"{dim('token=' + str(err.get('confirmation_token')))}")
                    print(f"        {dim('draft: ' + _truncate(err.get('draft')))}")
                else:
                    print(f"      {red('✗ ' + code)} {dim(message)}")

    return printer


# ── Worker construction ────────────────────────────────────────────────────
def build_worker(agent: str, model: str | None, session_id: str):
    if agent == "writer":
        return WriterWorker(session_id=session_id, model=model)
    if agent == "reader":
        return BaseWorker(
            scope=READER_SCOPE,
            system_prompt=(
                "You are a CRM Read Agent for Twenty CRM. Resolve and return "
                "records. Use get_tool_catalog → learn_tools → execute_tool. "
                "Never guess tool names or argument shapes."
            ),
            session_id=session_id,
            model=model,
        )
    raise SystemExit(f"Unknown agent '{agent}' (use 'writer' or 'reader').")


def _placeholder(name: str) -> bool:
    value = os.environ.get(name, "")
    return (not value) or value.startswith("your-")


def preflight(worker, agent: str) -> None:
    print(bold(f"\n  Twenty CRM — {agent.capitalize()} Agent  "))
    print(dim("  " + "─" * 46))
    print(f"  tools  : {', '.join(worker.tool_names)}")
    print(f"  bridge : {os.environ.get('NODE_BRIDGE_BASE_URL', '(unset)')}")

    warnings = []
    if _placeholder("LLM_API_KEY"):
        warnings.append("LLM_API_KEY is a placeholder — the model call will fail.")
    if any(_placeholder(v) for v in ("TWENTY_WORKSPACE_ID", "TWENTY_USER_ID")):
        warnings.append("TWENTY identity vars are placeholders — bridge tool "
                        "calls will return errors (you'll still see the trace).")
    for w in warnings:
        print("  " + yellow("⚠ " + w))
    print(dim("\n  Type your message. 'exit'/'quit'/Ctrl-D to leave.\n"))


# ── REPL ────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive CRM agent harness")
    parser.add_argument("--agent", choices=["writer", "reader"], default="writer")
    parser.add_argument("--model", default=None, help="alias or OpenRouter slug")
    parser.add_argument("--session", default="cli-session")
    args = parser.parse_args()

    worker = build_worker(args.agent, args.model, args.session)
    printer = make_printer()
    preflight(worker, args.agent)

    turn = 0
    while True:
        try:
            message = input(blue("You ❯ ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not message:
            continue
        if message.lower() in {"exit", "quit", ":q"}:
            break

        turn += 1
        print(dim(f"  ── turn {turn} ─────────────────────────────────"))
        try:
            result = asyncio.run(worker.run(message, on_event=printer))
        except KeyboardInterrupt:
            print(yellow("\n  …turn aborted.\n"))
            continue
        except Exception as error:  # noqa: BLE001 — surface anything to the user
            print(red(f"\n  ✗ {type(error).__name__}: {error}\n"))
            continue

        print("\n" + bold("🤖 Agent ❯ ") + (result.get("response") or dim("(no text)")))
        print()

    print(dim("  bye 👋"))


if __name__ == "__main__":
    main()
