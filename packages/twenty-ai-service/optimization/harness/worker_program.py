"""DSPy program wrapping the live Writer agent (carrier-predictor pattern).

We deliberately keep the production ``BaseWorker.run()`` loop — we do **not**
rebuild the writer as ``dspy.ReAct``. To let GEPA/MIPROv2 optimize the *system
prompt* of that external loop, we hold the prompt as the ``instructions`` of one
"carrier" ``dspy.Predict``. GEPA proposes new ``instructions`` for the carrier;
``forward`` reads them and runs the real worker (live bridge, masking on) with
that candidate prompt. The carrier is also invoked once per rollout so GEPA has a
predictor trace to attach its reflection to — its LLM output is discarded.

The optimized ``instructions`` string is the deliverable: it ports straight back
into ``_WRITER_SYSTEM_PROMPT`` in ``agent/workers/writer_worker.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import dspy

from optimization.harness.bridge_runtime import run_request

_DATASET = Path(__file__).resolve().parent.parent / "dataset" / "writer_cases.json"


def seed_prompt() -> str:
    """The current production Writer system prompt (optimization starting point)."""
    from agent.workers.writer_worker import _WRITER_SYSTEM_PROMPT

    return _WRITER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Carrier signature — its instructions ARE the writer system prompt
# ---------------------------------------------------------------------------

class _WriterCarrier(dspy.Signature):
    """Placeholder instructions — replaced with the writer system prompt at init
    and rewritten by the optimizer."""

    request: str = dspy.InputField(desc="The orchestrator's instruction to the writer.")
    acknowledgement: str = dspy.OutputField(desc="A one-line restatement of the task.")


# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------

class WriterProgram(dspy.Module):
    """Optimizable wrapper around the live Writer worker."""

    def __init__(self, prompt: str | None = None, *, model: str | None = None,
                 teardown: bool = True, run_carrier: bool = True) -> None:
        super().__init__()
        self.carrier = dspy.Predict(_WriterCarrier)
        self.carrier.signature = self.carrier.signature.with_instructions(
            prompt or seed_prompt()
        )
        self._model = model
        self._teardown = teardown
        # During pure evaluation we skip the (cost-only) carrier LLM call.
        self._run_carrier = run_carrier

    @property
    def prompt(self) -> str:
        """The current candidate system prompt (carrier instructions)."""
        return self.carrier.signature.instructions

    def forward(self, request: str, **_labels) -> dspy.Prediction:
        # Register a predictor trace so GEPA has something to reflect on; the
        # carrier's own output is intentionally ignored.
        if self._run_carrier:
            try:
                self.carrier(request=request)
            except Exception:  # noqa: BLE001 — never let the dummy call fail a rollout
                pass

        result = run_request(
            self.prompt, request, model=self._model, teardown=self._teardown
        )
        return dspy.Prediction(
            response=result.response,
            tool_calls=result.tool_calls,
            prompt_masked=result.prompt_masked,
            created_records=result.created_records,
            torn_down=result.torn_down,
            error=result.error,
        )


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def load_dataset(split: str | None = None) -> list[dspy.Example]:
    """Load writer_cases.json as dspy Examples. ``split`` filters train/val/test."""
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    examples: list[dspy.Example] = []
    for case in cases:
        if split and case.get("split") != split:
            continue
        example = dspy.Example(
            request=case["request"],
            gold=case["gold"],
            category=case["category"],
            case_id=case["id"],
        ).with_inputs("request")
        examples.append(example)
    return examples


def split_counts() -> dict[str, int]:
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.get("split", "?")] = counts.get(case.get("split", "?"), 0) + 1
    return counts
