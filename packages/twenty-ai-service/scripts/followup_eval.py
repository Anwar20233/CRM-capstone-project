"""Client-grade evaluation harness for the Follow-Up agent.

Runs EVERY scenario in ``followup_email_scenarios.py`` through the REAL
orchestrator graph (no fakes) and grades the agent the way you'd defend it to a
customer — not "did it answer", but "did it reach the right answer the right way,
and did it never leak a person's data to the cloud LLM".

Five graded dimensions per scenario:

  1. PATH        — the run took the correct node trace (extract→…→create_pending,
                   or a clean halt for an unknown sender). Proves control flow.
  2. TOOL CALLS  — the prep tasks the plan required actually ran (draft_email,
                   check_calendar, …). Proves the agent *did the work*, not just
                   narrated it.
  3. OUTPUT      — headline action_type ∈ the acceptable set, urgency ∈ its set,
                   a draft exists when one should, a meeting got a calendar check
                   when the email demanded one, risk landed in the right band.
  4. PII SAFETY  — every string that crossed the wire to ANY LLM (after masking)
                   is scanned with the project's Presidio pipeline; a single raw
                   person/email/phone is a hard failure. The scenario's PLANTED,
                   un-seeded PII (new names, raw addresses) must be absent verbatim.
  5. GROUNDED    — the persisted pending action carries the plan + reasoning the
                   rep will review (sanity that the run produced a real artifact).

PII capture is COMPLETE: the two chat-model factories every boundary uses
(``followup.profile.llm.build_chat_llm`` for extract/synthesize and
``agent.llm_client._resolve_chat_model`` for the next-step + drafting subagents)
are wrapped so a capture callback rides on every model — so we see the extraction,
synthesis, planner AND drafter prompts, post-masking, exactly as the LLM saw them.

Prerequisites (same as followup_orchestrator_e2e.py):
  * Twenty backend on :3000 (NODE_BRIDGE_BASE_URL),
  * the twenty-ai-service .env (LLM_* + TWENTY_*),
  * seeded data (seed_data.py) so senders resolve,
  * the GLiNER/Presidio models for PII discovery+scoring (HF_HOME set). Without
    them new-name masking AND the leak scan are unreliable — the harness refuses
    to certify a PII pass and says so loudly.

Scenarios run CONCURRENTLY (``--concurrency``, default 5) for speed; per-scenario
PII capture stays isolated via a contextvar, so the grading is identical to a
sequential run. Use ``--concurrency 1`` to force the old sequential behavior.

Run from packages/twenty-ai-service:
    HF_HOME=$HOME/.cache/huggingface .venv/bin/python scripts/followup_eval.py
    .venv/bin/python scripts/followup_eval.py --scenarios meeting_request_with_specific_slots,pricing_above_approved_budget
    .venv/bin/python scripts/followup_eval.py --concurrency 8 --json out/eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import os
import pathlib
import re
import sys
import uuid
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from scripts.followup_email_scenarios import (  # noqa: E402
    EXPECTATIONS,
    SCENARIOS,
    ScenarioExpectations,
    get,
)

# ===========================================================================
# PII boundary capture — wrap the two chat-model factories so a capture
# callback rides on EVERY LLM the pipeline builds (after masking).
# ===========================================================================

# A masked handle the pipeline emits on purpose — never a leak. Matches person001,
# email002, phone003, company004, … (3+ digits, see followup/profile/masking.py).
HANDLE_RE = re.compile(r"\b(?:person|company|email|phone|location|url)\d{3,}\b", re.I)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")

# The masked-PII labels we MUST never see in clear text at a boundary. Company /
# location are intentionally left visible (business context, not PII) — masking
# them would corrupt the deal facts, so we do not flag them as leaks.
_LEAK_LABELS = {"person", "email address", "phone number"}

# Labels the masking layer deliberately leaves visible (business context, not
# PII). The NER tagger sometimes mislabels one of these AS a person (e.g. tags
# "Stripe" or "Figma" person instead of company); we use the scenario's own
# business-entity vocabulary — aggregated across ALL its boundary prompts — to
# correct that misclassification so a company name is never counted as a person
# leak. This mirrors the masker's design; it does not relax the verbatim
# planted-PII check, which still catches any real un-seeded person name.
_BUSINESS_LABELS = {"company", "competitor", "location", "job title", "product"}

# Vendor/customer/competitor names that recur across the scenario set. These are
# unambiguously companies (business context, never PII), but the NER tagger is
# inconsistent — in some prompts it labels them ``company`` and in others
# ``person``. Seeding the allowlist with them guarantees a company name is never
# counted as a person leak regardless of that per-prompt inconsistency.
_KNOWN_BUSINESS_NAMES = frozenset({
    "stripe", "figma", "airbnb", "datadog", "notion", "beamdata", "segment",
    "airtable", "grafana", "grafana cloud", "pagerduty", "okta", "saastr",
    "daemonset", "soc 2", "msa", "dpa", "tam", "saml", "cmk", "roi", "bant",
})

# Minimum NER confidence for a person span to count as a candidate leak — drops
# the low-confidence noise the tagger emits on fragments and single letters.
_PERSON_SCORE_FLOOR = 0.6

# Leading articles/determiners that turn a role phrase into a false "person".
_ROLE_PREFIXES = ("the ", "a ", "an ", "our ", "their ", "your ", "my ")

# Common English words the NER tagger mislabels as a person when they appear
# capitalized at a sentence start ("Need a number…", "Tone was off"). A real name
# is never one of these; dropping a single-token person that is one of them
# removes scanner noise without hiding any actual name (planted names are checked
# verbatim and separately). Kept deliberately small and word-list-shaped.
_PERSON_NOISE_WORDS = frozenset({
    "need", "needs", "tone", "thanks", "thank", "regards", "best", "cheers",
    "hi", "hello", "hey", "dear", "team", "all", "sincerely", "please", "note",
    "update", "subject", "re", "fwd", "ok", "okay", "yes", "no", "sounds",
})


def _norm(value: str) -> str:
    return (value or "").strip().casefold()


def _is_junk_person(value: str) -> bool:
    """A person span too degenerate to be a real name leak (NER noise)."""
    stripped = (value or "").strip()
    if len(stripped) < 2:  # single letters like a "K" signature
        return True
    if not any(ch.isalpha() for ch in stripped):  # punctuation / digits only
        return True
    lowered = stripped.casefold()
    # A bare role phrase ("the Director of Operations") is a title, not a name.
    if any(lowered.startswith(prefix) for prefix in _ROLE_PREFIXES):
        return True
    # A single common English word mislabeled as a name (sentence-start noise).
    return " " not in stripped and lowered in _PERSON_NOISE_WORDS


class _CapturedPrompts:
    """Per-scenario accumulator of every message sent to any LLM this run.

    One instance is bound to the running scenario via ``_CURRENT_CAPTURE`` (a
    contextvar), so scenarios that run CONCURRENTLY each collect only their own
    boundary prompts — the capture handler resolves the active accumulator at
    fire time from the async context it runs in.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_from_langchain(self, messages: Any) -> None:
        # messages is list[list[BaseMessage]] (one batch of turns).
        try:
            for batch in messages:
                for message in batch:
                    content = getattr(message, "content", message)
                    if isinstance(content, str) and content.strip():
                        self.messages.append(content)
                    elif isinstance(content, list):
                        for part in content:
                            text = part.get("text") if isinstance(part, dict) else None
                            if text:
                                self.messages.append(text)
        except Exception:  # noqa: BLE001 — capture must never break a run
            pass


