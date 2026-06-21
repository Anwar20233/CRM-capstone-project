"""Composite metric + GEPA feedback for the extraction agent.

Deterministic (no LLM judge): scores deal selection, fact recall (against the
gold fact types/keywords the text supports), correct abstention on ambiguous
multi-deal cases, and new-person discovery — the behaviours the extraction prompt
is supposed to get right.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_WEIGHTS = {
    "valid_json": 0.15,
    "deal_choice": 0.30,
    "fact_recall": 0.35,
    "unknown_person": 0.20,
}


@dataclass
class ScoreBreakdown:
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    applicable: dict[str, bool] = field(default_factory=dict)
    feedback: list[str] = field(default_factory=list)


def _fact_blob(facts: list[dict[str, Any]]) -> str:
    parts = []
    for f in facts:
        parts.append(str(f.get("fact_type", "")))
        parts.append(str(f.get("fact_value", "")))
    return " ".join(parts).lower()


def score_extraction(prediction, gold: dict[str, Any], *, error: str | None = None):
    signals: dict[str, float] = {}
    applies: dict[str, bool] = {}
    notes: list[str] = []

    facts = getattr(prediction, "facts", []) or []
    opportunity_id = getattr(prediction, "opportunity_id", None)
    unknown = getattr(prediction, "unknown_persons", []) or []

    applies["valid_json"] = True
    signals["valid_json"] = 0.0 if error else 1.0
    if error:
        notes.append(f"Did not return valid JSON: {error}. Return ONLY the JSON object, no prose or fences.")

    # deal_choice -----------------------------------------------------------
    applies["deal_choice"] = True
    expected_id = gold.get("opportunity_id")  # may be None when abstention is correct
    if gold.get("expect_abstain"):
        ok = opportunity_id in (None, "null", "")
        signals["deal_choice"] = 1.0 if ok else 0.0
        if not ok:
            notes.append(f"Chose '{opportunity_id}' but the text gives NO signal to pick between the candidate deals — set opportunity_id to null and extract nothing.")
    else:
        # Normalize the crm_/shadow_ label prefix so gold can store the bare id.
        def _bare(value: Any) -> str:
            text = str(value or "")
            for prefix in ("crm_", "shadow_"):
                if text.startswith(prefix):
                    return text[len(prefix):]
            return text
        ok = expected_id is None or _bare(opportunity_id) == _bare(expected_id)
        signals["deal_choice"] = 1.0 if ok else 0.0
        if not ok:
            notes.append(f"Attributed to '{opportunity_id}'; the message is about '{expected_id}'. Infer the deal from products/people/amounts/timing.")

    # fact_recall -----------------------------------------------------------
    must = [k.lower() for k in gold.get("must_find", [])]
    if must:
        applies["fact_recall"] = True
        blob = _fact_blob(facts)
        missing = [k for k in must if k not in blob]
        signals["fact_recall"] = 1.0 - len(missing) / len(must)
        if missing:
            notes.append(f"Missed the actionable fact(s): {missing}. Extract specific, sales-actionable facts (concerns, budget, deadlines, competitors).")
    else:
        applies["fact_recall"] = False

    # unknown_person --------------------------------------------------------
    if gold.get("expect_unknown_person"):
        applies["unknown_person"] = True
        name = gold["expect_unknown_person"].lower()
        found = any(name in str(p.get("name", "")).lower() for p in unknown)
        signals["unknown_person"] = 1.0 if found else 0.0
        if not found:
            notes.append(f"Did not surface the newly-mentioned person '{gold['expect_unknown_person']}' — a named person not in KNOWN ENTITIES belongs in unknown_persons, never merged into a similarly-titled contact.")
    else:
        applies["unknown_person"] = False

    total = sum(_WEIGHTS[n] for n, ok in applies.items() if ok)
    score = sum(_WEIGHTS[n] * signals[n] for n, ok in applies.items() if ok) / total if total else 0.0
    return ScoreBreakdown(round(score, 4), signals, applies, notes)


def metric_with_feedback(example, prediction, trace=None, pred_name=None, pred_trace=None):
    import dspy

    gold = example.gold if hasattr(example, "gold") else example["gold"]
    breakdown = score_extraction(prediction, gold, error=getattr(prediction, "error", None))
    header = f"Score {breakdown.score:.2f}. Case: {getattr(example, 'category', '?')}."
    lines = [header]
    if breakdown.feedback:
        lines.extend(f"- {n}" for n in breakdown.feedback)
    else:
        lines.append("- Extraction matched the target. Keep this behaviour.")
    return dspy.Prediction(score=breakdown.score, feedback="\n".join(lines))
