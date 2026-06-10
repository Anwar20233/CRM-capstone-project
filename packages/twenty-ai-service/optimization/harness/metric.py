"""Composite trajectory metric + GEPA feedback for the Writer agent.

Scores one rollout against its gold label on the signals the plan calls out:
outcome, action correctness, learn-before-execute order, **parsimony** (fewest
tool calls), argument validity, resolve_date usage, tier-3 confirmation
compliance, and response appropriateness.

Two entry points share one core:

- ``score_trajectory(trajectory, gold)`` -> ``ScoreBreakdown`` for reports.
- ``metric_with_feedback(example, prediction, ...)`` -> ``dspy.Prediction`` with
  ``score`` and a human-readable ``feedback`` string — the lever GEPA reflects on
  to rewrite the prompt.

The metric reads ``prediction``'s ``response`` + ``tool_calls`` (the meta-tool
trajectory), so the real action is at ``tool_calls[i]["args"]["tool"]`` when the
tool is ``execute_tool``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.tool_scope import classify_tool, Capability


# Weights per signal (only *applicable* signals are normalised into the score).
_WEIGHTS = {
    "outcome_match": 0.35,
    "action_correct": 0.20,
    "protocol_order": 0.10,
    "parsimony": 0.10,
    "arg_validity": 0.10,
    "resolve_date": 0.05,
    "confirmation": 0.05,
    "response": 0.05,
}

_ERROR_CODES_BAD = {"INVALID_ARGUMENTS", "UNRESOLVED_HANDLE", "OUT_OF_SCOPE"}

_REJECT_HINTS = (
    "can't read", "cannot read", "only handle", "only perform writes", "i'm a write",
    "write agent", "reader", "out of scope", "not able to search", "don't search",
    "do not search", "can't search", "cannot search", "unable to", "not something i",
)
_CLARIFY_HINTS = (
    "could you", "which ", "please provide", "need more", "what is the", "what's the",
    "can you specify", "more detail", "more information", "who ", "clarify", "?",
)


# ---------------------------------------------------------------------------
# Normalised trajectory view
# ---------------------------------------------------------------------------

@dataclass
class _Step:
    kind: str            # catalog | learn | execute | resolve_date | current_user | other
    action: str | None   # for execute: the inner tool; for learn: joined names
    capability: str | None
    ok: bool
    error_code: str | None
    has_token: bool


def _to_steps(tool_calls: list[dict[str, Any]]) -> list[_Step]:
    steps: list[_Step] = []
    for call in tool_calls:
        name = call.get("name", "")
        args = call.get("args") or {}
        result = call.get("result") or {}
        ok = bool(isinstance(result, dict) and result.get("ok"))
        error_code = None
        if isinstance(result, dict) and not ok:
            error_code = (result.get("error") or {}).get("code")

        if name == "execute_tool":
            inner = args.get("tool")
            steps.append(_Step(
                kind="execute", action=inner,
                capability=classify_tool(inner).value if inner else None,
                ok=ok, error_code=error_code,
                has_token=bool(args.get("confirmation_token")),
            ))
        elif name == "learn_tools":
            names = args.get("tool_names") or []
            cap = None
            for n in names:
                if classify_tool(n) == Capability.READ:
                    cap = "read"
                    break
            steps.append(_Step(kind="learn", action=",".join(names),
                               capability=cap, ok=ok, error_code=error_code, has_token=False))
        elif name == "get_tool_catalog":
            steps.append(_Step("catalog", None, None, ok, error_code, False))
        elif name == "resolve_date":
            steps.append(_Step("resolve_date", None, None, ok, error_code, False))
        elif name == "get_current_user":
            steps.append(_Step("current_user", None, None, ok, error_code, False))
        else:
            steps.append(_Step("other", name, None, ok, error_code, False))
    return steps


def _attempted_read(steps: list[_Step]) -> bool:
    for step in steps:
        if step.kind in ("execute", "learn") and step.capability == "read":
            return True
        if step.error_code == "OUT_OF_SCOPE":
            return True
    return False


def _executed_write(steps: list[_Step]) -> bool:
    return any(s.kind == "execute" and s.capability == "write" and s.ok for s in steps)


def _confirmation_required(steps: list[_Step]) -> bool:
    return any(s.kind == "execute" and s.error_code == "CONFIRMATION_REQUIRED" for s in steps)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class ScoreBreakdown:
    score: float
    signals: dict[str, float] = field(default_factory=dict)       # name -> sub-score
    applicable: dict[str, bool] = field(default_factory=dict)     # name -> applies?
    feedback: list[str] = field(default_factory=list)             # human-readable notes
    actual_outcome: str = "unknown"


def _classify_outcome(steps: list[_Step], response: str) -> str:
    if _executed_write(steps):
        return "executed"
    if _confirmation_required(steps):
        return "confirmation_required"
    text = (response or "").lower()
    if _attempted_read(steps) or any(h in text for h in _REJECT_HINTS):
        # If it also asks a clarifying question without scope-refusal, prefer clarify.
        if not _attempted_read(steps) and "?" in text and not any(
            h in text for h in _REJECT_HINTS[:6]
        ):
            return "clarification_needed"
        return "rejected_out_of_scope"
    if any(h in text for h in _CLARIFY_HINTS):
        return "clarification_needed"
    return "no_action"


def score_trajectory(
    response: str,
    tool_calls: list[dict[str, Any]],
    gold: dict[str, Any],
) -> ScoreBreakdown:
    steps = _to_steps(tool_calls)
    n_calls = len(steps)
    signals: dict[str, float] = {}
    applies: dict[str, bool] = {}
    notes: list[str] = []

    gold_outcome = gold["outcome"]
    gold_action = gold.get("primary_action")
    actual_outcome = _classify_outcome(steps, response)

    # 1. outcome_match -----------------------------------------------------
    applies["outcome_match"] = True
    signals["outcome_match"] = 1.0 if actual_outcome == gold_outcome else 0.0
    if signals["outcome_match"] < 1.0:
        notes.append(f"Outcome was '{actual_outcome}' but should be '{gold_outcome}'.")

    # 2. action_correct ----------------------------------------------------
    applies["action_correct"] = True
    if gold_action:
        hit = any(s.kind == "execute" and s.action == gold_action for s in steps)
        signals["action_correct"] = 1.0 if hit else 0.0
        if not hit:
            called = [s.action for s in steps if s.kind == "execute"]
            notes.append(
                f"Expected execute_tool(tool='{gold_action}'); "
                f"executed {called or 'nothing'}."
            )
    else:
        # reject/clarify: success == performed NO write.
        wrote = _executed_write(steps)
        signals["action_correct"] = 0.0 if wrote else 1.0
        if wrote:
            notes.append("Performed a write for a request that should be refused / clarified.")

    # 3. protocol_order: learn before execute -----------------------------
    execute_indices = [i for i, s in enumerate(steps) if s.kind == "execute"]
    if execute_indices:
        applies["protocol_order"] = True
        first_execute = execute_indices[0]
        learned_before = any(
            s.kind == "learn" and s.capability != "read" for s in steps[:first_execute]
        )
        signals["protocol_order"] = 1.0 if learned_before else 0.0
        if not learned_before:
            notes.append("Called execute_tool before learn_tools — always learn the schema first.")
    else:
        applies["protocol_order"] = False

    # 4. parsimony ---------------------------------------------------------
    applies["parsimony"] = True
    min_calls = gold.get("min_tool_calls", 0)
    if min_calls <= 0:
        signals["parsimony"] = 1.0 if n_calls == 0 else max(0.0, 1.0 - 0.34 * n_calls)
        if n_calls > 0:
            notes.append(f"Made {n_calls} tool call(s); optimal was 0 (refuse without calling tools).")
    elif n_calls == 0:
        signals["parsimony"] = 0.0
    else:
        signals["parsimony"] = min(min_calls / n_calls, 1.0)
        if n_calls > min_calls:
            notes.append(
                f"Made {n_calls} tool calls; optimal was {min_calls}. "
                "Skip get_tool_catalog when you already know the tool name; don't re-fetch."
            )

    # 5. arg_validity ------------------------------------------------------
    bad = [s for s in steps if s.error_code in _ERROR_CODES_BAD]
    if steps:
        applies["arg_validity"] = True
        signals["arg_validity"] = 1.0 if not bad else max(0.0, 1.0 - len(bad) / len(steps))
        for s in bad:
            notes.append(f"Tool error {s.error_code} on {s.kind}({s.action}) — fix arguments/scope.")
    else:
        applies["arg_validity"] = False

    # 6. resolve_date ------------------------------------------------------
    applies["resolve_date"] = True
    used_resolve = any(s.kind == "resolve_date" for s in steps)
    needs = bool(gold.get("needs_resolve_date"))
    signals["resolve_date"] = 1.0 if used_resolve == needs else 0.0
    if needs and not used_resolve:
        notes.append("Relative date present — call resolve_date before the write.")
    if used_resolve and not needs:
        notes.append("Called resolve_date with no relative date in the request.")

    # 7. confirmation ------------------------------------------------------
    if gold_outcome == "confirmation_required":
        applies["confirmation"] = True
        got = _confirmation_required(steps)
        # blind retry = a second execute of a tier-3 action without a token.
        tier3 = [s for s in steps if s.kind == "execute" and s.action == gold_action]
        blind_retry = len(tier3) > 1 and not any(s.has_token for s in tier3[1:])
        signals["confirmation"] = 1.0 if (got and not blind_retry) else 0.0
        if not got:
            notes.append("Tier-3 action should return CONFIRMATION_REQUIRED and surface a draft.")
        if blind_retry:
            notes.append("Re-tried a tier-3 write without a confirmation token — never blind-retry.")
    else:
        applies["confirmation"] = False

    # 8. response appropriateness -----------------------------------------
    if gold_outcome in ("rejected_out_of_scope", "clarification_needed", "confirmation_required"):
        applies["response"] = True
        text = (response or "").lower()
        if gold_outcome == "rejected_out_of_scope":
            good = bool(text) and any(h in text for h in _REJECT_HINTS)
        elif gold_outcome == "clarification_needed":
            good = "?" in text or any(h in text for h in _CLARIFY_HINTS)
        else:  # confirmation_required
            good = bool(text) and ("confirm" in text or "delete" in text or "are you sure" in text)
        signals["response"] = 1.0 if good else 0.0
        if not good:
            notes.append(f"Final reply doesn't match a '{gold_outcome}' response.")
    else:
        applies["response"] = False

    # -- weighted aggregate over applicable signals -----------------------
    total_weight = sum(_WEIGHTS[name] for name, ok in applies.items() if ok)
    score = (
        sum(_WEIGHTS[name] * signals[name] for name, ok in applies.items() if ok) / total_weight
        if total_weight
        else 0.0
    )

    return ScoreBreakdown(
        score=round(score, 4),
        signals=signals,
        applicable=applies,
        feedback=notes,
        actual_outcome=actual_outcome,
    )


# ---------------------------------------------------------------------------
# DSPy GEPA entry point
# ---------------------------------------------------------------------------

def metric_with_feedback(example, prediction, trace=None, pred_name=None, pred_trace=None):
    """GEPA feedback metric. Returns ``dspy.Prediction(score, feedback)``.

    ``example`` carries ``gold``/``request``/``category``; ``prediction`` carries
    ``response`` and ``tool_calls`` from the live rollout.
    """
    import dspy

    gold = example.gold if hasattr(example, "gold") else example["gold"]
    response = getattr(prediction, "response", "") or ""
    tool_calls = getattr(prediction, "tool_calls", []) or []
    run_error = getattr(prediction, "error", None)

    breakdown = score_trajectory(response, tool_calls, gold)

    header = (
        f"Score {breakdown.score:.2f}. Request type: "
        f"{getattr(example, 'category', '?')}. "
        f"Target outcome: {gold['outcome']}"
        + (f" via {gold['primary_action']}." if gold.get("primary_action") else ".")
    )
    lines = [header]
    if run_error:
        lines.append(f"Rollout raised: {run_error}")
    if breakdown.feedback:
        lines.extend(f"- {note}" for note in breakdown.feedback)
    else:
        lines.append("- Trajectory was optimal for this request. Keep this behaviour.")

    return dspy.Prediction(score=breakdown.score, feedback="\n".join(lines))
