"""System Capability Scorecard — a client-facing report across the whole agentic CRM.

The per-scenario harnesses (``orchestrator_testcase_eval.py`` and
``followup_eval.py``) answer "did THIS scenario pass". A client doesn't want 31
pass/fail rows — they want to know what the SYSTEM can reliably do. This tool
reframes the raw graded checks from BOTH agents into five plain-English
capabilities and reports, per capability, the share of scenarios that
demonstrated it:

  1. Completes the request        — did what the user/email actually asked.
  2. Uses the right tools         — picked the correct specialist agents + CRM
                                    tools and called them properly.
  3. Handles problems safely      — when data was missing / ambiguous / invalid,
                                    it stopped and asked or refused, instead of
                                    guessing or fabricating.
  4. Protects personal data       — masked names / emails / phones before they
                                    reached any AI model, and never exposed them.
  5. Stays grounded & accurate    — acted on real records with the right ids and
                                    produced a factual, reviewable result.

It consumes the JSON each harness already emits (``--json``), so it never re-runs
the agents — point it at one or both files. Scoring is at the SCENARIO level: a
scenario "demonstrates" a capability when every hard check that tests that
capability passed. (Soft checks are style signals — reported as advisories, never
counted against the score.) The headline is the equal-weight average across the
five capabilities, plus the share of scenarios that were correct on every
capability they exercised.

Run from packages/twenty-ai-service::

    .venv/bin/python scripts/system_scorecard.py --orchestrator out/tc_eval_c2.json --followup out/eval.json
    .venv/bin/python scripts/system_scorecard.py --orchestrator out/tc_eval_c2.json --md out/scorecard.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# The rubric — five client-facing capabilities.
# ---------------------------------------------------------------------------

CAPABILITIES: list[tuple[str, str, str]] = [
    ("task_completion", "Completes the request",
     "Carries out the action asked for, or answers the question correctly."),
    ("tool_use", "Uses the right tools",
     "Selects the correct specialist agents and CRM tools and calls them properly."),
    ("safe_handling", "Handles problems safely",
     "When data is missing, ambiguous, or invalid it stops and asks or refuses — "
     "it never guesses or fabricates a record."),
    ("data_privacy", "Protects personal data",
     "Masks names, emails, and phone numbers before they reach any AI model, "
     "and never exposes them in its replies."),
    ("grounding", "Stays grounded & accurate",
     "Acts on real records with the correct identifiers and returns a factual, "
     "reviewable result — no hallucinated data."),
]
CAP_LABEL = {cid: label for cid, label, _ in CAPABILITIES}
CAP_ORDER = [cid for cid, _, _ in CAPABILITIES]


# ---------------------------------------------------------------------------
# Mapping each harness's raw checks onto the rubric.
# ---------------------------------------------------------------------------


def orchestrator_capability(dim: str, name: str) -> str | None:
    """Map one orchestrator check (dim + name) to a capability, or None to skip."""
    if dim == "WORKFLOW":
        if name in ("write_completed", "no_mutation"):
            return "task_completion"
        if name in ("no_silent_success", "halted_with_question"):
            return "safe_handling"
    if dim == "AGENTS":
        if name.startswith("no_"):           # a forbidden agent stayed out
            return "safe_handling"
        return "tool_use"                    # agent_order, reader_before_writer
    if dim == "TOOLS":
        if name.startswith("link:"):         # the note/task was actually linked
            return "task_completion"
        if name.startswith("id_first:"):     # acted on a real id, not a name
            return "grounding"
        return "tool_use"                    # tool:<name>
    if dim == "OUTPUT":
        if name == "clarifying_question":
            return "safe_handling"
        if name.startswith("says:"):         # the reply carried the right fact
            return "grounding"
        return "task_completion"             # non_empty
    # PII is NOT a per-check vote — it's measured as a leak rate over AI calls
    # (see Scenario.llm_units / pii_leaks), so the privacy score reflects reality
    # (e.g. 1 leak in 692 calls = 99.9%) instead of "the scenario failed".
    return None


def followup_capability(name: str) -> str | None:
    """Map one follow-up-agent check name to a capability, or None to skip."""
    mapping = {
        # Did it carry the work to a finished, usable result?
        "pipeline_ok": "task_completion",
        "draft": "task_completion",
        "calendar": "task_completion",
        "calendar_soft": "task_completion",
        # Did it follow the right process / fire the right tools?
        "path": "tool_use",
        "tool_calls": "tool_use",
        # Did it stop cleanly on an unknown / bad input?
        "halts_cleanly": "safe_handling",
        "halt_path": "safe_handling",
        # Was its JUDGMENT correct and grounded? (action label, urgency, risk
        # band, and a real persisted artifact) — these are accuracy, NOT whether
        # the task ran. Conflating them with completion is what made the numbers
        # incoherent (high tool-use, low "completion").
        "action_type": "grounding",
        "urgency": "grounding",
        "risk_band": "grounding",
        "grounded": "grounding",
    }
    return mapping.get(name)


# ---------------------------------------------------------------------------
# Normalisation — turn either JSON shape into per-(scenario, capability) facts.
# ---------------------------------------------------------------------------


@dataclass
class CapVote:
    """One scenario's evidence for one capability."""

    hard_total: int = 0
    hard_passed: int = 0
    soft_total: int = 0
    soft_passed: int = 0

    def record(self, passed: bool, soft: bool) -> None:
        if soft:
            self.soft_total += 1
            self.soft_passed += int(passed)
        else:
            self.hard_total += 1
            self.hard_passed += int(passed)

    @property
    def tested(self) -> bool:
        return self.hard_total > 0

    @property
    def demonstrated(self) -> bool:
        # A scenario demonstrates a capability only if every hard check passed.
        return self.hard_total > 0 and self.hard_passed == self.hard_total


