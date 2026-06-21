"""Offline self-check for the follow-up optimization harness — NO API calls.

Stubs each agent's single LLM seam with a canned response, then drives the real
carrier program → agent → metric loop for one example per agent. Proves the
wiring (dataset load, input construction, prompt swap, prediction shape, scoring)
end to end without spending tokens. Run before the real GEPA pass:

    python optimization/followup/selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class _StubMessage:
    def __init__(self, content: str = "", tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


def _check_next_step() -> tuple[bool, str]:
    from followup.next_step.agents.next_step import next_step_agent as mod
    from followup.next_step.agents.next_step.schemas import (
        NextStepAgentResult, OrchestratorAction, RecommendedAction,
    )
    from optimization.followup.program import next_step_program as prog
    from optimization.followup.metric.next_step_metric import score_actions

    async def fake_agent(context, event, *, trigger_context=None, model=None, system_prompt=None, **_):
        assert system_prompt and "B2B sales coach" in system_prompt  # candidate prompt threaded through
        return NextStepAgentResult(
            recommended_actions=[RecommendedAction(
                action_type="send_email", title="Reply", description="Answer the question",
                priority=3, reasoning="Buyer asked a question", evidence=["Email asks about EU data residency"],
                profile_fact_refs=[],
                orchestrator_action=OrchestratorAction(tool="send_email", instruction="reply", params={}),
            )],
            summary_reasoning="ok", confidence=0.8, skipped=False, skip_reason=None,
        )

    prog.run_next_step_agent = fake_agent  # type: ignore[assignment]
    program = prog.NextStepProgram(run_carrier=False)
    pred = program(case_id="ns_question_only")
    breakdown = score_actions(pred.actions, {"tools": ["send_email"], "urgency": ["medium", "low"], "expect_no_action": False})
    return breakdown.score >= 0.9, f"score={breakdown.score} actions={len(pred.actions)}"


def _check_drafting() -> tuple[bool, str]:
    from followup.emailer.agents.drafting.schemas import EmailDraft
    from optimization.followup.program import drafting_program as prog
    from optimization.followup.metric.drafting_metric import score_draft

    body = ("Hi Amy, thanks for raising data residency. Yes — data can be pinned to the EU region. "
            + "We support regional data pinning end to end. " * 12
            + "\n\nBest regards,\n[Your Name]\nBeamData")

    async def fake_call_llm_json(prompt, schema, model=None):
        assert "sales drafting assistant" in prompt
        return EmailDraft(subject="Re: data residency", body=body, draft_type="follow_up_email")

    prog.call_llm_json = fake_call_llm_json  # type: ignore[assignment]
    program = prog.DraftingProgram(run_carrier=False)
    pred = program(case_id="dr_followup_question")
    gold = {"must_include": ["data residency", "EU"], "company": "Initech", "contact": "Amy Chen"}
    breakdown = score_draft(pred.subject, pred.body, gold)
    return breakdown.score >= 0.8, f"score={breakdown.score} subj={pred.subject!r}"


def _check_extraction() -> tuple[bool, str]:
    from optimization.followup.program import extraction_program as prog
    from optimization.followup.metric.extraction_metric import score_extraction

    canned = ('{"opportunity_id":"crm_opp-init","facts":[{"entity_id":"crm_amy",'
              '"fact_type":"concern","fact_value":"EU data residency is a hard requirement",'
              '"confidence":0.9,"sentiment":"negative"}],"relationships":[],"unknown_persons":[]}')

    def fake_get_chat_model(model=None):
        async def ainvoke(messages):
            return _StubMessage(content=canned)
        return SimpleNamespace(ainvoke=ainvoke)

    prog.get_chat_model = fake_get_chat_model  # type: ignore[assignment]
    program = prog.ExtractionProgram(run_carrier=False)
    pred = program(case_id="ex_single_concern")
    breakdown = score_extraction(pred, {"opportunity_id": "opp-init", "must_find": ["residency", "EU"]})
    return breakdown.score >= 0.8, f"score={breakdown.score} opp={pred.opportunity_id}"


def _check_synthesis() -> tuple[bool, str]:
    from optimization.followup.program import synthesis_program as prog
    from optimization.followup.metric.synthesis_metric import score_briefing

    briefing = ("Alice Wong (CISO) is the decision-maker on the Umbrella security platform deal. "
                "She is weighing CompetitorX on cost and the decision meeting is Friday, so the "
                "risk here is real and the cost gap must be addressed quickly.")

    def fake_get_chat_model(model=None):
        async def ainvoke(messages):
            return _StubMessage(content=briefing)
        return SimpleNamespace(ainvoke=ainvoke)

    prog.get_chat_model = fake_get_chat_model  # type: ignore[assignment]
    program = prog.SynthesisProgram(run_carrier=False)
    pred = program(case_id="sy_at_risk_competitor")
    gold = {"contact_names": ["Alice Wong"], "must_mention": ["competitorx", "cost"], "expect_risk": True}
    breakdown = score_briefing(pred.briefing, gold)
    return breakdown.score >= 0.8, f"score={breakdown.score} leak_free len={len(pred.briefing.split())}"


def _check_chat() -> tuple[bool, str]:
    from followup.chat import agent as chat_agent
    from optimization.followup.program import chat_program as prog
    from optimization.followup.metric.chat_metric import score_chat

    # Stub LLM: first turn calls GetOpportunityHealth, second turn replies.
    class _StubLLM:
        def __init__(self) -> None:
            self._turn = 0

        def bind_tools(self, _tools):
            return self

        async def ainvoke(self, _messages):
            self._turn += 1
            if self._turn == 1:
                return _StubMessage(tool_calls=[{"name": "GetOpportunityHealth", "args": {}, "id": "t1"}])
            return _StubMessage(content="This deal is mid-stage with one open security concern.")

    chat_agent.get_chat_model = lambda model=None: _StubLLM()  # type: ignore[assignment]
    program = prog.ChatProgram(run_carrier=False)
    pred = program(case_id="ch_health_question")
    gold = {"required_tools": ["GetOpportunityHealth"],
            "forbidden_tools": ["CreateFollowup", "AcceptAction"]}
    breakdown = score_chat(pred.tools_called, gold)
    return breakdown.score >= 0.9, f"score={breakdown.score} tools={pred.tools_called}"


def main() -> int:
    checks = [
        ("next_step", _check_next_step),
        ("drafting", _check_drafting),
        ("extraction", _check_extraction),
        ("synthesis", _check_synthesis),
        ("chat", _check_chat),
    ]
    failed = 0
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<11} {detail}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} agents wired correctly (offline).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
