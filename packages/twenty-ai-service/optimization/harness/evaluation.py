"""Evaluation core — run a prompt over a split and build a scored report.

Shared by ``run_baseline.py`` and ``evaluate.py``. Produces an aggregate score,
per-category and per-signal breakdowns, a per-example trace, and a masking-health
check. ``regression_gate`` compares a candidate report against a baseline and
fails if the aggregate drops or any category regresses beyond a threshold.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optimization.harness.metric import score_trajectory
from optimization.harness.worker_program import WriterProgram, load_dataset

_REPORTS = Path(__file__).resolve().parent.parent / "reports"

_masking_warmed = False


def warm_up_masking() -> bool:
    """Load the Presidio NER model once, up front, so masking reliably engages
    (and we surface a clear status instead of silently degrading mid-run)."""
    global _masking_warmed
    if _masking_warmed:
        return True
    from pipelines import load_models, models_loaded

    load_models()
    available = models_loaded()
    print(f"[masking] Presidio NER {'loaded' if available else 'UNAVAILABLE (masking disabled)'}")
    _masking_warmed = True
    return available


def evaluate_prompt(
    prompt: str,
    split: str,
    *,
    model: str | None = None,
    repeats: int = 1,
    label: str = "prompt",
) -> dict[str, Any]:
    """Run *prompt* over *split* (optionally repeated) and return a report dict."""
    warm_up_masking()
    program = WriterProgram(
        prompt, model=model, teardown=True, run_carrier=False
    )
    examples = load_dataset(split)

    per_example: list[dict[str, Any]] = []
    by_category_scores: dict[str, list[float]] = defaultdict(list)
    by_signal_scores: dict[str, list[float]] = defaultdict(list)
    masked_hits = 0
    masked_total = 0

    for example in examples:
        run_scores: list[float] = []
        last_breakdown = None
        last_pred = None
        for _ in range(repeats):
            pred = program(request=example.request)
            breakdown = score_trajectory(pred.response, pred.tool_calls, example.gold)
            run_scores.append(breakdown.score)
            last_breakdown, last_pred = breakdown, pred

        score = statistics.mean(run_scores)
        by_category_scores[example.category].append(score)
        for signal, value in last_breakdown.signals.items():
            if last_breakdown.applicable.get(signal):
                by_signal_scores[signal].append(value)

        # Masking health: only meaningful when the request carried PII.
        if last_pred.prompt_masked:
            masked_hits += 1
        masked_total += 1

        per_example.append({
            "id": example.case_id,
            "category": example.category,
            "score": round(score, 4),
            "actual_outcome": last_breakdown.actual_outcome,
            "gold_outcome": example.gold["outcome"],
            "n_tool_calls": len(last_pred.tool_calls),
            "min_tool_calls": example.gold.get("min_tool_calls"),
            "prompt_masked": last_pred.prompt_masked,
            "torn_down": last_pred.torn_down,
            "error": last_pred.error,
            "notes": last_breakdown.feedback,
        })

    all_scores = [row["score"] for row in per_example]
    report = {
        "label": label,
        "split": split,
        "model": model,
        "repeats": repeats,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n": len(per_example),
        "aggregate": round(statistics.mean(all_scores), 4) if all_scores else 0.0,
        "by_category": {
            category: {"mean": round(statistics.mean(scores), 4), "n": len(scores)}
            for category, scores in sorted(by_category_scores.items())
        },
        "by_signal": {
            signal: {"mean": round(statistics.mean(scores), 4), "n": len(scores)}
            for signal, scores in sorted(by_signal_scores.items())
        },
        "masking_rate": round(masked_hits / masked_total, 4) if masked_total else 0.0,
        "prompt_chars": len(prompt),
        "per_example": per_example,
    }
    return report


def save_report(report: dict[str, Any], name: str) -> Path:
    _REPORTS.mkdir(exist_ok=True)
    path = _REPORTS / name
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def regression_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_category_drop: float = 0.05,
) -> dict[str, Any]:
    """Accept candidate only if aggregate >= baseline AND no category regresses."""
    aggregate_delta = round(candidate["aggregate"] - baseline["aggregate"], 4)
    regressions: list[dict[str, Any]] = []
    for category, base in baseline["by_category"].items():
        cand = candidate["by_category"].get(category)
        if cand is None:
            continue
        drop = round(base["mean"] - cand["mean"], 4)
        if drop > max_category_drop:
            regressions.append({
                "category": category,
                "baseline": base["mean"],
                "candidate": cand["mean"],
                "drop": drop,
            })
    passed = aggregate_delta >= 0 and not regressions
    return {
        "passed": passed,
        "aggregate_delta": aggregate_delta,
        "baseline_aggregate": baseline["aggregate"],
        "candidate_aggregate": candidate["aggregate"],
        "max_category_drop": max_category_drop,
        "regressions": regressions,
    }


def print_summary(report: dict[str, Any]) -> None:
    print(f"\n== {report['label']} | split={report['split']} | n={report['n']} ==")
    print(f"aggregate: {report['aggregate']:.4f}   masking_rate: {report['masking_rate']:.2f}"
          f"   prompt_chars: {report['prompt_chars']}")
    print("by_category:")
    for category, stats in report["by_category"].items():
        print(f"  {category:<18} {stats['mean']:.3f}  (n={stats['n']})")
    print("by_signal:")
    for signal, stats in report["by_signal"].items():
        print(f"  {signal:<18} {stats['mean']:.3f}  (n={stats['n']})")