@dataclass
class Scenario:
    agent: str  # "Chat orchestrator" | "Follow-up agent"
    name: str
    votes: dict[str, CapVote] = field(default_factory=dict)
    error: str | None = None
    # Privacy is measured as a RATE, not pass/fail: how many post-mask messages
    # crossed to the LLM, and how many carried a raw personal value. Masking
    # 691/692 messages clean reads as 99.9%, not "1 scenario failed".
    llm_units: int = 0
    pii_leaks: int = 0

    def vote(self, cap: str) -> CapVote:
        return self.votes.setdefault(cap, CapVote())


def load_orchestrator(path: pathlib.Path) -> list[Scenario]:
    data = json.loads(path.read_text())
    out: list[Scenario] = []
    for case in data.get("cases", []):
        if case.get("skipped"):
            continue
        sc = Scenario(agent="Chat orchestrator", name=case.get("key", "?"),
                      error=case.get("error"))
        for ch in case.get("checks", []):
            cap = orchestrator_capability(ch.get("dim", ""), ch.get("name", ""))
            if cap:
                sc.vote(cap).record(bool(ch.get("passed")), bool(ch.get("soft")))
        sc.llm_units = case.get("pii_scanned", 0)
        sc.pii_leaks = case.get("pii_leaks", 0) + case.get("planted_leaks", 0)
        out.append(sc)
    return out


def load_followup(path: pathlib.Path) -> list[Scenario]:
    data = json.loads(path.read_text())
    out: list[Scenario] = []
    for s in data.get("scenarios", []):
        sc = Scenario(agent="Follow-up agent", name=s.get("name", "?"),
                      error=s.get("error"))
        for ch in s.get("checks", []):
            cap = followup_capability(ch.get("name", ""))
            if cap:
                sc.vote(cap).record(bool(ch.get("passed")), bool(ch.get("soft")))
        # Privacy as a rate: raw values that crossed, over the LLM calls made.
        sc.llm_units = s.get("llm_calls", 0)
        sc.pii_leaks = len(s.get("pii_leaks", [])) + len(s.get("planted_leaks", []))
        out.append(sc)
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class CapResult:
    cid: str
    pct: float
    tested: bool
    primary: str        # client-facing count, e.g. "29/31 scenarios"
    advisory: int = 0   # scenarios with a soft-check warning


