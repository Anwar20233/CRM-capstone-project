"""Follow-up task registry — the hot-loaded "known steps".

This is the follow-up-domain parallel to the agent-discovery trio
(``agent/agent_scope.py`` + ``agent/agent_registry.py`` + ``agent/agent_tools.py``).
Where those let an agent discover and activate *sub-agents*, these let the
follow-up agent discover and run its *tasks* — the known steps the next-step
agent recommends (check the calendar, draft an email, write a note):

    get_agent_catalog  ->  get_task_catalog   (what tasks exist + their role)
    learn_agent        ->  learn_task          (the task's custom instructions)
    delegate_to_agent  ->  execute_task        (run the task)

The custom instructions live on each ``FollowupTaskSpec`` and are returned by
``learn_task`` — that is the "hot load": the agent only pulls a task's
instructions when the recommendation calls for it. Realized in-process; the same
shape can later be backed by a Node-bridge follow-up endpoint behind these tools.

``build_task_tools`` exposes the meta-tools for a future *agentic* follow-up
worker. The Step-6 graph instead dispatches the registry deterministically (see
``orchestrator/nodes.py``), but both go through the same registry + scope guard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from langchain_core.tools import StructuredTool

from followup.calendar.availability import CalendarResult, check_availability
from followup.contracts.drafting import DRAFT_TONE_TYPES, DraftRequest

if TYPE_CHECKING:
    from followup.contracts.next_step import NextStepPlan
    from followup.contracts.risk import RiskAssessment
    from followup.orchestrator.deps import OrchestratorDeps
    from followup.profile.schemas import DealContext


# ===========================================================================
# Scope — which tasks the follow-up agent may discover and run
# ===========================================================================


@dataclass(frozen=True)
class FollowupTaskScope:
    """Allow-list of task names (mirrors ``agent_scope.AgentScope``)."""

    name: str
    allowed_tasks: frozenset[str]


def is_task_allowed(task_name: str, scope: FollowupTaskScope) -> bool:
    """Case-insensitive membership, so model casing ("Draft_Email") still resolves."""
    allowed_lower = {name.lower() for name in scope.allowed_tasks}
    return task_name.lower() in allowed_lower


FOLLOWUP_SCOPE = FollowupTaskScope(
    name="followup",
    allowed_tasks=frozenset(
        {"check_calendar", "draft_email", "write_note", "create_task"}
    ),
)


# ===========================================================================
# Task spec + registry (mirror agent_registry.AgentSpec / AgentRegistry)
# ===========================================================================


@dataclass
class TaskContext:
    """Everything a task handler needs, assembled per dispatch.

    Carries the accumulating run state so a later task reads an earlier one's
    output — e.g. ``draft_email`` reads the ``calendar`` result ``check_calendar``
    produced. ``instructions`` is the task's own hot-loaded instruction text.
    """

    state: dict[str, Any]
    deal_context: "DealContext"
    plan: "NextStepPlan"
    instructions: str
    classification: dict[str, Any] = field(default_factory=dict)
    risk_assessment: Optional["RiskAssessment"] = None
    calendar: Optional[CalendarResult] = None

    def step_intent(self, *kinds: str) -> str:
        """The intent of the first plan step matching one of ``kinds`` (or "")."""
        for step in self.plan.steps:
            if step.kind in kinds:
                return step.intent
        return ""


TaskHandler = Callable[[TaskContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class FollowupTaskSpec:
    """One discoverable follow-up task."""

    name: str
    role: str  # one-line catalog description (get_task_catalog)
    when_to_use: str  # routing cue: which next_step action_types map here
    instructions: str  # the custom instructions, hot-loaded by learn_task
    input_schema: dict[str, Any]
    handler: TaskHandler

    def catalog_entry(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role, "when_to_use": self.when_to_use}

    def learn_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "when_to_use": self.when_to_use,
            "instructions": self.instructions,
            "input_schema": self.input_schema,
        }


@dataclass
class FollowupTaskRegistry:
    """An in-process catalog of ``FollowupTaskSpec`` objects, keyed by name."""

    _tasks: dict[str, FollowupTaskSpec] = field(default_factory=dict)

    def register(self, spec: FollowupTaskSpec) -> None:
        self._tasks[spec.name.lower()] = spec

    def get(self, name: str) -> Optional[FollowupTaskSpec]:
        return self._tasks.get(name.lower())

    def catalog(self, scope: FollowupTaskScope) -> list[dict[str, str]]:
        return [
            spec.catalog_entry()
            for spec in self._tasks.values()
            if is_task_allowed(spec.name, scope)
        ]

    def learn(
        self, names: list[str], scope: FollowupTaskScope
    ) -> list[dict[str, Any]]:
        entries = []
        for name in names:
            spec = self._tasks.get(name.lower())
            if spec is not None and is_task_allowed(spec.name, scope):
                entries.append(spec.learn_entry())
        return entries


# ===========================================================================
# Meta-tools (mirror agent_tools.build_agent_tools) — the agentic surface
# ===========================================================================


def build_task_tools(
    registry: FollowupTaskRegistry, scope: FollowupTaskScope
) -> list[StructuredTool]:
    """Build the three task-discovery meta-tools, closed over (registry, scope)."""

    async def _get_task_catalog() -> dict:
        """List the follow-up tasks you can run (name + role).

        Call learn_task next to get a task's instructions before executing it.
        """
        return {"ok": True, "data": {"tasks": registry.catalog(scope)}}

    async def _learn_task(task_names: list[str]) -> dict:
        """Fetch the custom instructions + input schema for specific tasks."""
        blocked = [n for n in task_names if not is_task_allowed(n, scope)]
        if blocked:
            return {
                "ok": False,
                "error": {
                    "code": "OUT_OF_SCOPE",
                    "message": (
                        f"Tasks not available in '{scope.name}' scope: "
                        + ", ".join(blocked)
                    ),
                },
            }
        return {"ok": True, "data": {"tasks": registry.learn(task_names, scope)}}

    async def _execute_task(task: str, context: TaskContext) -> dict:
        """Run a follow-up task against an assembled TaskContext."""
        if not is_task_allowed(task, scope):
            return {
                "ok": False,
                "error": {
                    "code": "OUT_OF_SCOPE",
                    "message": f"Task '{task}' is not available in '{scope.name}' scope",
                },
            }
        spec = registry.get(task)
        if spec is None:
            return {
                "ok": False,
                "error": {
                    "code": "UNKNOWN_TASK",
                    "message": f"No task named '{task}' is registered",
                },
            }
        try:
            result = await spec.handler(context)
        except Exception as error:  # noqa: BLE001
            # A task failure becomes a recoverable result, not a crashed turn
            # (mirrors agent_tools.delegate_to_agent / BaseWorker.invoke_tool).
            return {
                "ok": False,
                "error": {
                    "code": "TASK_FAILED",
                    "message": f"Task '{task}' failed: {error}",
                },
            }
        return {"ok": True, "data": result}

    return [
        StructuredTool.from_function(coroutine=_get_task_catalog, name="get_task_catalog"),
        StructuredTool.from_function(coroutine=_learn_task, name="learn_task"),
        StructuredTool.from_function(coroutine=_execute_task, name="execute_task"),
    ]


# ===========================================================================
# Default registry — the three known steps, handlers closed over deps
# ===========================================================================


def _recipient(deal_context: "DealContext") -> Optional[str]:
    """First contact with an email — the draft's default recipient."""
    return next((c.email for c in deal_context.contacts if c.email), None)


