"""Composite metric + GEPA feedback for the Next-Step planner.

Scores one planning rollout against its gold label on the same dimensions the
production eval grades (``scripts/followup_eval.py``): the *orchestrator tool* of
the headline (most-urgent) action, its *urgency band*, whether every action is
*grounded* in evidence, and *no-action* correctness for purely informational
emails. Returns a ``dspy.Prediction(score, feedback)`` — the natural-language
feedback is what GEPA reflects on to rewrite the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Only *applicable* signals are normalised into the score.
_WEIGHTS = {
    "valid_schema": 0.15,
    "tool_match": 0.35,
    "urgency_match": 0.25,
    "grounded": 0.15,
    "no_action": 0.10,
}

_PLACEHOLDER_EVIDENCE = {"no specific evidence cited"}


def _band(priority: int | None) -> str:
    if priority is None:
        return "unknown"
    if priority <= 2:
        return "high"
    if priority == 3:
        return "medium"
    return "low"


def _headline(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most-urgent action (lowest priority number)."""
    scored = [a for a in actions if a.get("priority") is not None]
    if not scored:
        return actions[0] if actions else None
    return min(scored, key=lambda a: a["priority"])


def _is_no_action(actions: list[dict[str, Any]]) -> bool:
    if len(actions) != 1:
        return False
    only = actions[0]
    return (only.get("action_type") == "no_action"
            or only.get("orchestrator_tool") == "log_activity")


@dataclass
class ScoreBreakdown:
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    applicable: dict[str, bool] = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)


def score_actions(
    actions: list[dict[str, Any]],
    gold: dict[str, Any],
    *,
    error: str | None = None,
) -> ScoreBreakdown:
    signals: dict[str, float] = {}
    applies: dict[str, bool] = {}
    notes: list[str] = []

    expect_no_action = bool(gold.get("expect_no_action"))
    gold_tools = set(gold.get("tools", []))
    gold_urgency = set(gold.get("urgency", []))

    # 1. valid_schema -----------------------------------------------------
    applies["valid_schema"] = True
    valid = bool(actions) and len(actions) <= 5 and not error
    signals["valid_schema"] = 1.0 if valid else 0.0
    if error:
        notes.append(f"Rollout errored: {error}")
    elif not actions:
        notes.append("Returned no actions — always return 1–5 actions (use no_action when nothing is needed).")
    elif len(actions) > 5:
        notes.append(f"Returned {len(actions)} actions — never more than 5.")

    headline = _headline(actions)

    # 5. no_action --------------------------------------------------------
    applies["no_action"] = True
    got_no_action = _is_no_action(actions)
    if expect_no_action:
        signals["no_action"] = 1.0 if got_no_action else 0.0
        if not got_no_action:
            notes.append(
                "This email is purely informational ('no reply needed' / FYI / OOO) "
                "— return EXACTLY ONE action with action_type 'no_action' and "
                "orchestrator_tool 'log_activity'. Do not draft a reply or book a meeting."
            )
    else:
        signals["no_action"] = 0.0 if got_no_action else 1.0
        if got_no_action:
            notes.append(
                "Returned a no_action/log_activity response for an email that needs "
                "follow-up — read the trigger again and take a concrete next step."
            )

    # 2. tool_match (headline action's orchestrator tool) -----------------
    if headline is not None and not expect_no_action:
        applies["tool_match"] = True
        tool = headline.get("orchestrator_tool")
        hit = tool in gold_tools
        signals["tool_match"] = 1.0 if hit else 0.0
        if not hit:
            notes.append(
                f"Headline action used orchestrator_tool '{tool}'; expected one of "
                f"{sorted(gold_tools)}. Match the action to what the email actually asks for."
            )
    elif expect_no_action:
        applies["tool_match"] = True
        signals["tool_match"] = 1.0 if (headline and headline.get("orchestrator_tool") == "log_activity") else 0.0
    else:
        applies["tool_match"] = False

    # 3. urgency_match (headline action's priority band) ------------------
    if headline is not None:
        applies["urgency_match"] = True
        band = _band(headline.get("priority"))
        hit = band in gold_urgency
        signals["urgency_match"] = 1.0 if hit else 0.0
        if not hit:
            notes.append(
                f"Headline urgency was '{band}' (priority {headline.get('priority')}); "
                f"expected {sorted(gold_urgency)}. Priority is ABSOLUTE urgency, not a "
                "ranking: 1–2 only for active risk / hard deadline within days, 3 for "
                "normal progress, 4–5 for routine/positive-momentum touches."
            )
    else:
        applies["urgency_match"] = False

    # 4. grounded (every action cites real evidence) ----------------------
    if actions:
        applies["grounded"] = True
        ungrounded = [
            a for a in actions
            if not a.get("evidence")
            or all(str(e).strip().lower() in _PLACEHOLDER_EVIDENCE for e in a["evidence"])
        ]
        signals["grounded"] = 1.0 if not ungrounded else max(0.0, 1.0 - len(ungrounded) / len(actions))
        if ungrounded:
            notes.append(
                f"{len(ungrounded)} action(s) cite no concrete evidence — every action "
                "must cite specific deal facts (timeline items, engagement metrics, "
                "contacts, BANT signals)."
            )
    else:
        applies["grounded"] = False

    total_weight = sum(_WEIGHTS[name] for name, ok in applies.items() if ok)
    score = (
        sum(_WEIGHTS[name] * signals[name] for name, ok in applies.items() if ok) / total_weight
        if total_weight else 0.0
    )
    return ScoreBreakdown(round(score, 4), signals, applies, notes)


def metric_with_feedback(example, prediction, trace=None, pred_name=None, pred_trace=None):
    """GEPA feedback metric. Returns ``dspy.Prediction(score, feedback)``."""
    import dspy

    gold = example.gold if hasattr(example, "gold") else example["gold"]
    actions = getattr(prediction, "actions", []) or []
    error = getattr(prediction, "error", None)

    breakdown = score_actions(actions, gold, error=error)

    header = (
        f"Score {breakdown.score:.2f}. Situation: {getattr(example, 'category', '?')}. "
        f"Expected headline tool ∈ {sorted(set(gold.get('tools', [])))}, "
        f"urgency ∈ {sorted(set(gold.get('urgency', [])))}"
        + (", and NO follow-up action (informational email)." if gold.get("expect_no_action") else ".")
    )
    lines = [header]
    if breakdown.feedback:
        lines.extend(f"- {note}" for note in breakdown.feedback)
    else:
        lines.append("- Plan matched the target on every dimension. Keep this behaviour.")

    return dspy.Prediction(score=breakdown.score, feedback="\n".join(lines))
