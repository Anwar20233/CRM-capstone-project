"""Measure tokens + latency for ONE orchestrator run and ONE follow-up run.

Patches the OpenAI SDK at the class level (sync Completions.create and async
AsyncCompletions.create) so it captures *every* LLM call regardless of path —
the raw LLMClient and LangChain's ChatOpenAI both go through these. Each call
records prompt/completion/total tokens (from response.usage) and wall-clock
latency, tagged by the active phase (orchestrator | followup).

Run from packages/twenty-ai-service (live services + seed data required):
    .venv/bin/python scripts/measure_one_run.py
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

# Keep LangSmith out of the picture (no credits) and quiet the noise.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ.setdefault("LANGSMITH_TRACING", "false")

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING)
for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio", "langsmith", "agent", "followup"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Per-call ledger
# ---------------------------------------------------------------------------

_PHASE: contextvars.ContextVar[str] = contextvars.ContextVar("phase", default="unattributed")


@dataclass
class Call:
    phase: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_s: float
    usage_present: bool


@dataclass
class PhaseStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    llm_latency_s: float = 0.0  # summed time inside create() (includes concurrency overlap)
    wall_s: float = 0.0  # end-to-end wall clock for the phase
    missing_usage: int = 0
    records: list[Call] = field(default_factory=list)

    def add(self, call: Call) -> None:
        self.calls += 1
        self.prompt_tokens += call.prompt_tokens
        self.completion_tokens += call.completion_tokens
        self.total_tokens += call.total_tokens
        self.llm_latency_s += call.latency_s
        if not call.usage_present:
            self.missing_usage += 1
        self.records.append(call)


LEDGER: dict[str, PhaseStats] = {}


def _record(resp: object, model: str, latency_s: float) -> None:
    usage = getattr(resp, "usage", None)
    pt = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    ct = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    tt = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
    if usage and not tt:
        tt = pt + ct
    phase = _PHASE.get()
    LEDGER.setdefault(phase, PhaseStats()).add(
        Call(phase, model, pt, ct, tt, latency_s, usage is not None)
    )


# ---------------------------------------------------------------------------
# SDK-level patch (catches raw client + LangChain ChatOpenAI)
# ---------------------------------------------------------------------------

def install_patches() -> None:
    from openai.resources.chat import completions as sync_mod

    orig_sync = sync_mod.Completions.create

    def sync_create(self, *args, **kwargs):
        # Ask for usage even on streamed calls, when the caller streams.
        if kwargs.get("stream") and "stream_options" not in kwargs:
            kwargs["stream_options"] = {"include_usage": True}
        start = time.perf_counter()
        resp = orig_sync(self, *args, **kwargs)
        _record(resp, kwargs.get("model", "?"), time.perf_counter() - start)
        return resp

    sync_mod.Completions.create = sync_create

    try:
        from openai.resources.chat import completions as async_mod

        orig_async = async_mod.AsyncCompletions.create

        async def async_create(self, *args, **kwargs):
            if kwargs.get("stream") and "stream_options" not in kwargs:
                kwargs["stream_options"] = {"include_usage": True}
            start = time.perf_counter()
            resp = await orig_async(self, *args, **kwargs)
            _record(resp, kwargs.get("model", "?"), time.perf_counter() - start)
            return resp

        async_mod.AsyncCompletions.create = async_create
    except Exception as exc:  # noqa: BLE001
        print(f"  (async patch skipped: {exc})")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

async def run_orchestrator() -> str:
    from scripts.orchestrator_test_cases import TEST_CASES
    from agent.orchestrator import Orchestrator

    case = TEST_CASES["composite_account_overview"]
    token = _PHASE.set("orchestrator")
    start = time.perf_counter()
    try:
        orch = Orchestrator(session_id=f"measure-{uuid.uuid4().hex[:8]}")
        for prior in case.prior_turns:
            await orch.handle(prior)
        result = await orch.handle(case.user_input)
    finally:
        LEDGER.setdefault("orchestrator", PhaseStats()).wall_s = time.perf_counter() - start
        _PHASE.reset(token)
    return f"[{case.id}] {case.user_input!r} -> {(result.get('response') or '')[:160]!r}"


async def run_followup() -> str:
    from scripts.followup_email_scenarios import get
    from followup.orchestrator import OrchestratorDeps, build_followup_graph
    from followup.store.repositories import Database

    scenario = get("meeting_request_with_specific_slots")
    db = await Database.connect()
    token = _PHASE.set("followup")
    start = time.perf_counter()
    try:
        deps = OrchestratorDeps.create(db)
        graph = build_followup_graph(deps)
        initial_state = {
            "entry_point": "email",
            "trigger": {
                "id": str(uuid.uuid4()),
                "sender_email": scenario.sender,
                "subject": scenario.subject,
                "body": scenario.body,
                "owner_user_id": os.environ.get("TWENTY_USER_ID"),
            },
            "workspace_id": os.environ.get("TWENTY_WORKSPACE_ID", ""),
            "run_id": str(uuid.uuid4()),
            "status": "running",
            "trace": [],
        }
        final: dict = {}
        async for chunk in graph.astream(initial_state, stream_mode="updates"):
            for _node, update in chunk.items():
                if isinstance(update, dict):
                    final.update(update)
    finally:
        LEDGER.setdefault("followup", PhaseStats()).wall_s = time.perf_counter() - start
        _PHASE.reset(token)
        try:
            await db.close()
        except Exception:  # noqa: BLE001
            pass
    return f"[{scenario.name}] status={final.get('status')} trace={' -> '.join(final.get('trace') or [])}"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt(stats: PhaseStats) -> str:
    avg = stats.llm_latency_s / stats.calls if stats.calls else 0.0
    miss = f"  ({stats.missing_usage} calls w/o usage)" if stats.missing_usage else ""
    return (
        f"    LLM calls        : {stats.calls}{miss}\n"
        f"    prompt tokens    : {stats.prompt_tokens:,}\n"
        f"    completion tokens: {stats.completion_tokens:,}\n"
        f"    TOTAL tokens     : {stats.total_tokens:,}\n"
        f"    wall-clock       : {stats.wall_s:.1f}s\n"
        f"    summed LLM time  : {stats.llm_latency_s:.1f}s  (avg {avg:.2f}s/call)"
    )


# gpt-4o-mini pricing (USD per 1M tokens), as of 2024/2025.
PRICE_IN = 0.15 / 1_000_000
PRICE_OUT = 0.60 / 1_000_000


def main() -> None:
    install_patches()

    print("\n" + "=" * 70)
    print("  RUN 1/2 — Chat orchestrator (composite_account_overview)")
    print("=" * 70)
    summary_orch = asyncio.run(run_orchestrator())
    print("  " + summary_orch)

    print("\n" + "=" * 70)
    print("  RUN 2/2 — Follow-up agent (meeting_request_with_specific_slots email)")
    print("=" * 70)
    summary_fu = asyncio.run(run_followup())
    print("  " + summary_fu)

    print("\n" + "=" * 70)
    print("  RESULTS")
    print("=" * 70)
    grand = PhaseStats()
    for phase in ("orchestrator", "followup"):
        stats = LEDGER.get(phase)
        if not stats:
            print(f"\n  {phase.upper()}: no calls recorded")
            continue
        cost = stats.prompt_tokens * PRICE_IN + stats.completion_tokens * PRICE_OUT
        print(f"\n  {phase.upper()}  (model: gpt-4o-mini)")
        print(_fmt(stats))
        print(f"    est. cost        : ${cost:.5f}")
        grand.calls += stats.calls
        grand.prompt_tokens += stats.prompt_tokens
        grand.completion_tokens += stats.completion_tokens
        grand.total_tokens += stats.total_tokens
        grand.wall_s += stats.wall_s
        grand.missing_usage += stats.missing_usage

    gcost = grand.prompt_tokens * PRICE_IN + grand.completion_tokens * PRICE_OUT
    print("\n  " + "-" * 66)
    print(f"  COMBINED: {grand.calls} calls, {grand.total_tokens:,} tokens, "
          f"{grand.wall_s:.1f}s wall, est. ${gcost:.5f}")
    if grand.missing_usage:
        print(f"  NOTE: {grand.missing_usage} call(s) returned no usage object "
              f"(token counts for those are 0).")
    print("=" * 70)


if __name__ == "__main__":
    main()