def _write_targets(deal_context: "DealContext") -> list[dict[str, Any]]:
    """Resolved ``{type, id, name}`` references the writer addresses a write to.

    The follow-up agent never writes to the CRM itself: a write is routed to the
    CRM orchestrator, which delegates to the writer. The writer needs to know
    *where* to write, so we hand it ids paired with the human-readable name of
    what each id is (opportunity, the deal's contacts) — never a bare id.
    """
    targets: list[dict[str, Any]] = [
        {
            "type": "opportunity",
            "id": deal_context.opportunity_id,
            "name": deal_context.opportunity_name,
        }
    ]
    targets += [
        {"type": "person", "id": contact.crm_id, "name": contact.name}
        for contact in deal_context.contacts
        if contact.crm_id
    ]
    return targets


def infer_tone(deal_context: "DealContext") -> str:
    """Guess draft formality from the deal. Defaults to professional.

    A high risk score warrants urgency; otherwise stay professional. Kept simple
    and deterministic — the real P4 agent will refine tone from the narrative.
    """
    if (deal_context.risk_score or 0.0) >= 0.7:
        return "urgent"
    return "professional"


# The next-step plan's headline action → the drafter's email-"type" token. On the
# email path there is no LLM classifier; the planner's intent is what should shape
# the drafter's manner, so we translate the plan into the same {type, urgency}
# signal the drafter already derives tone + draft-type from. This keeps the
# planner — not a pre-classifier — in charge of how the communication reads.
_HEADLINE_TO_DRAFT_SIGNAL: dict[str, str] = {
    "escalate": "objection",          # urgent framing
    "send_proposal": "buying_signal",  # consultative + proposal-delivery framing
    "close_deal": "buying_signal",
    "schedule_meeting": "meeting_request",  # consultative
    "check_in": "info_sharing",        # casual
}