# The accumulator for the scenario running in the current async context. Set per
# scenario in run_scenario; read by the capture handler. None outside a scenario.
_CURRENT_CAPTURE: contextvars.ContextVar[_CapturedPrompts | None] = (
    contextvars.ContextVar("followup_eval_capture", default=None)
)


def _install_prompt_capture() -> Callable[[], None]:
    """Patch both chat-model factories to attach a capture callback.

    Returns an undo() that restores the originals. The callback's
    ``on_chat_model_start`` fires with the exact, already-masked messages — so the
    scan sees what the provider sees, across all four boundaries. The handler is
    stateless and routes each capture to the scenario active in its async context,
    so concurrent scenarios never cross-contaminate.
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class _CaptureHandler(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: ANN001
            capture = _CURRENT_CAPTURE.get()
            if capture is not None:
                capture.add_from_langchain(messages)

    def _with_capture(model: Any) -> Any:
        existing = list(getattr(model, "callbacks", None) or [])
        existing.append(_CaptureHandler())
        try:
            model.callbacks = existing
        except Exception:  # noqa: BLE001 — some models freeze attrs; best effort
            pass
        return model

    import agent.llm_client as llm_client
    import followup.profile.llm as profile_llm

    orig_resolve = llm_client._resolve_chat_model
    orig_build = profile_llm.build_chat_llm

    def patched_resolve(model: str | None = None):
        return _with_capture(orig_resolve(model))

    def patched_build(model=None, temperature=0.0):  # noqa: ANN001
        return _with_capture(orig_build(model, temperature))

    llm_client._resolve_chat_model = patched_resolve
    profile_llm.build_chat_llm = patched_build

    def undo() -> None:
        llm_client._resolve_chat_model = orig_resolve
        profile_llm.build_chat_llm = orig_build

    return undo


def scan_for_pii(texts: list[str]) -> list[dict[str, Any]]:
    """Return every raw person/email/phone found in boundary text (handles excluded).

    Uses the project's Presidio pipeline when available, plus a regex backstop for
    emails/phones that NER occasionally misses.
    """
    leaks: list[dict[str, Any]] = []
    try:
        from pipelines import extract as presidio_extract
    except Exception:  # noqa: BLE001
        presidio_extract = None

    # Pass 1 — build the scenario's business-entity vocabulary (company /
    # competitor / location / role / product) across ALL boundary prompts, so a
    # company the tagger mislabels as a person in one prompt is still recognized
    # as business context and not counted as a person leak.
    entities_per_text: list[list[dict[str, Any]]] = []
    business_names: set[str] = set(_KNOWN_BUSINESS_NAMES)
    for text in texts:
        if not text or presidio_extract is None:
            entities_per_text.append([])
            continue
        try:
            found = list(presidio_extract(text))
        except Exception:  # noqa: BLE001
            found = []
        entities_per_text.append(found)
        for entity in found:
            if entity.get("label") in _BUSINESS_LABELS:
                business_names.add(_norm(entity.get("text", "")))

    # Pass 2 — collect the genuine person/email/phone leaks.
    seen_values: set[str] = set()
    for text, found in zip(texts, entities_per_text):
        if not text:
            continue
        for entity in found:
            label, value = entity.get("label"), entity.get("text", "")
            if label not in _LEAK_LABELS:
                continue
            stripped = value.strip()
            if HANDLE_RE.fullmatch(stripped) or _norm(value) in seen_values:
                continue
            # A masked-handle email (``person004@airbnb.com``) has the PERSON
            # hidden — only the company domain remains (business context, not a
            # leak). The whole-string fullmatch above misses these because the
            # domain trails the handle, so check for a handle anywhere in it.
            if label == "email address" and HANDLE_RE.search(stripped):
                continue
            if label == "person":
                if _norm(value) in business_names or _is_junk_person(value):
                    continue
                if (entity.get("score") or 0.0) < _PERSON_SCORE_FLOOR:
                    continue
            seen_values.add(_norm(value))
            leaks.append({"type": label, "value": value, "score": entity.get("score")})
        for email in EMAIL_RE.findall(text):
            if not HANDLE_RE.search(email) and _norm(email) not in seen_values:
                seen_values.add(_norm(email))
                leaks.append({"type": "email address (regex)", "value": email, "score": 1.0})
        for phone in PHONE_RE.findall(text):
            if _norm(phone) not in seen_values:
                seen_values.add(_norm(phone))
                leaks.append({"type": "phone number (regex)", "value": phone, "score": 1.0})
    return leaks


def find_planted_pii(texts: list[str], planted: tuple[str, ...]) -> list[str]:
    """Which of the scenario's planted, un-seeded PII tokens survived to a boundary."""
    haystack = "\n".join(texts).lower()
    return [token for token in planted if token.lower() in haystack]


