"""DSPy program wrapping the live extraction LLM step (carrier-predictor pattern).

The extraction agent's static instructions live in ``EXTRACTION_INSTRUCTIONS``
(``followup/profile/prompts.py``). The carrier holds a candidate copy; ``forward``
reproduces the graph's single LLM seam — ``build_extraction_prompt`` →
``chat_llm.ainvoke`` → ``parse_json_response`` — with that candidate, bypassing
the LangGraph reader/persistence (no DB). Returns the extracted graph for scoring.

Dataset content is synthetic (pre-masked-equivalent), so no Presidio masking runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import dspy
from langchain_core.messages import HumanMessage

from agent.llm_client import get_chat_model
from followup.profile.llm import parse_json_response
from followup.profile.prompts import build_extraction_prompt, render_known_entities

_DATASET = Path(__file__).resolve().parent.parent / "dataset" / "extraction_cases.json"


def seed_prompt() -> str:
    from followup.profile.prompts import EXTRACTION_INSTRUCTIONS

    return EXTRACTION_INSTRUCTIONS


def _load_cases() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(_DATASET.read_text(encoding="utf-8"))}


class _ExtractionCarrier(dspy.Signature):
    """Placeholder — replaced with EXTRACTION_INSTRUCTIONS at init, rewritten by GEPA."""

    message: str = dspy.InputField(desc="The source text to mine.")
    acknowledgement: str = dspy.OutputField(desc="A one-line restatement.")


class ExtractionProgram(dspy.Module):
    def __init__(self, prompt: str | None = None, *, model: str | None = None,
                 run_carrier: bool = True) -> None:
        super().__init__()
        self.carrier = dspy.Predict(_ExtractionCarrier)
        self.carrier.signature = self.carrier.signature.with_instructions(prompt or seed_prompt())
        self._model = model
        self._run_carrier = run_carrier
        self._cases = _load_cases()

    @property
    def prompt(self) -> str:
        return self.carrier.signature.instructions

    def forward(self, case_id: str, **_labels) -> dspy.Prediction:
        case = self._cases[case_id]
        entities = case["entities"]
        block = render_known_entities(
            entities.get("sender"),
            entities.get("contacts", []),
            entities.get("company"),
            entities.get("opportunities", []),
            entities.get("shadows", []),
        )
        prompt = build_extraction_prompt(
            case.get("source_type", "email"),
            case["source_text"],
            block,
            system_prompt=self.prompt,
        )

        if self._run_carrier:
            try:
                self.carrier(message=case["source_text"][:300])
            except Exception:  # noqa: BLE001
                pass

        error: str | None = None
        data: dict[str, Any] = {}
        try:
            llm = get_chat_model(self._model)
            response = asyncio.run(llm.ainvoke([HumanMessage(content=prompt)]))
            content = response.content if isinstance(response.content, str) else str(response.content)
            data = parse_json_response(content)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        return dspy.Prediction(
            opportunity_id=data.get("opportunity_id"),
            facts=data.get("facts") or [],
            relationships=data.get("relationships") or [],
            unknown_persons=data.get("unknown_persons") or [],
            error=error,
        )


def load_dataset(split: str | None = None) -> list[dspy.Example]:
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    examples: list[dspy.Example] = []
    for case in cases:
        if split and case.get("split") != split:
            continue
        examples.append(
            dspy.Example(case_id=case["id"], gold=case["gold"], category=case["category"]).with_inputs("case_id")
        )
    return examples


def split_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in json.loads(_DATASET.read_text(encoding="utf-8")):
        counts[case.get("split", "?")] = counts.get(case.get("split", "?"), 0) + 1
    return counts
