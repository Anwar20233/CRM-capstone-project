"""Evaluate the CURRENT production Writer prompt and save the baseline report.

This is the reference every optimized prompt is measured against (and the
regression gate compares to). Run it before optimizing.

    .venv/bin/python optimization/run_baseline.py --split test
"""

from __future__ import annotations

import argparse

import optimization._bootstrap  # noqa: F401  (sys.path side effect)
from optimization.harness import config
from optimization.harness.evaluation import evaluate_prompt, print_summary, save_report
from optimization.harness.worker_program import seed_prompt


def main() -> None:
    parser = argparse.ArgumentParser(description="Baseline-eval the current writer prompt.")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--repeats", type=int, default=1, help="rollouts per example (avg).")
    parser.add_argument("--out", default="baseline.json")
    args = parser.parse_args()

    config.load_env()  # worker -> OpenAI; no DSPy LM needed for plain eval.

    report = evaluate_prompt(
        seed_prompt(), args.split, repeats=args.repeats, label="baseline (production prompt)"
    )
    print_summary(report)
    path = save_report(report, args.out)
    print(f"\nSaved baseline report -> {path}")


if __name__ == "__main__":
    main()
