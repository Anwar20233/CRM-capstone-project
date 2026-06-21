"""DSPy program wrapping the live synthesis step (carrier-predictor pattern).

The synthesis agent's instructions live in ``_SYSTEM_PROMPT``
(``followup/profile/synthesis.py``). The carrier holds a candidate copy;
``forward`` calls the real ``synthesize_profile`` with that candidate and an
in-memory knowledge graph (no DB). Returns the briefing text for scoring.

The knowledge graph is built from synthetic, pre-masked-equivalent data; the
agent's own ProfileMasker only masks the names we register (deterministic, no
Presidio model load), so rollouts stay fast.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy

from agent.llm_client import get_chat_model
from followup.profile.schemas import ContactSummary
from followup.profile.synthesis import synthesize_profile

_DATASET = Path(__file__).resolve().parent.parent / "dataset" / "synthesis_cases.json"
_NOW = datetime(2026, 6, 20, tzinfo=timezone.utc)


def seed_prompt() -> str:
    from followup.profile.synthesis import _SYSTEM_PROMPT

    return _SYSTEM_PROMPT


def _load_cases() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(_DATASET.read_text(encoding="utf-8"))}


def _contacts(raw: list[dict[str, Any]]) -> list[ContactSummary]:
    return [
        ContactSummary(
            crm_id=c["crm_id"], name=c["name"], role=c.get("role"),
            email=c.get("email"), facts=c.get("facts", []),
            is_decision_maker=c.get("is_decision_maker", False),
        )
        for c in raw
    ]


def _facts(raw: list[dict[str, Any]]) -> list[Any]:
    return [
        SimpleNamespace(
            fact_type=f["fact_type"], fact_value=f["fact_value"],
            sentiment=f.get("sentiment"), extracted_at=_NOW,
        )
        for f in raw
    ]


def _shadows(raw: list[dict[str, Any]]) -> list[Any]:
    return [
        SimpleNamespace(
            id=s.get("id", s["name"]), name=s["name"],
            title_or_role=s.get("title_or_role"), mention_count=s.get("mention_count", 1),
        )
        for s in raw
    ]


def _relationships(raw: list[dict[str, Any]]) -> list[Any]:
    return [
        SimpleNamespace(
            relationship_type=r["relationship_type"], description=r.get("description"),
            last_seen_at=_NOW,
        )
        for r in raw
    ]


class _SynthesisCarrier(dspy.Signature):
    """Placeholder — replaced with _SYSTEM_PROMPT at init, rewritten by GEPA."""

    deal: str = dspy.InputField(desc="The deal to brief on.")
    acknowledgement: str = dspy.OutputField(desc="A one-line restatement.")


class SynthesisProgram(dspy.Module):
    def __init__(self, prompt: str | None = None, *, model: str | None = None,
                 run_carrier: bool = True) -> None:
        super().__init__()
        self.carrier = dspy.Predict(_SynthesisCarrier)
        self.carrier.signature = self.carrier.signature.with_instructions(prompt or seed_prompt())
        self._model = model
        self._run_carrier = run_carrier
        self._cases = _load_cases()

    @property
    def prompt(self) -> str:
        return self.carrier.signature.instructions

    def forward(self, case_id: str, **_labels) -> dspy.Prediction:
        case = self._cases[case_id]

        if self._run_carrier:
            try:
                self.carrier(deal=str(case.get("deal", {}).get("name", ""))[:200])
            except Exception:  # noqa: BLE001
                pass

        error: str | None = None
        briefing = ""
        try:
            briefing = asyncio.run(synthesize_profile(
                deal=case["deal"],
                company=case.get("company"),
                contacts=_contacts(case.get("contacts", [])),
                shadows=_shadows(case.get("shadows", [])),
                facts=_facts(case.get("facts", [])),
                relationships=_relationships(case.get("relationships", [])),
                risk_score=case.get("risk_score"),
                chat_llm=get_chat_model(self._model),
                system_prompt=self.prompt,
            ))
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        return dspy.Prediction(briefing=briefing, error=error)


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
