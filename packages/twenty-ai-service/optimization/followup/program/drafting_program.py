"""DSPy program wrapping the live Drafting agent (carrier-predictor pattern).

The drafting agent's static instructions live in ``DRAFTING_SYSTEM_PROMPT``
(``followup/emailer/agents/drafting/prompts.py``). The carrier holds a candidate
copy; ``forward`` calls the real ``build_draft_prompt`` + ``call_llm_json`` path
with that candidate and returns the generated draft for the metric to score.

Isolated by design: RAG retrieval is bypassed (we pass a bundled template file
directly and no catalog), so rollouts need no vector store, DB, or Presidio
masking. Dataset content is synthetic (pre-masked-equivalent), so nothing needs
masking/unmasking.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import dspy

from agent.llm_client import call_llm_json
from followup.emailer.agents.drafting.prompts import build_draft_prompt
from followup.emailer.agents.drafting.schemas import EMAIL_DRAFT_TYPES, DraftType, EmailDraft, ProposalDraft
from followup.emailer.context.schemas import (
    CompanyContext,
    ContactContext,
    DealContext,
    MeetingSummary,
    NoteSummary,
    OpportunityContext,
)

_DATASET = Path(__file__).resolve().parent.parent / "dataset" / "drafting_cases.json"
_TEMPLATES = Path(__file__).resolve().parents[3] / "followup/emailer/knowledge/email_templates"


def seed_prompt() -> str:
    from followup.emailer.agents.drafting.prompts import DRAFTING_SYSTEM_PROMPT

    return DRAFTING_SYSTEM_PROMPT


def _load_cases() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(_DATASET.read_text(encoding="utf-8"))}


def _context(deal: dict[str, Any]) -> DealContext:
    return DealContext(
        opportunity=OpportunityContext(**deal["opportunity"]),
        company=CompanyContext(**deal["company"]),
        contact=ContactContext(**deal["contact"]),
        recent_meetings=[MeetingSummary(**m) for m in deal.get("recent_meetings", [])],
        recent_notes=[NoteSummary(**n) for n in deal.get("recent_notes", [])],
    )


def _template_text(key: str | None) -> str:
    if not key:
        return ""
    path = _TEMPLATES / key
    return path.read_text(encoding="utf-8") if path.exists() else ""


class _DraftingCarrier(dspy.Signature):
    """Placeholder — replaced with DRAFTING_SYSTEM_PROMPT at init, rewritten by GEPA."""

    deal: str = dspy.InputField(desc="A short description of the deal to draft for.")
    acknowledgement: str = dspy.OutputField(desc="A one-line restatement of the draft task.")


class DraftingProgram(dspy.Module):
    def __init__(self, prompt: str | None = None, *, model: str | None = None,
                 run_carrier: bool = True) -> None:
        super().__init__()
        self.carrier = dspy.Predict(_DraftingCarrier)
        self.carrier.signature = self.carrier.signature.with_instructions(prompt or seed_prompt())
        self._model = model
        self._run_carrier = run_carrier
        self._cases = _load_cases()

    @property
    def prompt(self) -> str:
        return self.carrier.signature.instructions

    def forward(self, case_id: str, **_labels) -> dspy.Prediction:
        case = self._cases[case_id]
        context = _context(case["deal"])
        draft_type = DraftType(case["draft_type"])
        template = _template_text(case.get("template_key"))

        if self._run_carrier:
            try:
                self.carrier(deal=case.get("deal", {}).get("company", {}).get("name", "")[:200])
            except Exception:  # noqa: BLE001
                pass

        prompt = build_draft_prompt(context, template, [], draft_type, system_prompt=self.prompt)
        schema = EmailDraft if draft_type in EMAIL_DRAFT_TYPES else ProposalDraft

        error: str | None = None
        subject = body = ""
        try:
            draft = asyncio.run(call_llm_json(prompt, schema, model=self._model))
            if isinstance(draft, EmailDraft):
                subject, body = draft.subject, draft.body
            else:
                subject = draft.title
                body = "\n".join(f"{s.heading}\n{s.content}" for s in draft.sections)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        return dspy.Prediction(subject=subject, body=body, draft_type=draft_type.value, error=error)


def load_dataset(split: str | None = None) -> list[dspy.Example]:
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    examples: list[dspy.Example] = []
    for case in cases:
        if split and case.get("split") != split:
            continue
        # Carry the deal so the metric can check personalization.
        examples.append(
            dspy.Example(
                case_id=case["id"], gold=case["gold"], category=case["category"],
                company=case["deal"]["company"]["name"],
                contact=case["deal"]["contact"]["name"],
            ).with_inputs("case_id")
        )
    return examples


def split_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in json.loads(_DATASET.read_text(encoding="utf-8")):
        counts[case.get("split", "?")] = counts.get(case.get("split", "?"), 0) + 1
    return counts
