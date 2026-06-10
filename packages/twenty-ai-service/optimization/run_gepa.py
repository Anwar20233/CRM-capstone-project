"""Optimize the Writer system prompt with GEPA (reflective prompt evolution).

GEPA runs the live Writer agent on the train set, reads the composite trajectory
feedback from ``metric_with_feedback``, and uses a stronger reflection LM to
rewrite the prompt — keeping a Pareto frontier selected on the val set. The
winning prompt is written to ``reports/gepa_prompt.txt`` for review + porting back
into ``_WRITER_SYSTEM_PROMPT``.

    .venv/bin/python optimization/run_gepa.py --auto light

NOTE: every rollout hits the LIVE bridge and performs real tier-1/2 writes
(torn down afterwards). Keep --auto light unless you're pointed at a disposable
workspace and watching cost.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import optimization._bootstrap  # noqa: F401
import dspy

from optimization.harness import config
from optimization.harness.evaluation import evaluate_prompt, print_summary, save_report
from optimization.harness.metric import metric_with_feedback
from optimization.harness.worker_program import WriterProgram, load_dataset, split_counts

_REPORTS = Path(__file__).resolve().parent / "reports"


def main() -> None:
    parser = argparse.ArgumentParser(description="GEPA-optimize the writer prompt.")
    parser.add_argument("--auto", default="light", choices=["light", "medium", "heavy"])
    parser.add_argument("--threads", type=int, default=4, help="parallel rollouts (live bridge!).")
    parser.add_argument("--reflection-minibatch", type=int, default=3)
    parser.add_argument("--out-prompt", default="gepa_prompt.txt")
    parser.add_argument("--eval-after", action="store_true",
                        help="evaluate the winning prompt on the test split when done.")
    args = parser.parse_args()

    config.configure()  # task LM -> OpenAI; worker -> OpenAI
    print("splits:", split_counts())

    trainset = load_dataset("train")
    valset = load_dataset("val")

    program = WriterProgram(run_carrier=True, teardown=True)

    optimizer = dspy.GEPA(
        metric=metric_with_feedback,
        auto=args.auto,
        num_threads=args.threads,
        track_stats=True,
        track_best_outputs=True,
        reflection_minibatch_size=args.reflection_minibatch,
        reflection_lm=config.build_reflection_lm(),
    )

    print(f"\nCompiling with GEPA (auto={args.auto}) over "
          f"{len(trainset)} train / {len(valset)} val examples ...")
    optimized = optimizer.compile(program, trainset=trainset, valset=valset)

    optimized_prompt = optimized.prompt
    _REPORTS.mkdir(exist_ok=True)
    prompt_path = _REPORTS / args.out_prompt
    prompt_path.write_text(optimized_prompt, encoding="utf-8")
    print(f"\nSaved optimized prompt -> {prompt_path} ({len(optimized_prompt)} chars)")

    if args.eval_after:
        report = evaluate_prompt(optimized_prompt, "test", label="GEPA-optimized")
        print_summary(report)
        save_report(report, "gepa_eval.json")


if __name__ == "__main__":
    main()
