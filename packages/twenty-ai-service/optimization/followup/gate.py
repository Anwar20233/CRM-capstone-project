"""End-to-end regression gate for the optimized follow-up prompts.

Compares two ``scripts/followup_eval.py --json`` reports (baseline = current
prompts, candidate = optimized prompts, both run on gpt-5.4-mini) and accepts the
candidate only if:

  - overall pass-rate does not drop,
  - PII stays clean (zero leaks), and
  - no per-check dimension regresses by more than ``--max-drop`` (default 0.05).

    python optimization/followup/gate.py \
        --baseline out/baseline.json --candidate out/optimized.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def _pass_rate(report: dict[str, Any]) -> float:
    summary = report["summary"]
    total = summary["total"] or 1
    return round(summary["passed"] / total, 4)


def _dimension_rates(report: dict[str, Any]) -> dict[str, float]:
    """Per-check pass rate across all scenarios (the 'dimensions' we protect)."""
    passed: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for scenario in report["scenarios"]:
        for check in scenario["checks"]:
            total[check["name"]] += 1
            if check["passed"]:
                passed[check["name"]] += 1
    return {name: round(passed[name] / total[name], 4) for name in total}


def gate(baseline: dict[str, Any], candidate: dict[str, Any], *, max_drop: float) -> dict[str, Any]:
    base_rate, cand_rate = _pass_rate(baseline), _pass_rate(candidate)
    base_pii = baseline["summary"]["pii_leaks"]
    cand_pii = candidate["summary"]["pii_leaks"]

    base_dims, cand_dims = _dimension_rates(baseline), _dimension_rates(candidate)
    regressions = []
    for name, base in base_dims.items():
        cand = cand_dims.get(name)
        if cand is None:
            continue
        drop = round(base - cand, 4)
        if drop > max_drop:
            regressions.append({"dimension": name, "baseline": base, "candidate": cand, "drop": drop})

    passed = (cand_rate >= base_rate) and (cand_pii == 0) and not regressions
    return {
        "passed": passed,
        "pass_rate_delta": round(cand_rate - base_rate, 4),
        "baseline_pass_rate": base_rate,
        "candidate_pass_rate": cand_rate,
        "baseline_pii_leaks": base_pii,
        "candidate_pii_leaks": cand_pii,
        "max_drop": max_drop,
        "dimension_regressions": regressions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E gate for optimized follow-up prompts.")
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--max-drop", type=float, default=0.05)
    args = parser.parse_args()

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    result = gate(baseline, candidate, max_drop=args.max_drop)

    print(json.dumps(result, indent=2))
    if result["candidate_pii_leaks"]:
        print(f"\nREJECTED — candidate leaked PII in {result['candidate_pii_leaks']} place(s).")
    elif result["dimension_regressions"]:
        print("\nREJECTED — dimension regressions:")
        for reg in result["dimension_regressions"]:
            print(f"  {reg['dimension']}: {reg['baseline']} -> {reg['candidate']} (drop {reg['drop']})")
    elif not result["passed"]:
        print(f"\nREJECTED — pass-rate dropped ({result['pass_rate_delta']}).")
    else:
        print(f"\nACCEPTED — pass-rate {result['baseline_pass_rate']} -> "
              f"{result['candidate_pass_rate']}, PII clean, no dimension regression.")
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