# ===========================================================================
# Observed-run extraction — turn final graph state into graded facts
# ===========================================================================

# Reverse of orchestrator/routing.STEP_PREP: which final-state signal proves a
# given prep task actually ran.
def observed_tasks(state: dict[str, Any]) -> set[str]:
    tasks: set[str] = set()
    if state.get("draft") is not None:
        tasks.add("draft_email")
    if state.get("calendar") is not None:
        tasks.add("check_calendar")
    for key in (state.get("task_results") or {}):
        if key in ("write_note", "create_task"):
            tasks.add(key)
    return tasks


def risk_band(state: dict[str, Any]) -> str | None:
    risk = state.get("risk_assessment")
    score = getattr(risk, "risk_score", None)
    if score is None:
        return None
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def headline_action(state: dict[str, Any]) -> str | None:
    plan = state.get("plan")
    return getattr(plan, "headline_action", None)


def plan_urgency(state: dict[str, Any]) -> str | None:
    plan = state.get("plan")
    steps = getattr(plan, "steps", None)
    return steps[0].priority if steps else None


# ===========================================================================
# Scoring
# ===========================================================================


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    soft: bool = False  # a soft check that failed is a warning, not a failure


@dataclass
class ScenarioResult:
    name: str
    checks: list[Check] = field(default_factory=list)
    pii_leaks: list[dict[str, Any]] = field(default_factory=list)
    planted_leaks: list[str] = field(default_factory=list)
    llm_calls: int = 0
    seconds: float = 0.0
    error: str | None = None

    def add(self, name: str, passed: bool, detail: str = "", soft: bool = False) -> None:
        self.checks.append(Check(name, passed, detail, soft))

    @property
    def hard_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.soft]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.hard_checks) and not self.pii_leaks and not self.planted_leaks


