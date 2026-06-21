"""Per-agent evaluation + regression gate for the follow-up harness.

``evaluate_spec`` runs a prompt over a split and builds a scored report (aggregate
+ per-category + per-example trace). ``regression_gate`` reuses the Writer
harness's gate logic so a winning prompt is accepted only if it doesn't regress.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optimization.followup.registry import AgentSpec

_REPORTS = Path(__file__).resolve().parent / "reports"


def _score_prediction(spec: AgentSpec, prediction, gold: dict[str, Any], example):
    """Dispatch to the agent's structural scorer (returns a ScoreBreakdown)."""
    error = getattr(prediction, "error", None)
    if spec.name == "next_step":
        from optimization.followup.metric.next_step_metric import score_actions
        return score_actions(getattr(prediction, "actions", []) or [], gold, error=error)
    if spec.name == "drafting":
        from optimization.followup.metric.drafting_metric import score_draft
        gold = {**gold, "company": getattr(example, "company", ""),
                "contact": getattr(example, "contact", "")}
        return score_draft(getattr(prediction, "subject", "") or "",
                           getattr(prediction, "body", "") or "", gold, error=error)
    if spec.name == "extraction":
        from optimization.followup.metric.extraction_metric import score_extraction
        return score_extraction(prediction, gold, error=error)
    if spec.name == "synthesis":
        from optimization.followup.metric.synthesis_metric import score_briefing
        return score_briefing(getattr(prediction, "briefing", "") or "", gold, error=error)
    if spec.name == "chat":
        from optimization.followup.metric.chat_metric import score_chat
        return score_chat(getattr(prediction, "tools_called", []) or [], gold, error=error)
    raise NotImplementedError(f"No structural scorer wired for agent '{spec.name}'.")


def evaluate_spec(
    spec: AgentSpec,
    prompt: str,
    split: str,
    *,
    model: str | None = None,
    label: str = "prompt",
) -> dict[str, Any]:
    program = spec.build_program(prompt, model=model, run_carrier=False)
    examples = spec.load_dataset(split)

    per_example: list[dict[str, Any]] = []
    by_category: dict[str, list[float]] = defaultdict(list)
    by_signal: dict[str, list[float]] = defaultdict(list)

    for example in examples:
        pred = program(**{spec.input_field: getattr(example, spec.input_field)})
        breakdown = _score_prediction(spec, pred, example.gold, example)
        by_category[example.category].append(breakdown.score)
        for signal, value in breakdown.signals.items():
            if breakdown.applicable.get(signal):
                by_signal[signal].append(value)
        per_example.append({
            "id": getattr(example, spec.input_field),
            "category": example.category,
            "score": breakdown.score,
            "error": getattr(pred, "error", None),
            "notes": breakdown.feedback,
        })

    all_scores = [row["score"] for row in per_example]
    return {
        "agent": spec.name,
        "label": label,
        "split": split,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n": len(per_example),
        "aggregate": round(statistics.mean(all_scores), 4) if all_scores else 0.0,
        "by_category": {
            cat: {"mean": round(statistics.mean(s), 4), "n": len(s)}
            for cat, s in sorted(by_category.items())
        },
        "by_signal": {
            sig: {"mean": round(statistics.mean(s), 4), "n": len(s)}
            for sig, s in sorted(by_signal.items())
        },
        "prompt_chars": len(prompt),
        "per_example": per_example,
    }


def save_report(report: dict[str, Any], name: str) -> Path:
    _REPORTS.mkdir(exist_ok=True)
    path = _REPORTS / name
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def print_summary(report: dict[str, Any]) -> None:
    print(f"\n== {report['agent']} | {report['label']} | split={report['split']} | n={report['n']} ==")
    print(f"aggregate: {report['aggregate']:.4f}   prompt_chars: {report['prompt_chars']}")
    for category, stats in report["by_category"].items():
        print(f"  {category:<22} {stats['mean']:.3f}  (n={stats['n']})")
    if report["by_signal"]:
        print("by_signal:")
        for signal, stats in report["by_signal"].items():
            print(f"  {signal:<22} {stats['mean']:.3f}  (n={stats['n']})")
