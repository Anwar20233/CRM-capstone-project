"""Composite metric + GEPA feedback for the chat agent.

Deterministic (no LLM judge): scores tool selection — the required tools must be
called and forbidden tools must not — plus a light parsimony nudge. Captures the
behaviour the chat prompt is supposed to get right: read before editing, act only
when asked, and never take a side-effecting action for a read-only question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_WEIGHTS = {
    "required_tools": 0.50,
    "no_forbidden": 0.35,
    "parsimony": 0.15,
}


@dataclass
class ScoreBreakdown:
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    applicable: dict[str, bool] = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)


def score_chat(tools_called: list[str], gold: dict[str, Any], *, error: str | None = None):
    signals: dict[str, float] = {}
    applies: dict[str, bool] = {}
    notes: list[str] = []
    called = list(tools_called)
    called_set = set(called)

    required = gold.get("required_tools", [])
    forbidden = set(gold.get("forbidden_tools", []))

    applies["required_tools"] = True
    if error:
        notes.append(f"Rollout errored: {error}")
    if required:
        missing = [t for t in required if t not in called_set]
        signals["required_tools"] = 1.0 - len(missing) / len(required)
        if missing:
            notes.append(f"Did not call required tool(s): {missing}. (For edits, call list_pending_actions first, then revise_step; for a new draft use create_followup; for deal questions use get_opportunity_health.)")
    else:
        # No tool should be called (e.g. a clarifying question / refusal).
        signals["required_tools"] = 1.0 if not called else 0.0
        if called:
            notes.append(f"Called tools {called} when none were warranted — ask a clarifying question instead.")

    applies["no_forbidden"] = True
    hit = [t for t in called if t in forbidden]
    signals["no_forbidden"] = 1.0 if not hit else 0.0
    if hit:
        notes.append(f"Called forbidden tool(s) {hit}. Only act when the rep clearly asks; never take a side-effecting action for a read-only question.")

    applies["parsimony"] = True
    target = max(len(required), 0)
    if not called:
        signals["parsimony"] = 1.0
    elif target == 0:
        signals["parsimony"] = 0.0
    else:
        signals["parsimony"] = min(target / len(called), 1.0)
        if len(called) > target + 1:
            notes.append(f"Made {len(called)} tool calls for a {target}-tool task — don't loop or re-fetch.")

    total = sum(_WEIGHTS[n] for n, ok in applies.items() if ok)
    score = sum(_WEIGHTS[n] * signals[n] for n, ok in applies.items() if ok) / total if total else 0.0
    return ScoreBreakdown(round(score, 4), signals, applies, notes)


def metric_with_feedback(example, prediction, trace=None, pred_name=None, pred_trace=None):
    import dspy

    gold = example.gold if hasattr(example, "gold") else example["gold"]
    breakdown = score_chat(getattr(prediction, "tools_called", []) or [], gold,
                           error=getattr(prediction, "error", None))
    header = (
        f"Score {breakdown.score:.2f}. Intent: {getattr(example, 'category', '?')}. "
        f"Required {gold.get('required_tools', [])}, forbidden {gold.get('forbidden_tools', [])}."
    )
    lines = [header]
    if breakdown.feedback:
        lines.extend(f"- {n}" for n in breakdown.feedback)
    else:
        lines.append("- Selected exactly the right tools. Keep this behaviour.")
    return dspy.Prediction(score=breakdown.score, feedback="\n".join(lines))
