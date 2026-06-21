"""DSPy program wrapping the live chat agent (carrier-predictor pattern).

The chat agent's instructions live in ``_SYSTEM_PROMPT``
(``followup/chat/agent.py``). The carrier holds a candidate copy; ``forward``
runs the real ``run_followup_chat`` loop with that candidate against a lightweight
fake ``deps`` (no DB), capturing which tools the model selects via the
``on_tool_call`` hook. The metric scores tool selection against gold.

We score the *choice* of tool (captured before dispatch), so the fake backend can
return canned/empty results without affecting the signal. Synthetic data, no PII,
no masking.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dspy

from followup.chat.agent import run_followup_chat

_DATASET = Path(__file__).resolve().parent.parent / "dataset" / "chat_cases.json"
_OPP_ID = "11111111-1111-1111-1111-111111111111"
_USER_ID = "22222222-2222-2222-2222-222222222222"
_WORKSPACE_ID = "33333333-3333-3333-3333-333333333333"


def seed_prompt() -> str:
    from followup.chat.agent import _SYSTEM_PROMPT

    return _SYSTEM_PROMPT


def _load_cases() -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in json.loads(_DATASET.read_text(encoding="utf-8"))}


# ---------------------------------------------------------------------------
# Minimal fake backend — enough for the real loop to run without a DB.
# ---------------------------------------------------------------------------

class _FakePendingActions:
    async def list_pending(self, _opportunity_id, status="pending"):
        return []  # empty → the heavy from_db projection is never exercised

    async def get(self, _action_id):
        return None

    async def save(self, _action):
        return None


class _FakeProfileService:
    async def build_profile_narrative(self, _opportunity_id):
        return SimpleNamespace(
            narrative="Mid-stage deal; champion engaged, one open security concern.",
            key_facts=["Budget approved", "SSO support is an open concern"],
            relationships=["IT Lead reports to VP Operations"],
            risk_score=0.4,
        )


class _FakeDeps:
    def __init__(self) -> None:
        self.pipeline = SimpleNamespace(pending_actions=_FakePendingActions())
        self.profile_service = _FakeProfileService()


async def _fake_run_pipeline(**_kwargs):
    return SimpleNamespace(status="completed", pending_action_id="new-action-1", error=None)


class _FakeGraph:
    async def ainvoke(self, _state):
        return {"status": "completed"}


class _ChatCarrier(dspy.Signature):
    """Placeholder — replaced with _SYSTEM_PROMPT at init, rewritten by GEPA."""

    message: str = dspy.InputField(desc="The rep's chat message.")
    acknowledgement: str = dspy.OutputField(desc="A one-line restatement.")


class ChatProgram(dspy.Module):
    def __init__(self, prompt: str | None = None, *, model: str | None = None,
                 run_carrier: bool = True) -> None:
        super().__init__()
        self.carrier = dspy.Predict(_ChatCarrier)
        self.carrier.signature = self.carrier.signature.with_instructions(prompt or seed_prompt())
        self._model = model
        self._run_carrier = run_carrier
        self._cases = _load_cases()

    @property
    def prompt(self) -> str:
        return self.carrier.signature.instructions

    def forward(self, case_id: str, **_labels) -> dspy.Prediction:
        case = self._cases[case_id]
        tools_called: list[str] = []

        if self._run_carrier:
            try:
                self.carrier(message=case["message"][:300])
            except Exception:  # noqa: BLE001
                pass

        error: str | None = None
        reply = ""
        try:
            result = asyncio.run(run_followup_chat(
                deps=_FakeDeps(),
                accept_graph=_FakeGraph(),
                followup_graph=_FakeGraph(),
                run_pipeline=_fake_run_pipeline,
                opportunity_id=_OPP_ID,
                workspace_id=_WORKSPACE_ID,
                user_id=_USER_ID,
                message=case["message"],
                history=case.get("history", []),
                model=self._model,
                system_prompt=self.prompt,
                on_tool_call=lambda name, args: tools_called.append(name),
            ))
            reply = result.reply
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        return dspy.Prediction(tools_called=tools_called, reply=reply, error=error)


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