def plan_to_draft_signal(plan: "NextStepPlan") -> dict[str, Any]:
    """Derive the drafter's {type, urgency} tone signal from the next-step plan.

    Used on the email path, where no classifier runs: the planner's headline
    action sets the email framing/tone and its top step's priority sets urgency.
    """
    signal: dict[str, Any] = {}
    email_type = _HEADLINE_TO_DRAFT_SIGNAL.get(plan.headline_action)
    if email_type:
        signal["type"] = email_type
    if plan.steps:
        signal["urgency"] = plan.steps[0].priority
    return signal


assert "professional" in DRAFT_TONE_TYPES and "urgent" in DRAFT_TONE_TYPES


def build_default_task_registry(deps: "OrchestratorDeps") -> FollowupTaskRegistry:
    """Register check_calendar / draft_email / write_note, bound to ``deps``."""

    async def _check_calendar(ctx: TaskContext) -> dict[str, Any]:
        # check_calendar only runs for a meeting step, so when the email proposed
        # no times we proactively offer the rep's next free slots — the draft then
        # proposes real, calendar-verified times instead of inventing them.
        trigger = ctx.state.get("trigger") or {}
        proposed = trigger.get("proposed_times") or (ctx.plan.metadata or {}).get("proposed_times") or []
        result = await check_availability(
            calendar_reader=deps.pipeline.calendar_reader,
            owner_user_id=trigger.get("owner_user_id"),
            workspace_id=ctx.state["workspace_id"],
            proposed_times=proposed,
            duration_minutes=30,
            find_slots_when_empty=True,
        )
        return {"calendar": result}

    async def _draft_email(ctx: TaskContext) -> dict[str, Any]:
        # Thread the calendar slots (proposed + alternatives) into the draft so
        # the email offers concrete times. check_calendar always runs first when
        # both are planned (see STEP_PREP ordering).
        available_slots = None
        cal = ctx.calendar
        if cal and cal.available_slots:
            available_slots = [asdict(s) for s in cal.available_slots]
            if cal.all_busy and cal.suggested_alternatives:
                available_slots += [asdict(s) for s in cal.suggested_alternatives]

        # For email triggers, pass the inbound email as reply context so the
        # draft engine addresses the sender and references their points.
        trigger = ctx.state.get("trigger") or {}
        reply_context = None
        if ctx.state.get("entry_point") == "email" and trigger.get("sender_email"):
            reply_context = {
                "sender_email": trigger["sender_email"],
                "sender_name": trigger.get("sender_name"),
                "subject": trigger.get("subject"),
                "body": trigger.get("body"),
            }

        # On a revise re-run the prior draft is threaded in (the rep's edit
        # instructions reach the plan via the trigger) so the drafting agent
        # revises the earlier email instead of writing a fresh one.
        prior_draft = trigger.get("prior_draft")
        previous_draft = prior_draft.get("body") if isinstance(prior_draft, dict) else None

        # Send the drafter content + context only — it owns tone/template. We do
        # NOT forward the next-step agent's wording: the follow-up agent authors
        # the directive itself. A meeting step gets a minimal people+reason brief
        # (which also becomes the calendar event title at accept time); any other
        # email step gets a "why + what" directive.
        is_meeting = bool(ctx.step_intent("book_meeting")) and not ctx.step_intent("draft_email")
        directive = (
            deps.content_author.meeting_context(ctx)
            if is_meeting
            else deps.content_author.email_directive(ctx)
        )
        # The drafter derives tone + framing from this signal. On the email path
        # there is no classifier, so the next-step agent's plan supplies it — the
        # planner, not a pre-classifier, decides how the communication reads.
        draft_signal = ctx.classification or plan_to_draft_signal(ctx.plan)
        request = DraftRequest(
            deal_context=ctx.deal_context,
            intent=directive,
            classification=draft_signal,
            mode="single",
            recipient_email=_recipient(ctx.deal_context),
            available_slots=available_slots,
            reply_context=reply_context,
            previous_draft=previous_draft,
        )
        result = await deps.agents.drafting.run(request)
        out: dict[str, Any] = {"draft": result}
        # Persist the authored meeting brief so the calendar event title at accept
        # time is the agent's "names + reason", never the next-step agent's words.
        if is_meeting:
            out["task_results"] = {"book_meeting": {"title": directive}}
        return out

    async def _write_note(ctx: TaskContext) -> dict[str, Any]:
        # A deferred write: shape the note payload for the pending action. The
        # follow-up agent does NOT write to the CRM — execution is routed to the
        # CRM orchestrator, which delegates to the writer, and only after the rep
        # accepts (Step 7). We hand the writer resolved id+name targets so it can
        # locate exactly where to write. Nothing is written here.
        #
        # The note body is authored HERE by the follow-up agent's own note LLM —
        # the next-step agent only chose that a note should exist, not its words.
        payload = {
            "body": await deps.content_author.author_note(ctx),
            "instructions": ctx.instructions,
            "targets": _write_targets(ctx.deal_context),
            "route": "orchestrator->writer",
        }
        return {"task_results": {"write_note": payload}}

    async def _create_task(ctx: TaskContext) -> dict[str, Any]:
        # Same deferred-write shape as _write_note, but the task TITLE is authored
        # by the follow-up agent's own task LLM (a separate prompt from notes).
        # Authoring at plan time means the title is in the pending action the rep
        # reviews before accepting.
        payload = {
            "title": await deps.content_author.author_task(ctx),
            "instructions": ctx.instructions,
            "targets": _write_targets(ctx.deal_context),
            "route": "orchestrator->writer",
        }
        return {"task_results": {"create_task": payload}}

    registry = FollowupTaskRegistry()
    registry.register(
        FollowupTaskSpec(
            name="check_calendar",
            role="Checks the rep's calendar availability for proposed meeting times.",
            when_to_use="When the next step is schedule_meeting, before drafting.",
            instructions=(
                "Read the rep's calendar around the proposed times. If every "
                "proposed time is busy, offer the next free business-hours slots. "
                "Never book — availability only; the rep confirms later."
            ),
            input_schema={"proposed_times": "list[str]", "duration_minutes": "int"},
            handler=_check_calendar,
        )
    )
    registry.register(
        FollowupTaskSpec(
            name="draft_email",
            role="Drafts a ready-to-review follow-up email for the recommendation.",
            when_to_use="For most next steps that reach out to the contact.",
            instructions=(
                "Write a concise, action-oriented email that advances the deal. "
                "Match the recommendation's intent and tone. If calendar slots "
                "are available, offer them as concrete options. Do not send — the "
                "draft is for rep review."
            ),
            input_schema={"tone": "str", "available_slots": "list[dict] | None"},
            handler=_draft_email,
        )
    )
    registry.register(
        FollowupTaskSpec(
            name="write_note",
            role="Shapes an internal CRM note capturing the recommendation.",
            when_to_use="When the deal needs an internal record rather than outreach.",
            instructions=(
                "Summarize the recommendation as an internal note on the "
                "opportunity. This is a deferred write — only the payload is "
                "shaped here; creation happens after the rep accepts."
            ),
            input_schema={"body": "str"},
            handler=_write_note,
        )
    )
    registry.register(
        FollowupTaskSpec(
            name="create_task",
            role="Shapes an actionable follow-up task for the rep.",
            when_to_use="When the deal needs a concrete to-do rather than outreach.",
            instructions=(
                "Turn the recommendation into a single actionable task on the "
                "opportunity. This is a deferred write — only the payload is "
                "shaped here; creation happens after the rep accepts."
            ),
            input_schema={"title": "str"},
            handler=_create_task,
        )
    )
    return registry


__all__ = [
    "FollowupTaskScope",
    "FOLLOWUP_SCOPE",
    "is_task_allowed",
    "TaskContext",
    "TaskHandler",
    "FollowupTaskSpec",
    "FollowupTaskRegistry",
    "build_task_tools",
    "build_default_task_registry",
    "infer_tone",
    "plan_to_draft_signal",
]
