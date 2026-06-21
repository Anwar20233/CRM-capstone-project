"""GEPA-optimize the follow-up agent prompts (one pass per agent).

For each registered agent this runs ``dspy.GEPA`` over its train/val split using
the agent's feedback metric, writes the winning prompt to
``reports/<agent>_prompt.txt``, and (optionally) evaluates it on the test split.
Agents are optimized in isolation (cheap, strong per-prompt signal); the
end-to-end gate is a separate step (``followup_eval.py`` + ``gate.py``).

Speed: two layers of parallelism — GEPA runs ``--threads`` rollouts concurrently
*within* each agent, and (by default) all agents are optimized concurrently
across a thread pool. With synthetic data and masking bypassed, the only cost is
the OpenAI calls, so wall-clock ≈ the slowest single agent. Use ``--sequential``
or lower ``--threads`` if you hit rate limits.

    # all agents, fully parallel
    python optimization/followup/run_all.py --auto light --eval-after
    # one agent
    python optimization/followup/run_all.py --only next_step --auto light --eval-after

Routing (model = gpt-5.4-mini, reflection = gpt-5.4) comes from .env.training via
optimization/harness/config.configure().
"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Service root on sys.path before importing the optimization package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import dspy

from optimization.harness import config
from optimization.followup.evaluate import evaluate_spec, print_summary, save_report
from optimization.followup.registry import AgentSpec, available_agents, specs

_REPORTS = Path(__file__).resolve().parent / "reports"
_PRINT_LOCK = threading.Lock()


def _log(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def _optimize(spec: AgentSpec, args) -> str:
    _log(f"[{spec.name}] start — splits: {spec.split_counts()}")
    trainset = spec.load_dataset("train")
    valset = spec.load_dataset("val")

    program = spec.build_program(run_carrier=True)
    optimizer = dspy.GEPA(
        metric=spec.metric,
        auto=args.auto,
        num_threads=args.threads,
        track_stats=True,
        track_best_outputs=True,
        reflection_minibatch_size=args.reflection_minibatch,
        reflection_lm=config.build_reflection_lm(),
    )
    _log(f"[{spec.name}] compiling GEPA (auto={args.auto}) over "
         f"{len(trainset)} train / {len(valset)} val ...")
    optimized = optimizer.compile(program, trainset=trainset, valset=valset)

    _REPORTS.mkdir(exist_ok=True)
    prompt_path = _REPORTS / f"{spec.name}_prompt.txt"
    prompt_path.write_text(optimized.prompt, encoding="utf-8")
    _log(f"[{spec.name}] saved optimized prompt -> {prompt_path} ({len(optimized.prompt)} chars)")

    if args.eval_after:
        report = evaluate_spec(spec, optimized.prompt, "test", label=f"{spec.name}-GEPA")
        save_report(report, f"{spec.name}_eval.json")
        with _PRINT_LOCK:
            print_summary(report)
    return optimized.prompt


def main() -> None:
    parser = argparse.ArgumentParser(description="GEPA-optimize follow-up agent prompts.")
    parser.add_argument("--only", choices=available_agents(), help="optimize a single agent.")
    parser.add_argument("--auto", default="light", choices=["light", "medium", "heavy"])
    parser.add_argument("--threads", type=int, default=6,
                        help="concurrent rollouts WITHIN each agent's GEPA run.")
    parser.add_argument("--reflection-minibatch", type=int, default=3)
    parser.add_argument("--sequential", action="store_true",
                        help="optimize agents one at a time (default: all in parallel).")
    parser.add_argument("--eval-after", action="store_true",
                        help="evaluate each winning prompt on its test split.")
    args = parser.parse_args()

    config.configure()  # task LM -> gpt-5.4-mini, reflection -> gpt-5.4 (from .env.training)

    chosen = [args.only] if args.only else None
    chosen_specs = specs(chosen)

    failures: dict[str, str] = {}
    if args.sequential or len(chosen_specs) == 1:
        for spec in chosen_specs:
            try:
                _optimize(spec, args)
            except Exception as exc:  # noqa: BLE001 — one agent failing shouldn't abort the rest
                failures[spec.name] = f"{type(exc).__name__}: {exc}"
                _log(f"[{spec.name}] FAILED: {failures[spec.name]}")
    else:
        # All agents concurrently; each still runs --threads rollouts in parallel.
        with ThreadPoolExecutor(max_workers=len(chosen_specs)) as pool:
            future_to_name = {pool.submit(_optimize, spec, args): spec.name for spec in chosen_specs}
            for future in as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    failures[name] = f"{type(exc).__name__}: {exc}"
                    _log(f"[{name}] FAILED: {failures[name]}")

    print("\n" + "=" * 70)
    if failures:
        print(f"Completed with {len(failures)} failure(s): {failures}")
    print("Done. Review reports/<agent>_prompt.txt, port each back into its prompt "
          "constant (see registry.py for the file+symbol), then run the end-to-end "
          "gate (followup_eval.py + gate.py).")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
