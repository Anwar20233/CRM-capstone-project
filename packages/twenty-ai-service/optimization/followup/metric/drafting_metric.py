"""Composite metric + GEPA feedback for the Drafting agent.

Deterministic (no LLM judge) so rollouts stay fast and free: scores a generated
draft on validity, personalization, required sign-off, placeholder hygiene,
length, and required content keywords from the gold label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_WEIGHTS = {
    "valid": 0.20,
    "personalized": 0.20,
    "signoff": 0.15,
    "no_placeholder": 0.15,
    "length": 0.10,
    "must_include": 0.20,
}

# Bracket placeholders that are allowed (the sign-off explicitly uses them).
_ALLOWED_PLACEHOLDERS = {"[your name]", "[insert name]"}
_PLACEHOLDER_RE = re.compile(r"\[[A-Za-z][A-Za-z0-9 _]+\]")
_WORD_MIN, _WORD_MAX = 80, 800


@dataclass
class ScoreBreakdown:
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    applicable: dict[str, bool] = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)


def score_draft(subject: str, body: str, gold: dict[str, Any], *, error: str | None = None):
    signals: dict[str, float] = {}
    applies: dict[str, bool] = {}
    notes: list[str] = []
    text = f"{subject}\n{body}"
    lowered = text.lower()

    applies["valid"] = True
    valid = bool(subject.strip()) and bool(body.strip()) and not error
    signals["valid"] = 1.0 if valid else 0.0
    if error:
        notes.append(f"Rollout errored: {error}")
    elif not subject.strip():
        notes.append("Empty subject — every email needs a subject line.")
    elif not body.strip():
        notes.append("Empty body.")

    applies["personalized"] = True
    company = (gold.get("company") or "").lower()
    contact = (gold.get("contact") or "").lower()
    hit = (company and company in lowered) or (contact and contact in lowered)
    signals["personalized"] = 1.0 if hit else 0.0
    if not hit:
        notes.append("Draft never names the contact or company — personalize from the deal context.")

    applies["signoff"] = True
    has_signoff = "best regards" in lowered and "beamdata" in lowered
    signals["signoff"] = 1.0 if has_signoff else 0.0
    if not has_signoff:
        notes.append('Missing the required sign-off block: "Best regards," / "[Your Name]" / "BeamData".')

    applies["no_placeholder"] = True
    bad = [m.group(0) for m in _PLACEHOLDER_RE.finditer(text)
           if m.group(0).lower() not in _ALLOWED_PLACEHOLDERS]
    signals["no_placeholder"] = 1.0 if not bad else 0.0
    if bad:
        notes.append(f"Left unresolved placeholder(s) {sorted(set(bad))} — fill them from context.")

    applies["length"] = True
    words = len(body.split())
    signals["length"] = 1.0 if _WORD_MIN <= words <= _WORD_MAX else 0.0
    if words < _WORD_MIN:
        notes.append(f"Body is thin ({words} words) — aim for substantive content (~{_WORD_MIN}-{_WORD_MAX}).")
    elif words > _WORD_MAX:
        notes.append(f"Body is bloated ({words} words) — tighten to under {_WORD_MAX}.")

    must = [k.lower() for k in gold.get("must_include", [])]
    if must:
        applies["must_include"] = True
        missing = [k for k in must if k not in lowered]
        signals["must_include"] = 1.0 - len(missing) / len(must)
        if missing:
            notes.append(f"Did not address required point(s): {missing}. The email must speak to what the buyer raised.")
    else:
        applies["must_include"] = False

    total = sum(_WEIGHTS[n] for n, ok in applies.items() if ok)
    score = sum(_WEIGHTS[n] * signals[n] for n, ok in applies.items() if ok) / total if total else 0.0
    return ScoreBreakdown(round(score, 4), signals, applies, notes)


def metric_with_feedback(example, prediction, trace=None, pred_name=None, pred_trace=None):
    import dspy

    gold = dict(example.gold if hasattr(example, "gold") else example["gold"])
    gold.setdefault("company", getattr(example, "company", ""))
    gold.setdefault("contact", getattr(example, "contact", ""))
    subject = getattr(prediction, "subject", "") or ""
    body = getattr(prediction, "body", "") or ""
    error = getattr(prediction, "error", None)

    breakdown = score_draft(subject, body, gold, error=error)
    header = f"Score {breakdown.score:.2f}. Draft type: {getattr(example, 'category', '?')}."
    lines = [header]
    lines.extend(f"- {n}" for n in breakdown.feedback) if breakdown.feedback else lines.append(
        "- Draft met every requirement. Keep this behaviour."
    )
    return dspy.Prediction(score=breakdown.score, feedback="\n".join(lines))
