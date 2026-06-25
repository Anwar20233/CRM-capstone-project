"""Run the 30-scenario end-to-end eval with the OPTIMIZED prompts loaded.

Patches each agent's prompt constant **in memory** from
``reports/<agent>_prompt.txt`` (no source edits — fully reversible), pins the
model split (orchestrator vs subagents), then delegates to the real
``scripts/followup_eval.py`` main so the full pipeline + PII scan run unchanged.

    # default split: gpt-5.4-mini orchestrator, qwen subagents, all via OpenRouter
    python optimization/followup/eval_optimized.py --concurrency 8 \
        --json optimization/followup/reports/optimized_e2e.json

Pass ``--baseline`` to skip the prompt patch and eval the CURRENT prompts instead
(same model split), so the two reports are comparable for gate.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))  # so ``import followup_eval`` resolves
_REPORTS = Path(__file__).resolve().parent / "reports"

# (module path, attribute, report file) — the four prompts the E2E pipeline uses.
# Chat is not part of the orchestrator pipeline, so it is not exercised here.
_PATCH_TARGETS = [
    ("followup.next_step.agents.next_step.prompts", "SYSTEM_PROMPT", "next_step_prompt.txt"),
    ("followup.next_step.agents.next_step.next_step_agent", "SYSTEM_PROMPT", "next_step_prompt.txt"),
    ("followup.emailer.agents.drafting.prompts", "DRAFTING_SYSTEM_PROMPT", "drafting_prompt.txt"),
    ("followup.profile.prompts", "EXTRACTION_INSTRUCTIONS", "extraction_prompt.txt"),
    ("followup.profile.synthesis", "_SYSTEM_PROMPT", "synthesis_prompt.txt"),
]


def _patch_optimized_prompts() -> list[str]:
    import importlib

    patched: list[str] = []
    for module_path, attr, report in _PATCH_TARGETS:
        report_path = _REPORTS / report
        if not report_path.exists():
            continue
        text = report_path.read_text(encoding="utf-8")
        module = importlib.import_module(module_path)
        if hasattr(module, attr):
            setattr(module, attr, text)
            patched.append(f"{module_path}.{attr} ({len(text)}c)")
    return patched


def _route_openai_direct() -> str:
    """Point the global LLM client at OpenAI directly using OPENAI_API_KEY."""
    # Load the service .env first so OPENAI_API_KEY is visible (the LLM client
    # also loads it on import, but we read the key before that import runs).
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env", override=False)
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set in environment (.env).")
    os.environ["LLM_PROVIDER"] = "openai"
    os.environ["LLM_BASE_URL"] = "https://api.openai.com/v1"
    os.environ["LLM_API_KEY"] = key  # _load_config reads LLM_API_KEY
    return key


def _install_token_meter() -> None:
    """Append the token-meter callback at the two model-build seams the eval uses.

    We patch BEFORE followup_eval installs its own prompt-capture wrapper, so both
    callbacks end up on every model (its wrapper calls ours, then appends its own).
    """
    import agent.llm_client as llm_client
    import followup.profile.llm as profile_llm
    from optimization.followup.tokens import attach

    orig_resolve = llm_client._resolve_chat_model
    orig_build = profile_llm.build_chat_llm

    def resolve(model: str | None = None):
        return attach(orig_resolve(model))

    def build(model=None, temperature=0.0):  # noqa: ANN001
        return attach(orig_build(model, temperature))

    llm_client._resolve_chat_model = resolve
    profile_llm.build_chat_llm = build


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E eval with optimized prompts + token trace.")
    parser.add_argument("--orchestrator-model", default="gpt-5.4-mini")
    parser.add_argument("--subagent-model", default="gpt-5.4-mini")
    parser.add_argument("--provider", choices=["openai", "env"], default="openai",
                        help="openai = route directly at OpenAI via OPENAI_API_KEY (default); "
                             "env = leave .env routing (e.g. OpenRouter) untouched.")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--scenarios", default=None, help="comma-separated subset (default: all 30)")
    parser.add_argument("--json", default=str(_REPORTS / "optimized_openai_e2e.json"))
    parser.add_argument("--no-live-tokens", action="store_true", help="suppress the per-call token line.")
    parser.add_argument("--baseline", action="store_true",
                        help="eval the CURRENT prompts (skip the optimized patch).")
    args = parser.parse_args()

    if args.provider == "openai":
        _route_openai_direct()
        print("Provider: OpenAI direct (OPENAI_API_KEY).")

    # Pin the model split (both default to gpt-5.4-mini for the OpenAI-direct run).
    os.environ["FOLLOWUP_ORCHESTRATOR_MODEL"] = args.orchestrator_model
    os.environ["FOLLOWUP_SUBAGENT_MODEL"] = args.subagent_model

    from optimization.followup.tokens import METER
    METER.live = not args.no_live_tokens
    _install_token_meter()

    if args.baseline:
        print("Evaluating CURRENT prompts (baseline).")
    else:
        patched = _patch_optimized_prompts()
        print(f"Patched {len(patched)} optimized prompt(s) in memory:")
        for line in patched:
            print(f"  - {line}")

    print(f"Model split: orchestrator={args.orchestrator_model}  subagents={args.subagent_model}")
    print(f"Running {args.scenarios or 'all 30'} scenarios at concurrency {args.concurrency} "
          f"(live token trace -> stderr) ...\n")

    # Delegate to the real eval via its CLI surface (argparse).
    import followup_eval  # noqa: E402  (scripts/ is on sys.path via twenty-ai-service root)

    sys.argv = ["followup_eval", "--concurrency", str(args.concurrency), "--json", args.json]
    if args.scenarios:
        sys.argv += ["--scenarios", args.scenarios]
    try:
        asyncio.run(followup_eval.main())
    except SystemExit:
        pass  # the eval exits non-zero if any scenario fails; we still want the token report
    finally:
        print(METER.report())


if __name__ == "__main__":
    main()