def grade(name: str, exp: ScenarioExpectations, state: dict[str, Any]) -> ScenarioResult:
    result = ScenarioResult(name=name)
    status = state.get("status")
    trace = tuple(state.get("trace") or [])

    # --- Dimension 1: PATH ------------------------------------------------
    if exp.expect_pipeline_ok:
        result.add("pipeline_ok", status == "completed",
                   f"status={status} error={state.get('error')}")
        result.add("path", trace == exp.expected_path,
                   f"trace={'→'.join(trace) or '∅'}")
    else:
        # The expected behavior IS a halt: status failed, trace is the halt path.
        result.add("halts_cleanly", status == "failed", f"status={status}")
        result.add("halt_path", trace == exp.expected_path, f"trace={'→'.join(trace) or '∅'}")
        # A halt produces no plan/draft/PII boundary work to grade — stop here.
        return result

    # --- Dimension 2: TOOL CALLS ------------------------------------------
    observed = observed_tasks(state)
    if exp.required_tasks:
        missing = exp.required_tasks - observed
        result.add("tool_calls", not missing,
                   f"ran={sorted(observed)} required={sorted(exp.required_tasks)}"
                   + (f" MISSING={sorted(missing)}" if missing else ""))

    # --- Dimension 3: OUTPUT ----------------------------------------------
    action = headline_action(state)
    if exp.action_types:
        result.add("action_type", action in exp.action_types,
                   f"got={action} ∈ {sorted(exp.action_types)}")
    urgency = plan_urgency(state)
    if exp.urgency:
        result.add("urgency", urgency in exp.urgency,
                   f"got={urgency} ∈ {sorted(exp.urgency)}")
    if exp.expect_draft is not None:
        has_draft = state.get("draft") is not None
        result.add("draft", has_draft == exp.expect_draft,
                   f"draft_present={has_draft} expected={exp.expect_draft}")
    cal = state.get("calendar")
    has_slots = bool(getattr(cal, "available_slots", None)) if cal is not None else False
    if exp.require_calendar:
        result.add("calendar", cal is not None and has_slots,
                   f"calendar_present={cal is not None} slots={has_slots}")
    elif exp.expect_calendar:
        # Soft: booking is preferred but a draft-only reply is acceptable.
        result.add("calendar_soft", cal is not None,
                   f"calendar_present={cal is not None}", soft=True)
    band = risk_band(state)
    if exp.risk_bands:
        result.add("risk_band", band in exp.risk_bands,
                   f"got={band} ∈ {sorted(exp.risk_bands)}", soft=True)

    # --- Dimension 5: GROUNDED --------------------------------------------
    pending = state.get("pending_action")
    result.add("grounded", bool(pending and pending.get("reasoning")),
               f"pending={'yes' if pending else 'no'}")
    return result


# ===========================================================================
# Run one scenario through the real graph
# ===========================================================================