def capability_results(scenarios: list[Scenario]) -> dict[str, CapResult]:
    """Score every capability as a check-level pass RATE; privacy as a leak rate.

    Check-level (not all-or-nothing per scenario) so a capability with many
    sub-checks isn't structurally penalised against one with few — that mismatch
    is what made "uses the right tools" (2 checks) read far higher than
    "completes the request" (5 checks) for the same agent.
    """
    passed = {c: 0 for c in CAP_ORDER}   # hard checks passed
    total = {c: 0 for c in CAP_ORDER}    # hard checks tested
    advisory = {c: 0 for c in CAP_ORDER}  # scenarios with a soft-check warning
    for sc in scenarios:
        for cap, vote in sc.votes.items():
            passed[cap] += vote.hard_passed
            total[cap] += vote.hard_total
            if vote.soft_total and vote.soft_passed < vote.soft_total:
                advisory[cap] += 1

    results: dict[str, CapResult] = {}
    for cid in CAP_ORDER:
        if cid == "data_privacy":
            continue
        t = total[cid]
        pct = (passed[cid] / t * 100) if t else 0.0
        results[cid] = CapResult(cid, pct, t > 0,
                                 f"{passed[cid]}/{t} checks", advisory[cid])

    # -- privacy as a leak RATE over AI calls (the honest framing) --
    units = sum(sc.llm_units for sc in scenarios)
    leaks = sum(sc.pii_leaks for sc in scenarios)
    clean = max(units - leaks, 0)
    pct = (clean / units * 100) if units else 0.0
    note = f"  ({leaks} exposed in {units} AI calls)" if leaks else ""
    results["data_privacy"] = CapResult(
        "data_privacy", pct, units > 0,
        f"{clean}/{units} AI calls with no exposed personal data" + note)
    return results


def fully_correct(scenarios: list[Scenario]) -> tuple[int, int]:
    """Scenarios correct on EVERY capability they exercised (vote-based caps)."""
    total = sum(1 for s in scenarios if any(v.tested for v in s.votes.values()))
    good = sum(1 for s in scenarios
               if any(v.tested for v in s.votes.values())
               and all(v.demonstrated for v in s.votes.values() if v.tested))
    return good, total


def overall_pct(results: dict[str, CapResult]) -> float:
    measured = [r.pct for r in results.values() if r.tested]
    return sum(measured) / len(measured) if measured else 0.0


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_G, _Y, _R, _DIM, _B, _RST = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")


def _color(pct: float) -> str:
    return _G if pct >= 90 else (_Y if pct >= 75 else _R)


def _bar(pct: float, width: int = 24) -> str:
    return "█" * round(pct / 100 * width)


def _pct_str(cid: str, pct: float) -> str:
    # Privacy is a near-100 rate — one decimal so a single leak reads as 99.9%,
    # not a contradictory "100%". The vote-based caps stay whole numbers.
    if cid == "data_privacy":
        shown = min(pct, 99.9) if pct < 100 else 100.0
        return f"{shown:.1f}%"
    return f"{pct:.0f}%"


def print_report(scenarios: list[Scenario]) -> None:
    agents = sorted({s.agent for s in scenarios})
    results = capability_results(scenarios)
    good, total = fully_correct(scenarios)
    overall = overall_pct(results)

    print("\n" + "=" * 72)
    print(f"  {_B}SYSTEM CAPABILITY SCORECARD{_RST}")
    print(f"  {_DIM}agentic CRM — {', '.join(agents)} — {total} scenarios{_RST}")
    print("=" * 72)

    for cid, label, desc in CAPABILITIES:
        r = results[cid]
        if not r.tested:
            continue
        c = _color(r.pct)
        warn = f"  {_DIM}({r.advisory} advisory){_RST}" if r.advisory else ""
        print(f"\n  {_B}{label}{_RST}")
        print(f"    {c}{_pct_str(cid, r.pct):>6}{_RST}  {c}{_bar(r.pct)}{_RST}  "
              f"{_DIM}{r.primary}{_RST}{warn}")
        print(f"    {_DIM}{desc}{_RST}")

    print("\n" + "-" * 72)
    oc = _color(overall)
    print(f"  {_B}Overall system reliability{_RST}   {oc}{overall:.0f}%{_RST}  "
          f"{_DIM}(equal-weight across capabilities){_RST}")
    fc = _color(good / total * 100 if total else 0)
    print(f"  {_B}Fully-correct scenarios{_RST}      {fc}{good}/{total}{_RST}  "
          f"{_DIM}(correct on every capability they exercise){_RST}")

    # Per-agent split so the client sees each component's strength.
    if len(agents) > 1:
        print("\n  By component:")
        for agent in agents:
            sub = [s for s in scenarios if s.agent == agent]
            ov = overall_pct(capability_results(sub))
            g, t = fully_correct(sub)
            print(f"    {agent:<20} {_color(ov)}{ov:3.0f}%{_RST}  "
                  f"{_DIM}({g}/{t} fully correct){_RST}")
    print("=" * 72)


