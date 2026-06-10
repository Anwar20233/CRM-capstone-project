"""Evaluate an arbitrary prompt on a split and (optionally) gate it against the
baseline for regressions.

    # evaluate an optimized prompt file on the held-out test split
    .venv/bin/python optimization/evaluate.py --prompt-file reports/gepa_prompt.txt \
        --split test --gate reports/baseline.json --out reports/gepa_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Put the service root on sys.path before importing the optimization package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optimization.harness import config
from optimization.harness.evaluation import (
    evaluate_prompt,
    print_summary,
    regression_gate,
    save_report,
)
from optimization.harness.worker_program import seed_prompt


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a prompt, optionally gate vs baseline.")
    parser.add_argument("--prompt-file", help="Path to a .txt prompt; omit to use the production prompt.")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--gate", help="Path to a baseline report JSON to regression-check against.")
    parser.add_argument("--max-category-drop", type=float, default=0.05)
    parser.add_argument("--out", default="eval.json")
    parser.add_argument("--label", default="candidate")
    args = parser.parse_args()

    config.load_env()

    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else seed_prompt()
    report = evaluate_prompt(prompt, args.split, repeats=args.repeats, label=args.label)
    print_summary(report)
    path = save_report(report, args.out)
    print(f"\nSaved report -> {path}")

    if args.gate:
        baseline = json.loads(Path(args.gate).read_text(encoding="utf-8"))
        gate = regression_gate(baseline, report, max_category_drop=args.max_category_drop)
        print("\n== regression gate ==")
        print(f"passed: {gate['passed']}   aggregate delta: {gate['aggregate_delta']:+.4f}")
        if gate["regressions"]:
            print("regressions:")
            for reg in gate["regressions"]:
                print(f"  {reg['category']:<18} {reg['baseline']:.3f} -> {reg['candidate']:.3f}"
                      f"  (drop {reg['drop']:.3f})")
        save_report(gate, args.out.replace(".json", "_gate.json"))


if __name__ == "__main__":
    main()