async def run_scenario(
    graph, name: str, workspace_id: str
) -> tuple[dict[str, Any], float, list[str]]:
    scenario = get(name)
    initial_state = {
        "entry_point": "email",
        "trigger": {
            "id": str(uuid.uuid4()),
            "sender_email": scenario.sender,
            "subject": scenario.subject,
            "body": scenario.body,
            "owner_user_id": os.environ.get("TWENTY_USER_ID"),
        },
        "workspace_id": workspace_id,
        "run_id": str(uuid.uuid4()),
        "status": "running",
        "trace": [],
    }
    # Bind a fresh accumulator to THIS scenario's async context so its boundary
    # prompts never mix with a concurrently-running scenario's.
    capture = _CapturedPrompts()
    _CURRENT_CAPTURE.set(capture)
    start = perf_counter()
    try:
        final_state = await graph.ainvoke(initial_state)
    except Exception as exc:  # noqa: BLE001 — a crash is itself a failed run
        final_state = {"status": "failed", "error": f"graph crashed: {exc}", "trace": []}
    return final_state, perf_counter() - start, list(capture.messages)


# ===========================================================================
# Report
# ===========================================================================


def _mark(passed: bool, soft: bool = False) -> str:
    if passed:
        return "✓"
    return "~" if soft else "✗"


def print_report(results: list[ScenarioResult], models_loaded: bool) -> None:
    print("\n" + "=" * 90)
    print("  FOLLOW-UP AGENT — EVALUATION REPORT")
    print("=" * 90)

    # Per-scenario lines.
    for r in results:
        hard_pass = sum(c.passed for c in r.hard_checks)
        hard_total = len(r.hard_checks)
        pii_state = "CLEAN" if not (r.pii_leaks or r.planted_leaks) else f"LEAK×{len(r.pii_leaks)+len(r.planted_leaks)}"
        verdict = "PASS" if r.passed else "FAIL"
        print(f"\n  [{verdict:4}] {r.name}   ({hard_pass}/{hard_total} checks, "
              f"PII {pii_state}, {r.llm_calls} llm calls, {r.seconds:.1f}s)")
        for c in r.checks:
            print(f"        {_mark(c.passed, c.soft)} {c.name:<14} {c.detail}")
        if r.pii_leaks:
            shown = ", ".join(f"[{l['type']}] {l['value']}" for l in r.pii_leaks[:6])
            print(f"        ✗ PII_LEAK      {shown}")
        if r.planted_leaks:
            print(f"        ✗ PLANTED_PII   un-masked: {', '.join(r.planted_leaks)}")
        if r.error:
            print(f"        ! error         {r.error}")

    # Aggregate — the numbers you put in front of a client.
    total = len(results)
    passed = sum(r.passed for r in results)
    total_leaks = sum(len(r.pii_leaks) + len(r.planted_leaks) for r in results)
    total_calls = sum(r.llm_calls for r in results)
    clean_runs = sum(1 for r in results if not (r.pii_leaks or r.planted_leaks))

    # Per-dimension pass rates (hard checks only).
    dim_pass: dict[str, list[bool]] = {}
    for r in results:
        for c in r.hard_checks:
            dim_pass.setdefault(c.name, []).append(c.passed)

    print("\n" + "=" * 90)
    print("  AGGREGATE")
    print("=" * 90)
    print(f"  scenarios passed (all gates)   : {passed}/{total}  ({passed/total:.0%})")
    print(f"  PII: clean runs                : {clean_runs}/{total}  ({clean_runs/total:.0%})")
    print(f"  PII: total leaks               : {total_leaks}  across {total_calls} LLM calls")
    print(f"  PII NER models loaded          : {models_loaded}"
          + ("" if models_loaded else "   ⚠ PII PASS NOT CERTIFIED — load GLiNER/Presidio"))
    print("\n  per-check pass rate:")
    for dim, results_list in sorted(dim_pass.items()):
        ok = sum(results_list)
        print(f"    {dim:<16} {ok:>2}/{len(results_list):<2}  ({ok/len(results_list):.0%})")
    print("=" * 90)
    headline = "✅ AGENT CERTIFIED" if (passed == total and total_leaks == 0 and models_loaded) \
        else "⚠ NOT FULLY CERTIFIED — see failures above"
    print(f"  {headline}\n")


