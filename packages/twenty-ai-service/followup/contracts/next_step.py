"""P2 — Next Step contracts: request + the action PLAN + agent interface.

The Next Step agent is the planner. Given a slice of the deal's profile, the
activation cause (the trigger), the synthesized narrative, and the email
classification, it returns a ``NextStepPlan`` — an ordered list of high-level,
actionable ``PlannedStep`` intents ("draft an email", "write a note", "create a
task", "book a meeting", "advance the stage"). The follow-up orchestrator then
*follows the plan*, expanding each intent into the concrete task/tool calls.

Planning intelligence (how this company works) lives HERE, in the plan; the
orchestrator only does the mechanical dispatch. ``MockNextStepAgent`` is the
stand-in until the real agent ships — it returns a correctly-shaped plan derived
from real DealContext signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from followup.contracts.events import EmailSignalEvent
from followup.profile.schemas import DealContext

# The bundled pending action's headline action_type (DB-valid, ∈ this set).
NEXT_STEP_TYPES: frozenset[str] = frozenset(
    {
        "follow_up_call",
        "send_proposal",
        "check_in",
        "escalate",
        "close_deal",
        "schedule_meeting",
        "no_action",
    }
)

# The kinds of actionable step the planner can emit — each maps to a follow-up
# capability (a prep task at plan time + a writer tool at accept time).
STEP_KINDS: frozenset[str] = frozenset(
    {
        "draft_email",
        "write_note",
        "create_task",
        "book_meeting",
        "update_stage",
    }
)

NEXT_STEP_MODES: frozenset[str] = frozenset({"single", "sweep"})
PRIORITY_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})


@dataclass
class PlannedStep:
    """One high-level, actionable intent the follow-up agent must carry out.

    ``kind`` selects the capability (∈ STEP_KINDS); ``intent`` is the
    natural-language goal the orchestrator/sub-agent expands into concrete
    params at execution time (we deliberately do NOT resolve params here).
    """

    kind: str  # ∈ STEP_KINDS
    intent: str  # high-level goal, e.g. "reassure on the Q3 timeline"
    priority: str = "medium"  # ∈ PRIORITY_LEVELS
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NextStepPlan:
    """Output of the Next Step agent — an ordered plan of actionable steps."""

    steps: list[PlannedStep]
    headline_action: str  # ∈ NEXT_STEP_TYPES — the bundled pending action's type
    summary: str  # one-line rationale for the whole plan
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NextStepRequest:
    """Input bundle the orchestrator sends to the Next Step agent.

    The planner gets the deal picture (``deal_context``), the activation cause
    (``trigger`` + ``trigger_type``), the synthesized ``narrative``, and the
    email ``classification`` — everything it needs to decide the plan without
    reading the CRM itself.
    """

    deal_context: DealContext
    trigger_type: str  # ∈ SIGNAL_TYPES ("email_signal" | "manual" | "scheduled")
    trigger: EmailSignalEvent | None = None
    narrative: str | None = None
    classification: dict[str, Any] | None = None
    mode: str = "single"  # ∈ NEXT_STEP_MODES


@runtime_checkable
class NextStepAgent(Protocol):
    async def run(self, request: NextStepRequest) -> NextStepPlan: ...


class MockNextStepAgent:
    """Stand-in planner; returns a correctly-shaped plan from real deal signals.

    The plan mirrors a simple company playbook: at-risk deals get a reassurance
    email + an escalation note + a chase task; open concerns get a reassurance
    email; meeting requests get a booking; quiet deals get a check-in; otherwise
    propose a meeting.
    """

    async def run(self, request: NextStepRequest) -> NextStepPlan:
        ctx = request.deal_context
        classification = request.classification or {}
        risk = ctx.risk_score or 0.0
        concern = (
            ctx.open_concerns[0].get("content", "an open concern")
            if ctx.open_concerns
            else None
        )

        steps: list[PlannedStep] = []
        if risk >= 0.7:
            headline = "escalate"
            steps.append(PlannedStep(
                kind="draft_email",
                intent=f"Reassure {ctx.opportunity_name} on the open risk: {concern or 'recent concerns'}.",
                priority="high",
            ))
            steps.append(PlannedStep(
                kind="write_note",
                intent=f"Log the escalation context on {ctx.opportunity_name} for the account team.",
                priority="high",
            ))
            steps.append(PlannedStep(
                kind="create_task",
                intent=f"Chase the outstanding items on {ctx.opportunity_name} before the deal slips.",
                priority="high",
            ))
        elif concern:
            headline = "follow_up_call"
            steps.append(PlannedStep(
                kind="draft_email",
                intent=f"Address the open concern on {ctx.opportunity_name}: {concern}.",
                priority="medium",
            ))
        elif classification.get("requires_calendar") or classification.get("type") == "meeting_request":
            headline = "schedule_meeting"
            steps.append(PlannedStep(
                kind="book_meeting",
                intent=f"Offer times to meet about {ctx.opportunity_name}.",
                priority="medium",
            ))
        elif ctx.recent_activities:
            headline = "check_in"
            steps.append(PlannedStep(
                kind="draft_email",
                intent=f"Light check-in to keep {ctx.opportunity_name} moving forward.",
                priority="low",
            ))
        else:
            headline = "schedule_meeting"
            steps.append(PlannedStep(
                kind="book_meeting",
                intent=f"Propose an intro meeting for {ctx.opportunity_name}.",
                priority="medium",
            ))

        return NextStepPlan(
            steps=steps,
            headline_action=headline,
            summary=(
                f"Deal '{ctx.opportunity_name}' at stage {ctx.deal_stage} "
                f"(risk {risk:.2f}): {len(steps)}-step follow-up plan."
            ),
            metadata={
                "opportunity_id": ctx.opportunity_id,
                "trigger_type": request.trigger_type,
                "recipient_email": ctx.contacts[0].email if ctx.contacts else None,
            },
        )


async def run_next_step(
    request: NextStepRequest, *, agent: NextStepAgent | None = None
) -> NextStepPlan:
    return await (agent or MockNextStepAgent()).run(request)


async def mock_run_next_step(request: NextStepRequest) -> NextStepPlan:
    return await MockNextStepAgent().run(request)


__all__ = [
    "PlannedStep",
    "NextStepPlan",
    "NextStepRequest",
    "NextStepAgent",
    "MockNextStepAgent",
    "NEXT_STEP_TYPES",
    "STEP_KINDS",
    "NEXT_STEP_MODES",
    "PRIORITY_LEVELS",
    "run_next_step",
    "mock_run_next_step",
]
