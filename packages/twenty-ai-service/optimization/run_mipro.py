"""Optimize the Writer system prompt with MIPROv2 — the comparison baseline.

Same program, dataset, and (scalar) metric as GEPA, so the two optimizers are
directly comparable on the held-out test split. MIPROv2 tends to produce longer,
few-shot-heavy prompts and needs more rollouts; we run it once for a second data
point against GEPA.

    .venv/bin/python optimization/run_mipro.py --auto light
"""

from __future__ import annotations

import argparse
from pathlib import Path

import optimization._bootstrap  # noqa: F401
import dspy

from optimization.harness import config
from optimization.harness.evaluation import evaluate_prompt, print_summary, save_report
from optimization.harness.metric import score_trajectory
from optimization.harness.worker_program import WriterProgram, load_dataset

_REPORTS = Path(__file__).resolve().parent / "reports"


def _scalar_metric(example, prediction, trace=None) -> float:
    """MIPROv2 wants a scalar score (no textual feedback)."""
    gold = example.gold if hasattr(example, "gold") else example["gold"]
    response = getattr(prediction, "response", "") or ""
    tool_calls = getattr(prediction, "tool_calls", []) or []
    return score_trajectory(response, tool_calls, gold).score


def main() -> None:
    parser = argparse.ArgumentParser(description="MIPROv2-optimize the writer prompt.")
    parser.add_argument("--auto", default="light", choices=["light", "medium", "heavy"])
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--out-prompt", default="mipro_prompt.txt")
    parser.add_argument("--eval-after", action="store_true")
    args = parser.parse_args()

    config.configure()

    trainset = load_dataset("train")
    valset = load_dataset("val")
    program = WriterProgram(run_carrier=True, teardown=True)

    optimizer = dspy.MIPROv2(
        metric=_scalar_metric,
        auto=args.auto,
        num_threads=args.threads,
    )

    print(f"\nCompiling with MIPROv2 (auto={args.auto}) over "
          f"{len(trainset)} train / {len(valset)} val examples ...")
    optimized = optimizer.compile(
        program,
        trainset=trainset,
        valset=valset,
        # The carrier signature has no useful few-shot demos; optimize instructions only.
        max_bootstrapped_demos=0,
        max_labeled_demos=0,
        requires_permission_to_run=False,
    )

    optimized_prompt = optimized.prompt
    _REPORTS.mkdir(exist_ok=True)
    prompt_path = _REPORTS / args.out_prompt
    prompt_path.write_text(optimized_prompt, encoding="utf-8")
    print(f"\nSaved optimized prompt -> {prompt_path} ({len(optimized_prompt)} chars)")

    if args.eval_after:
        report = evaluate_prompt(optimized_prompt, "test", label="MIPROv2-optimized")
        print_summary(report)
        save_report(report, "mipro_eval.json")


if __name__ == "__main__":
    main()
