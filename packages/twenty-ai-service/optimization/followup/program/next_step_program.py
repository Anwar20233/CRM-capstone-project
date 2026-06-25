"""DSPy program wrapping the live Next-Step Intelligence agent.

Same carrier-predictor pattern as the Writer harness
(``optimization/harness/worker_program.py``): the agent's real ``SYSTEM_PROMPT``
is held as the ``instructions`` of a dummy ``dspy.Predict``. GEPA proposes new
``instructions``; ``forward`` runs the **real** ``run_next_step_agent`` with that
candidate prompt and returns its recommended actions for the metric to score.

The optimized ``instructions`` string ports straight back into ``SYSTEM_PROMPT``
in ``followup/next_step/agents/next_step/prompts.py``.

Unlike the Writer agent, the next-step agent needs no live Twenty bridge — it
reasons over an in-memory ``DealContext`` — so rollouts are cheap and side-effect
free (planning-skill tools fall back to bundled files when the DB is absent).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dspy

from followup.next_step.agents.next_step.next_step_agent import run_next_step_agent
from followup.next_step.context.schemas import DealContext
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType

_DATASET = Path(__file__).resolve().parent.parent / "dataset" / "next_step_cases.json"


def seed_prompt() -> str:
    """The current production next-step system prompt (optimization start point)."""
    from followup.next_step.agents.next_step.prompts import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def _load_cases() -> dict[str, dict[str, Any]]:
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    return {case["id"]: case for case in cases}


# ---------------------------------------------------------------------------
# Carrier signature — its instructions ARE the next-step system prompt
# ---------------------------------------------------------------------------

class _NextStepCarrier(dspy.Signature):
    """Placeholder instructions — replaced with SYSTEM_PROMPT at init and
    rewritten by the optimizer."""

    deal: str = dspy.InputField(desc="A short description of the deal + trigger.")
    acknowledgement: str = dspy.OutputField(desc="A one-line restatement of the situation.")


def _build_event(case: dict[str, Any], opportunity_id: str) -> FollowUpEvent:
    event_type = FollowUpEventType(case.get("event_type", "activity_logged"))
    return FollowUpEvent(
        event_id=str(uuid.uuid4()),
        idempotency_key=f"opt:{event_type.value}:{opportunity_id}:{uuid.uuid4()}",
        event_type=event_type,
        opportunity_id=opportunity_id,
        workspace_id="opt-workspace",
        user_id="opt-user",
        occurred_at=datetime.now(timezone.utc),
    )


def _serialize_actions(result) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for action in result.recommended_actions:
        orchestrator = getattr(action, "orchestrator_action", None)
        actions.append({
            "action_type": getattr(action, "action_type", None),
            "orchestrator_tool": getattr(orchestrator, "tool", None) if orchestrator else None,
            "priority": getattr(action, "priority", None),
            "evidence": list(getattr(action, "evidence", []) or []),
            "reasoning": getattr(action, "reasoning", "") or "",
            "title": getattr(action, "title", "") or "",
        })
    return actions


class NextStepProgram(dspy.Module):
    """Optimizable wrapper around the live next-step agent."""

    def __init__(self, prompt: str | None = None, *, model: str | None = None,
                 run_carrier: bool = True) -> None:
        super().__init__()
        self.carrier = dspy.Predict(_NextStepCarrier)
        self.carrier.signature = self.carrier.signature.with_instructions(
            prompt or seed_prompt()
        )
        self._model = model
        self._run_carrier = run_carrier
        self._cases = _load_cases()

    @property
    def prompt(self) -> str:
        return self.carrier.signature.instructions

    def forward(self, case_id: str, **_labels) -> dspy.Prediction:
        case = self._cases[case_id]
        context = DealContext.model_validate(case["context"])
        event = _build_event(case, context.opportunity.id)

        # Register a predictor trace so GEPA has something to reflect on; the
        # carrier's own output is intentionally ignored.
        if self._run_carrier:
            try:
                self.carrier(deal=case.get("trigger", "")[:500])
            except Exception:  # noqa: BLE001 — never let the dummy call fail a rollout
                pass

        error: str | None = None
        try:
            result = asyncio.run(run_next_step_agent(
                context,
                event,
                trigger_context=case.get("trigger"),
                model=self._model,
                system_prompt=self.prompt,
            ))
            actions = _serialize_actions(result)
            summary = result.summary_reasoning
            skipped = result.skipped
        except Exception as exc:  # noqa: BLE001 — surface as a scored failure
            actions, summary, skipped = [], "", False
            error = f"{type(exc).__name__}: {exc}"

        return dspy.Prediction(
            actions=actions,
            summary_reasoning=summary,
            skipped=skipped,
            error=error,
        )


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def load_dataset(split: str | None = None) -> list[dspy.Example]:
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    examples: list[dspy.Example] = []
    for case in cases:
        if split and case.get("split") != split:
            continue
        examples.append(
            dspy.Example(
                case_id=case["id"],
                gold=case["gold"],
                category=case["category"],
                trigger=case.get("trigger", ""),
            ).with_inputs("case_id")
        )
    return examples


def split_counts() -> dict[str, int]:
    cases = json.loads(_DATASET.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.get("split", "?")] = counts.get(case.get("split", "?"), 0) + 1
    return counts
