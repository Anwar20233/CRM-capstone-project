"""Composite metric + GEPA feedback for the synthesis agent.

Deterministic (no LLM judge): a good briefing names every contact, references the
key risks/concerns/competitors the data supports, mentions the risk score when
present, stays tight (1–3 paragraphs), and leaks no record ids or masking handles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_WEIGHTS = {
    "non_empty": 0.15,
    "names_present": 0.25,
    "must_mention": 0.25,
    "risk_mentioned": 0.10,
    "length_ok": 0.10,
    "no_id_leak": 0.15,
}

# crm_<uuid>, person001/shadow handles — must never appear in a human briefing.
_ID_LEAK_RE = re.compile(r"\b(crm_[\w-]+|shadow_[\w-]+|person\d{3,})\b", re.IGNORECASE)
_WORD_MIN, _WORD_MAX = 20, 350


@dataclass
class ScoreBreakdown:
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    applicable: dict[str, bool] = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)


def score_briefing(briefing: str, gold: dict[str, Any], *, error: str | None = None):
    signals: dict[str, float] = {}
    applies: dict[str, bool] = {}
    notes: list[str] = []
    lowered = briefing.lower()

    applies["non_empty"] = True
    signals["non_empty"] = 1.0 if briefing.strip() and not error else 0.0
    if error:
        notes.append(f"Rollout errored: {error}")

    names = gold.get("contact_names", [])
    if names:
        applies["names_present"] = True
        missing = [n for n in names if n.lower() not in lowered]
        signals["names_present"] = 1.0 - len(missing) / len(names)
        if missing:
            notes.append(f"Did not reference contact(s) by name: {missing}. Always name the people on the deal.")
    else:
        applies["names_present"] = False

    must = [k.lower() for k in gold.get("must_mention", [])]
    if must:
        applies["must_mention"] = True
        missing = [k for k in must if k not in lowered]
        signals["must_mention"] = 1.0 - len(missing) / len(must)
        if missing:
            notes.append(f"Omitted key signal(s): {missing}. The briefing must surface risks, concerns, competitors, and deadlines.")
    else:
        applies["must_mention"] = False

    if gold.get("expect_risk"):
        applies["risk_mentioned"] = True
        ok = "risk" in lowered
        signals["risk_mentioned"] = 1.0 if ok else 0.0
        if not ok:
            notes.append("A risk score was provided but the briefing never puts it in context.")
    else:
        applies["risk_mentioned"] = False

    applies["length_ok"] = True
    words = len(briefing.split())
    signals["length_ok"] = 1.0 if _WORD_MIN <= words <= _WORD_MAX else 0.0
    if words > _WORD_MAX:
        notes.append(f"Too long ({words} words) — keep it to a skimmable 1–3 paragraphs.")
    elif words and words < _WORD_MIN:
        notes.append(f"Too sparse ({words} words) — cover the deal, contacts, risks, and last activity.")

    applies["no_id_leak"] = True
    leaks = _ID_LEAK_RE.findall(briefing)
    signals["no_id_leak"] = 1.0 if not leaks else 0.0
    if leaks:
        notes.append(f"Leaked raw ids/handles {sorted(set(leaks))} — reference people by NAME, never ids.")

    total = sum(_WEIGHTS[n] for n, ok in applies.items() if ok)
    score = sum(_WEIGHTS[n] * signals[n] for n, ok in applies.items() if ok) / total if total else 0.0
    return ScoreBreakdown(round(score, 4), signals, applies, notes)


def metric_with_feedback(example, prediction, trace=None, pred_name=None, pred_trace=None):
    import dspy

    gold = example.gold if hasattr(example, "gold") else example["gold"]
    breakdown = score_briefing(getattr(prediction, "briefing", "") or "", gold,
                               error=getattr(prediction, "error", None))
    header = f"Score {breakdown.score:.2f}. Briefing for: {getattr(example, 'category', '?')}."
    lines = [header]
    if breakdown.feedback:
        lines.extend(f"- {n}" for n in breakdown.feedback)
    else:
        lines.append("- Briefing was complete, tight, and id-free. Keep this behaviour.")
    return dspy.Prediction(score=breakdown.score, feedback="\n".join(lines))