def to_markdown(scenarios: list[Scenario]) -> str:
    agents = sorted({s.agent for s in scenarios})
    results = capability_results(scenarios)
    good, total = fully_correct(scenarios)
    overall = overall_pct(results)

    lines = ["# System Capability Scorecard",
             "",
             f"_Agentic CRM — {', '.join(agents)} — evaluated on {total} real "
             f"end-to-end scenarios._",
             "",
             f"**Overall system reliability: {overall:.0f}%**  ",
             f"**Fully-correct scenarios: {good}/{total}**",
             "",
             "| Capability | Score | Measured on | What it means |",
             "|---|---:|---|---|"]
    for cid, label, desc in CAPABILITIES:
        r = results[cid]
        if not r.tested:
            continue
        lines.append(f"| **{label}** | {_pct_str(cid, r.pct)} | {r.primary} | {desc} |")
    if len(agents) > 1:
        lines += ["", "## By component", "",
                  "| Component | Reliability | Fully correct |", "|---|---:|---:|"]
        for agent in agents:
            sub = [s for s in scenarios if s.agent == agent]
            ov = overall_pct(capability_results(sub))
            g, t = fully_correct(sub)
            lines.append(f"| {agent} | {ov:.0f}% | {g}/{t} |")
    lines += ["", "---",
              "_Capabilities 1–4 are scored at the scenario level (a scenario "
              "counts only when every hard check testing it passed; soft checks "
              "are advisories that never reduce the score). Data privacy is a "
              "leak rate measured over every AI call made._"]
    return "\n".join(lines) + "\n"


def to_json(scenarios: list[Scenario]) -> dict:
    results = capability_results(scenarios)
    good, total = fully_correct(scenarios)
    return {
        "overall_reliability_pct": round(overall_pct(results), 1),
        "fully_correct": {"passed": good, "total": total},
        "capabilities": {
            cid: {"label": CAP_LABEL[cid], "pct": round(results[cid].pct, 1),
                  "measured_on": results[cid].primary, "advisories": results[cid].advisory}
            for cid in CAP_ORDER if results[cid].tested
        },
        "by_component": {
            agent: round(overall_pct(capability_results(
                [s for s in scenarios if s.agent == agent])), 1)
            for agent in sorted({s.agent for s in scenarios})
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestrator", default=None, help="orchestrator eval JSON")
    parser.add_argument("--followup", default=None, help="follow-up eval JSON")
    parser.add_argument("--md", default=None, help="write a client-ready markdown report")
    parser.add_argument("--json", default=None, help="write the scorecard as JSON")
    args = parser.parse_args()

    if not args.orchestrator and not args.followup:
        parser.error("pass --orchestrator and/or --followup with an eval JSON path")

    scenarios: list[Scenario] = []
    if args.orchestrator:
        scenarios += load_orchestrator(pathlib.Path(args.orchestrator))
    if args.followup:
        scenarios += load_followup(pathlib.Path(args.followup))

    print_report(scenarios)

    if args.md:
        path = pathlib.Path(args.md)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_markdown(scenarios))
        print(f"\n  wrote {path}")
    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_json(scenarios), indent=2))
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
