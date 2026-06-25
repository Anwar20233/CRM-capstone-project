"""Manual use-case walkthrough for the Next Step Intelligence Agent.

This is NOT a pytest test (no assertions) — it's a runnable script that
exercises `run_next_step_agent` against realistic scenarios and prints the
resulting `NextStepAgentResult` as JSON, so you can eyeball the behavior.

The LLM and RetrievalService are mocked/stubbed exactly like in
`followup/tests/test_next_step_agent.py`, so this requires no API keys,
network access, or database.

Run from `packages/twenty-ai-service`:

    python -m followup.next_step.agents.next_step.demo_use_cases
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from followup.next_step.agents.next_step import next_step_agent as next_step_agent_module
from followup.next_step.agents.next_step.next_step_agent import run_next_step_agent
from followup.next_step.agents.next_step.schemas import NextStepLLMActionItem, NextStepLLMOutput
from followup.next_step.context.schemas import DealContext
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType
from followup.next_step.rag.collections import CollectionName
from followup.next_step.rag.service import RetrievedChunk

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


# ---------------------------------------------------------------------------
# Stubs (same shape as the test suite's StubRetrievalService / fake LLM)
# ---------------------------------------------------------------------------


class StubRetrievalService:
    """In-memory RetrievalService for demo purposes."""

    def __init__(self, chunks: list[RetrievedChunk] | None = None, *, raise_error: bool = False):
        self._chunks = chunks or []
        self._raise_error = raise_error

    async def retrieve_documents(self, query: str, collection: CollectionName, top_k: int = 5) -> list[RetrievedChunk]:
        if self._raise_error:
            raise RuntimeError("retrieval backend unavailable")
        return [chunk for chunk in self._chunks if chunk.collection == collection][:top_k]


def _sample_chunks() -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            content="Discovery: confirm decision maker and document the pain point before proposing.",
            source="playbooks/Discovery.md",
            collection=CollectionName.SALES_PLAYBOOKS,
            score=0.9,
        ),
        RetrievedChunk(
            content="Proposal: send proposal once budget and need are qualified.",
            source="playbooks/Proposal.md",
            collection=CollectionName.SALES_PLAYBOOKS,
            score=0.85,
        ),
        RetrievedChunk(
            content="BANT: Authority means a contact flagged as decision maker is engaged.",
            source="bant.md",
            collection=CollectionName.BANT,
            score=0.8,
        ),
    ]


def _make_fake_llm(actions: list[NextStepLLMActionItem], summary: str, confidence: float):
    """Build a fake `call_llm_json` that always returns the given output."""

    async def fake_call_llm_json(prompt: str, schema):
        assert schema is NextStepLLMOutput
        return NextStepLLMOutput(actions=actions, summary_reasoning=summary, confidence=confidence)

    return fake_call_llm_json


def _make_failing_llm(exc: Exception):
    async def fake_call_llm_json(prompt: str, schema):
        raise exc

    return fake_call_llm_json


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_context(filename: str) -> DealContext:
    with open(FIXTURES_DIR / filename, encoding="utf-8") as handle:
        return DealContext.model_validate(json.load(handle))


def _make_event(event_type: FollowUpEventType, opportunity_id: str, workspace_id: str = "workspace-1") -> FollowUpEvent:
    return FollowUpEvent(
        event_id=str(uuid4()),
        idempotency_key=f"{workspace_id}:{event_type.value}:{opportunity_id}:{uuid4()}",
        event_type=event_type,
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        user_id="user-1",
        occurred_at=datetime.now(timezone.utc),
    )


def _print_result(title: str, result) -> None:
    print(f"\n{'=' * 70}\nUSE CASE: {title}\n{'=' * 70}")
    print(result.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def use_case_1_discovery_stage_changed():
    """Opportunity moves stage in Discovery -> expect 1-3 recommendations."""
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    actions = [
        NextStepLLMActionItem(
            action_type="qualify_authority",
            title="Identify the decision maker",
            description="No contact is flagged as a decision maker yet.",
            priority=1,
            reasoning="BANT Authority is unqualified, which blocks moving to Proposal.",
            evidence=["No contact has is_decision_maker=true.", "BANT: Authority requires a decision maker engaged."],
            profile_fact_refs=["fact-1"],
            suggested_crm_instruction="Create a task: Identify and engage the economic buyer",
        ),
        NextStepLLMActionItem(
            action_type="create_task",
            title="Resolve overdue discovery call task",
            description="The discovery call task is overdue.",
            priority=2,
            reasoning="An overdue task signals stalled momentum in Discovery.",
            evidence=["Task 'Schedule discovery call with operations team' is overdue."],
            profile_fact_refs=[],
            suggested_crm_instruction="Reschedule the overdue discovery call task",
        ),
        NextStepLLMActionItem(
            action_type="schedule_demo",
            title="Schedule a product demo",
            description="Set up a demo focused on inventory reconciliation.",
            priority=3,
            reasoning="The buyer described a clear pain point but no demo has occurred yet.",
            evidence=["Timeline note: pain with manual inventory reconciliation."],
            profile_fact_refs=[],
            suggested_crm_instruction="Create a task: Schedule product demo",
        ),
    ]
    fake_llm = _make_fake_llm(
        actions,
        summary="Deal is stalled in Discovery; qualify authority and address the overdue task before pursuing the demo.",
        confidence=0.78,
    )
    next_step_agent_module.call_llm_json = fake_llm

    result = await run_next_step_agent(context, event, StubRetrievalService(_sample_chunks()))
    _print_result("1. Discovery -> opportunity_stage_changed (happy path)", result)


async def use_case_2_meeting_completed_proposal():
    """Meeting completed on a Proposal-stage deal -> expect recap-style recommendation."""
    context = _load_context("next_step_context_proposal.json")
    event = _make_event(FollowUpEventType.MEETING_COMPLETED, context.opportunity.id)

    actions = [
        NextStepLLMActionItem(
            action_type="send_proposal",
            title="Send the proposal",
            description="Send the proposal now that budget ($75k-$90k) and need are confirmed.",
            priority=1,
            reasoning="Discovery wrap-up confirmed budget and Q3 timeline; the deal is ready for Proposal.",
            evidence=["Timeline: Discovery wrap-up call confirmed budget range of $75k-$90k and Q3 go-live target."],
            profile_fact_refs=["fact-2", "fact-3"],
            suggested_crm_instruction="Create a note with the proposal for Priya Shah",
        ),
        NextStepLLMActionItem(
            action_type="schedule_meeting",
            title="Schedule proposal walkthrough",
            description="Book a meeting with Priya Shah to walk through the proposal.",
            priority=2,
            reasoning="No future meeting is currently scheduled (has_future_meeting=false).",
            evidence=["engagement.has_future_meeting = false"],
            profile_fact_refs=[],
            suggested_crm_instruction="Schedule a meeting with Priya Shah to review the proposal",
        ),
    ]
    fake_llm = _make_fake_llm(
        actions,
        summary="Budget and need are confirmed - send the proposal and lock in a walkthrough meeting.",
        confidence=0.85,
    )
    next_step_agent_module.call_llm_json = fake_llm

    result = await run_next_step_agent(context, event, StubRetrievalService(_sample_chunks()))
    _print_result("2. Proposal -> meeting_completed (happy path)", result)


async def use_case_3_skip_closed_won():
    """Closed Won opportunity -> agent must skip."""
    context = _load_context("next_step_context_proposal.json")
    context = context.model_copy(update={"opportunity": context.opportunity.model_copy(update={"stage": "Closed Won"})})
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    result = await run_next_step_agent(context, event, StubRetrievalService(_sample_chunks()))
    _print_result("3. Closed Won -> skipped", result)


async def use_case_4_skip_no_company():
    """Opportunity with no linked company -> agent must skip."""
    context = _load_context("next_step_context_discovery.json")
    context = context.model_copy(update={"company": None})
    event = _make_event(FollowUpEventType.OPPORTUNITY_CREATED, context.opportunity.id)

    result = await run_next_step_agent(context, event, StubRetrievalService(_sample_chunks()))
    _print_result("4. No linked company -> skipped", result)


async def use_case_5_skip_ineligible_event():
    """task_completed is not in the Next Step routing table -> agent must skip."""
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.TASK_COMPLETED, context.opportunity.id)

    result = await run_next_step_agent(context, event, StubRetrievalService(_sample_chunks()))
    _print_result("5. task_completed event -> skipped (not eligible)", result)


async def use_case_6_retrieval_outage():
    """RAG backend is down -> agent should still produce recommendations."""
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    actions = [
        NextStepLLMActionItem(
            action_type="qualify_authority",
            title="Identify the decision maker",
            description="No contact is flagged as a decision maker yet.",
            priority=1,
            reasoning="BANT Authority is unqualified.",
            evidence=["No contact has is_decision_maker=true."],
            profile_fact_refs=[],
            suggested_crm_instruction="Create a task: Identify and engage the economic buyer",
        ),
    ]
    fake_llm = _make_fake_llm(actions, summary="RAG was unavailable; recommendation based on deal context only.", confidence=0.5)
    next_step_agent_module.call_llm_json = fake_llm

    # raise_error=True simulates retrieval backend being down for both queries.
    result = await run_next_step_agent(context, event, StubRetrievalService(raise_error=True))
    _print_result("6. Retrieval backend down -> graceful degradation", result)


async def use_case_7_llm_outage():
    """LLM provider call fails -> agent returns empty, non-skipped result."""
    context = _load_context("next_step_context_discovery.json")
    event = _make_event(FollowUpEventType.OPPORTUNITY_STAGE_CHANGED, context.opportunity.id)

    from agent.llm_client import LLMCallError

    next_step_agent_module.call_llm_json = _make_failing_llm(LLMCallError("provider timeout"))

    result = await run_next_step_agent(context, event, StubRetrievalService(_sample_chunks()))
    _print_result("7. LLM provider timeout -> graceful failure", result)


async def main():
    await use_case_1_discovery_stage_changed()
    await use_case_2_meeting_completed_proposal()
    await use_case_3_skip_closed_won()
    await use_case_4_skip_no_company()
    await use_case_5_skip_ineligible_event()
    await use_case_6_retrieval_outage()
    await use_case_7_llm_outage()


if __name__ == "__main__":
    asyncio.run(main())
