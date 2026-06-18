"""Interactive chat runner for the Next Step Intelligence Agent.

Pick a preset scenario or describe your own deal, then runs
run_next_step_agent and prints the recommendations.

Without a live LLM the agent's _run_llm_plan is replaced with a canned mock
that returns realistic NextStepLLMOutput for each preset. Set LLM_PROVIDER
+ your API key env vars to use a real model.

Run from packages/twenty-ai-service:

    python -m followup.next_step.agents.next_step.chat
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import followup.next_step.agents.next_step.next_step_agent as _module
from followup.next_step.agents.next_step.next_step_agent import run_next_step_agent
from followup.next_step.agents.next_step.schemas import NextStepLLMActionItem, NextStepLLMOutput
from followup.next_step.context.schemas import (
    CompanySnapshot,
    ContactSnapshot,
    DealContext,
    EngagementMetrics,
    OpportunitySnapshot,
    PipelineMeta,
    TimelineItem,
)
from followup.next_step.events.schemas import FollowUpEvent, FollowUpEventType

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

_CYAN = "\033[96m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _c(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _mock_plan(actions: list[NextStepLLMActionItem], summary: str, confidence: float = 0.8):
    """Return a coroutine that ignores messages and returns a canned output."""
    async def _fake(messages, *, model=None):
        return NextStepLLMOutput(actions=actions, summary_reasoning=summary, confidence=confidence)
    return _fake


# ---------------------------------------------------------------------------
# Preset scenarios
# ---------------------------------------------------------------------------


def _load(filename: str) -> DealContext:
    with open(FIXTURES / filename, encoding="utf-8") as f:
        return DealContext.model_validate(json.load(f))


def _event(etype: FollowUpEventType, opp_id: str) -> FollowUpEvent:
    return FollowUpEvent(
        event_id=str(uuid4()),
        idempotency_key=f"chat:{etype.value}:{opp_id}:{uuid4()}",
        event_type=etype,
        opportunity_id=opp_id,
        workspace_id="chat-workspace",
        user_id="chat-user",
        occurred_at=datetime.now(timezone.utc),
    )


PRESETS: list[dict] = [
    {
        "title": "Discovery deal — stage changed, overdue task, no decision maker",
        "description": "Acme Corp in Discovery. Overdue task, no budget info, no decision maker.",
        "loader": lambda: _load("next_step_context_discovery.json"),
        "event_type": FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
        "mock": _mock_plan(
            [
                NextStepLLMActionItem(
                    action_type="qualify_authority",
                    title="Identify the economic buyer",
                    description="No decision maker is linked — risk of late veto.",
                    priority=1,
                    reasoning="BANT Authority gap: no contact is flagged is_decision_maker=true.",
                    evidence=["No contact has is_decision_maker=true.", "Playbook: confirm DM before proposing."],
                    profile_fact_refs=["fact-1"],
                    orchestrator_tool="create_task",
                    orchestrator_instruction="Create a task on opportunity opp-1001: Identify and engage the economic buyer",
                ),
                NextStepLLMActionItem(
                    action_type="create_task",
                    title="Resolve overdue discovery call task",
                    description="Reschedule the stalled discovery call.",
                    priority=2,
                    reasoning="Overdue task signals stalled momentum in Discovery.",
                    evidence=["Task 'Schedule discovery call with operations team' is OVERDUE."],
                    profile_fact_refs=[],
                    orchestrator_tool="create_task",
                    orchestrator_instruction="Reschedule the overdue task on opportunity opp-1001",
                ),
                NextStepLLMActionItem(
                    action_type="qualify_budget",
                    title="Qualify the budget",
                    description="No budget range has been discussed.",
                    priority=3,
                    reasoning="bant_budget_status = missing. Budget must be confirmed before moving to Proposal.",
                    evidence=["profile fact: bant_budget_status = missing — no budget range discussed yet."],
                    profile_fact_refs=["fact-1"],
                    orchestrator_tool="create_task",
                    orchestrator_instruction="Create a task on opportunity opp-1001: Discuss and document budget range",
                ),
            ],
            summary="Three blockers before this deal can advance: DM unknown, overdue task, budget not discussed.",
            confidence=0.82,
        ),
    },
    {
        "title": "Proposal deal — meeting just completed, budget confirmed",
        "description": "Globex Inc in Proposal stage. Budget confirmed, decision maker known.",
        "loader": lambda: _load("next_step_context_proposal.json"),
        "event_type": FollowUpEventType.MEETING_COMPLETED,
        "mock": _mock_plan(
            [
                NextStepLLMActionItem(
                    action_type="send_proposal",
                    title="Send the proposal to Priya Shah",
                    description="Budget and need confirmed — unblock by sending the proposal today.",
                    priority=1,
                    reasoning="All Proposal-stage gates cleared: DM engaged, budget $75k-$90k, Q3 deadline.",
                    evidence=["Fact: bant_budget_status = confirmed $75k-$90k.", "Contact: Priya Shah is decision maker."],
                    profile_fact_refs=["fact-2", "fact-3"],
                    orchestrator_tool="send_email",
                    orchestrator_instruction="Send the proposal to Priya Shah on opportunity opp-2002",
                ),
                NextStepLLMActionItem(
                    action_type="schedule_meeting",
                    title="Book proposal walkthrough call",
                    description="No future meeting is booked — lock in a review call.",
                    priority=2,
                    reasoning="has_future_meeting=false; keeping momentum requires a booked next touchpoint.",
                    evidence=["Engagement: has_future_meeting = false"],
                    profile_fact_refs=[],
                    orchestrator_tool="schedule_meeting",
                    orchestrator_instruction="Schedule a meeting on opportunity opp-2002 with Priya Shah to review the proposal",
                ),
            ],
            summary="Budget and authority confirmed — send the proposal now and lock in a walkthrough call.",
            confidence=0.91,
        ),
    },
    {
        "title": "Closed Won — agent should skip",
        "description": "Deal already won. Agent must skip without making any recommendations.",
        "loader": lambda: _closed_context("Closed Won"),
        "event_type": FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
        "mock": None,
    },
    {
        "title": "Closed Lost — agent should skip",
        "description": "Deal already lost. Agent must skip.",
        "loader": lambda: _closed_context("Closed Lost"),
        "event_type": FollowUpEventType.MEETING_COMPLETED,
        "mock": None,
    },
    {
        "title": "No linked company — agent should skip",
        "description": "Opportunity has no company attached. Agent skips.",
        "loader": lambda: _no_company_context(),
        "event_type": FollowUpEventType.OPPORTUNITY_CREATED,
        "mock": None,
    },
    {
        "title": "Ineligible event type (task_completed) — agent should skip",
        "description": "task_completed events are not routed to the Next Step agent.",
        "loader": lambda: _load("next_step_context_discovery.json"),
        "event_type": FollowUpEventType.TASK_COMPLETED,
        "mock": None,
    },
]


def _closed_context(stage: str) -> DealContext:
    ctx = _load("next_step_context_proposal.json")
    return ctx.model_copy(update={"opportunity": ctx.opportunity.model_copy(update={"stage": stage})})


def _no_company_context() -> DealContext:
    ctx = _load("next_step_context_discovery.json")
    return ctx.model_copy(update={"company": None})


# ---------------------------------------------------------------------------
# Custom scenario builder
# ---------------------------------------------------------------------------

STAGES = ["Discovery", "Qualification", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]
EVENT_TYPES = [
    FollowUpEventType.OPPORTUNITY_CREATED,
    FollowUpEventType.OPPORTUNITY_STAGE_CHANGED,
    FollowUpEventType.MEETING_COMPLETED,
    FollowUpEventType.TASK_COMPLETED,
    FollowUpEventType.EMAIL_SENT,
]


def _pick(prompt: str, options: list[str]) -> int:
    print(f"\n{_c(prompt, _BOLD)}")
    for i, o in enumerate(options, 1):
        print(f"  {_c(str(i), _CYAN)}. {o}")
    while True:
        raw = input(_c("  → ", _CYAN)).strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(_c("  Please enter a number from the list.", _RED))


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"  {_c(prompt + hint + ': ', _CYAN)}").strip()
    return val or default


def _build_custom_context() -> tuple[DealContext, FollowUpEventType]:
    print(_c("\n── Build your scenario ──────────────────────────────────────────────", _DIM))

    opp_name = _ask("Deal name", "My Test Deal")
    stage_idx = _pick("Stage", STAGES)
    stage = STAGES[stage_idx]
    amount_raw = _ask("Amount (leave blank to skip)", "")
    amount = float(amount_raw) if amount_raw else None

    company_name = _ask("Company name (leave blank = no company)", "Acme Corp")
    company = CompanySnapshot(id="co-1", name=company_name) if company_name else None

    contact_name = _ask("Primary contact name", "Alex Kim")
    is_dm = _ask("Is this contact a decision maker? (y/n)", "n").lower() == "y"
    contacts = (
        [ContactSnapshot(id="ct-1", name=contact_name, role="Champion", is_decision_maker=is_dm)]
        if contact_name else []
    )

    has_overdue = _ask("Add an overdue task? (y/n)", "n").lower() == "y"
    tasks = []
    if has_overdue:
        from followup.next_step.context.schemas import TaskSnapshot
        tasks = [TaskSnapshot(
            id="task-1", title="Follow up with client", status="open",
            due_at=datetime(2024, 1, 1, tzinfo=timezone.utc), is_overdue=True,
        )]

    timeline_note = _ask("One-line timeline note (leave blank to skip)", "")
    timeline = []
    if timeline_note:
        timeline = [TimelineItem(
            type="note", title="Note",
            summary=timeline_note, occurred_at=datetime.now(timezone.utc),
        )]

    budget_known = _ask("Is the budget known? (y/n)", "n").lower() == "y"
    from followup.next_step.context.schemas import FactCategory, ProfileFact
    active_facts = [
        ProfileFact(
            fact_id="f-1",
            category=FactCategory.DEAL_PROGRESSION,
            fact_key="bant_budget_status",
            value="confirmed" if budget_known else "missing",
        )
    ]

    event_idx = _pick("Event type that triggered this run", [e.value for e in EVENT_TYPES])
    event_type = EVENT_TYPES[event_idx]

    opp = OpportunitySnapshot(id=f"opp-{uuid4().hex[:6]}", name=opp_name, stage=stage, amount=amount)
    engagement = EngagementMetrics(
        days_since_last_activity=5, activity_count_14d=3,
        activity_count_prior_14d=4, has_future_meeting=False,
    )
    ctx = DealContext(
        opportunity=opp, company=company, contacts=contacts,
        timeline=timeline, tasks=tasks, meetings=[],
        pipeline_meta=PipelineMeta(),
        engagement=engagement, active_facts=active_facts,
        loaded_at=datetime.now(timezone.utc),
    )
    return ctx, event_type


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------


def _print_result(result) -> None:
    if result.skipped:
        print(_c("\n⏭  SKIPPED", _YELLOW))
        print(f"   Reason: {result.skip_reason}")
        return

    if not result.recommended_actions:
        print(_c("\n⚠  No recommendations (LLM call failed or returned nothing)", _RED))
        print(f"   Summary: {result.summary_reasoning}")
        return

    print(_c(
        f"\n✓ {len(result.recommended_actions)} recommendation(s)  "
        f"(confidence {result.confidence:.0%})",
        _GREEN,
    ))
    print(_c(f"   Summary: {result.summary_reasoning}\n", _DIM))

    for i, action in enumerate(result.recommended_actions, 1):
        print(_c(f"  [{i}] Priority {action.priority}  ·  {action.action_type}", _BOLD))
        print(f"       Title      : {action.title}")
        print(f"       Description: {action.description}")
        print(f"       Reasoning  : {action.reasoning}")
        for e in action.evidence:
            print(f"       Evidence   : • {e}")
        oa = action.orchestrator_action
        print(_c(f"       CRM Action : [{oa.tool}] {oa.instruction}", _CYAN))
        print()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

USE_REAL_LLM = bool(os.getenv("LLM_PROVIDER"))


async def _run_scenario(ctx: DealContext, etype: FollowUpEventType, mock) -> None:
    event = _event(etype, ctx.opportunity.id)
    if not USE_REAL_LLM and mock is not None:
        _module._run_llm_plan = mock
    result = await run_next_step_agent(ctx, event)
    _print_result(result)


async def main() -> None:
    print(_c("\n==========================================================", _CYAN))
    print(_c("  Next Step Intelligence Agent  —  Interactive Tester", _CYAN))
    print(_c("==========================================================", _CYAN))
    if USE_REAL_LLM:
        print(_c(f"  LLM: {os.getenv('LLM_PROVIDER')} / {os.getenv('LLM_MODEL', 'default')}", _GREEN))
    else:
        print(_c("  LLM: mocked (set LLM_PROVIDER env var to use a real model)", _YELLOW))

    while True:
        print(_c("\n── Choose a scenario ────────────────────────────────────────────────", _DIM))
        for i, p in enumerate(PRESETS, 1):
            tag = _c("SKIP", _YELLOW) if p["mock"] is None else _c("✓", _GREEN)
            print(f"  {_c(str(i), _CYAN)}. [{tag}] {p['title']}")
        print(f"  {_c('c', _CYAN)}. Build a custom scenario")
        print(f"  {_c('q', _CYAN)}. Quit")

        choice = input(_c("\n  → ", _CYAN)).strip().lower()

        if choice == "q":
            print(_c("\nBye!\n", _DIM))
            break
        elif choice == "c":
            ctx, etype = _build_custom_context()
            print(_c(f"\n▶ Running custom scenario  [{etype.value}]  stage={ctx.opportunity.stage} ...", _DIM))
            await _run_scenario(ctx, etype, mock=None)
        elif choice.isdigit() and 1 <= int(choice) <= len(PRESETS):
            preset = PRESETS[int(choice) - 1]
            ctx = preset["loader"]()
            etype = preset["event_type"]
            print(_c(f"\n▶ {preset['title']}", _BOLD))
            print(_c(f"  {preset['description']}", _DIM))
            print(_c(f"  event: {etype.value}  ·  stage: {ctx.opportunity.stage}\n", _DIM))
            await _run_scenario(ctx, etype, preset["mock"])
        else:
            print(_c("  Invalid choice, try again.", _RED))


if __name__ == "__main__":
    asyncio.run(main())