def to_json(results: list[ScenarioResult], models_loaded: bool) -> dict[str, Any]:
    return {
        "models_loaded": models_loaded,
        "summary": {
            "total": len(results),
            "passed": sum(r.passed for r in results),
            "pii_leaks": sum(len(r.pii_leaks) + len(r.planted_leaks) for r in results),
            "llm_calls": sum(r.llm_calls for r in results),
        },
        "scenarios": [
            {
                "name": r.name,
                "passed": r.passed,
                "seconds": round(r.seconds, 2),
                "llm_calls": r.llm_calls,
                "checks": [
                    {"name": c.name, "passed": c.passed, "soft": c.soft, "detail": c.detail}
                    for c in r.checks
                ],
                "pii_leaks": r.pii_leaks,
                "planted_leaks": r.planted_leaks,
                "error": r.error,
            }
            for r in results
        ],
    }


# ===========================================================================
# Main
# ===========================================================================


def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio", "followup", "agent"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default=None,
                        help="comma-separated names (default: all)")
    parser.add_argument("--json", default=None, help="write the full result as JSON to this path")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="how many scenarios to run in parallel (default: 5; 1 = sequential)")
    parser.add_argument("--trace", action="store_true",
                        help="keep LangSmith tracing on (default: off — it adds latency and "
                             "rate-limit noise that destabilizes concurrent runs)")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for scenario in SCENARIOS.values():
            print(f"  {scenario.name:<38} {scenario.sender}")
        return

    # Tracing off by default: under --concurrency the LangSmith exporter's 429s
    # back up and slow the reader's retries, which is what flips a resolvable
    # sender into a transient halt. Must be set before any LLM client is built.
    if not args.trace:
        for var in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_TRACING"):
            os.environ[var] = "false"

    names = ([s.strip() for s in args.scenarios.split(",") if s.strip()]
             if args.scenarios else sorted(SCENARIOS))
    _configure_logging()
    workspace_id = os.environ.get("TWENTY_WORKSPACE_ID", "")

    from followup.profile.masking import ensure_models_loaded
    from followup.orchestrator import OrchestratorDeps, build_followup_graph
    from followup.store.repositories import Database

    models_loaded = ensure_models_loaded()
    if not models_loaded:
        print("⚠  PII NER models are NOT loaded — new-name masking + leak scoring "
              "are unreliable. Set HF_HOME and ensure GLiNER/Presidio are installed.",
              file=sys.stderr)

    undo_capture = _install_prompt_capture()
    db = await Database.connect()
    results_by_name: dict[str, ScenarioResult] = {}
    try:
        deps = OrchestratorDeps.create(db)
        graph = build_followup_graph(deps)

        # Scenarios run CONCURRENTLY (bounded by --concurrency). Per-scenario PII
        # capture is isolated by a contextvar, so every captured prompt is still
        # attributable to exactly one scenario despite the parallelism. A
        # semaphore caps in-flight runs to stay under provider rate limits.
        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def run_one(name: str) -> None:
            exp = EXPECTATIONS[name]
            async with semaphore:
                state, seconds, captured = await run_scenario(graph, name, workspace_id)
            result = grade(name, exp, state)
            result.seconds = seconds
            result.llm_calls = len(captured)
            result.error = state.get("error") if state.get("status") == "failed" and exp.expect_pipeline_ok else None
            # PII scoring runs over EVERY boundary prompt captured this scenario.
            result.pii_leaks = scan_for_pii(captured)
            result.planted_leaks = find_planted_pii(captured, exp.pii_must_mask)
            results_by_name[name] = result
            print(f"  · ran {name:<38} {'PASS' if result.passed else 'FAIL'} ({seconds:.1f}s)")

        await asyncio.gather(*(run_one(name) for name in names))
    finally:
        undo_capture()
        await db.close()

    # Preserve the requested order in the report regardless of completion order.
    results: list[ScenarioResult] = [results_by_name[name] for name in names if name in results_by_name]

    print_report(results, models_loaded)
    if args.json:
        out_path = pathlib.Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(to_json(results, models_loaded), indent=2, default=str))
        print(f"  wrote {out_path}")

    # Exit non-zero if anything failed — usable as a CI gate.
    if not all(r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
