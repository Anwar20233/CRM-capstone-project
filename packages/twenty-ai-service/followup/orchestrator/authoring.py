"""Content authoring for the follow-up agent — the agent writes its OWN content.

The next-step agent only *decides which steps* to take (the ``kind`` of each
``PlannedStep``); it does NOT author the words that go into a note, task, email
or meeting. That authoring is the follow-up agent's job and lives here.

Two small LLM authors, each with its own prompt:
* :meth:`ContentAuthor.author_note` — an internal CRM note body.
* :meth:`ContentAuthor.author_task` — a single actionable task title.

Email and meeting do NOT get their own LLM here: the Draft agent owns email
prose, so we only hand it a *directive* (why we're emailing + what we want); a
meeting invite is kept deliberately minimal (the people + the reason).

PII discipline mirrors the classify node: names/emails are masked before they
reach our LLM and unmasked on the way out, so the author LLM never sees real
people while the stored note/task still reads naturally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from langchain_core.messages import HumanMessage

from followup.profile.masking import ProfileMasker

if TYPE_CHECKING:
    from followup.orchestrator.deps import OrchestratorDeps
    from followup.orchestrator.tasks import TaskContext


# What the headline action is trying to achieve — the "what we want to do" half
# of an email directive. Keyed by NEXT_STEP_TYPES.
_ACTION_GOAL: dict[str, str] = {
    "follow_up_call": "set up a follow-up conversation",
    "send_proposal": "move the proposal forward",
    "check_in": "check in and keep the deal warm",
    "escalate": "address the risk and reassure them quickly",
    "close_deal": "push toward closing the deal",
    "schedule_meeting": "get a meeting on the calendar",
    "no_action": "stay in touch",
}

# Why we're reaching out — the "why we're emailing" half, from the email triage.
_REASON_BY_TYPE: dict[str, str] = {
    "objection": "they raised an objection",
    "buying_signal": "they showed a buying signal",
    "meeting_request": "they asked to meet",
    "question": "they asked a question",
    "info_sharing": "they shared an update",
    "risk_alert": "the deal is flagged at risk",
    "direct_send": "the rep asked us to reach out",
}


_NOTE_PROMPT = """You are a sales-operations assistant writing an INTERNAL CRM note on a deal.
Write a concise note (2-4 sentences) that captures what just happened and what
the rep should be aware of next. Be factual and specific. Do NOT add a greeting,
a sign-off, or a subject line — only the note body.

Deal context:
{context}

Note body:"""

_TASK_PROMPT = """You write ONE actionable follow-up task for a sales rep on a deal.
Output a single imperative task title on one line (no more than ~15 words). It
must be concrete and start with a verb. No greeting, no numbering, no quotes —
just the task title.

Deal context:
{context}

Task title:"""


class ContentAuthor:
    """Authors the content the follow-up agent puts into each plan step."""

    def __init__(self, deps: "OrchestratorDeps") -> None:
        self._deps = deps

    # -- LLM authors -----------------------------------------------------

    async def author_note(self, ctx: "TaskContext") -> str:
        """LLM-author an internal note body from the deal + trigger context."""
        masker = self._masker(ctx)
        prompt = _NOTE_PROMPT.format(
            context=self._context_block(ctx, masker, "write_note")
        )
        return await self._generate(prompt, masker, fallback=self._note_fallback(ctx))

    async def author_task(self, ctx: "TaskContext") -> str:
        """LLM-author a single actionable task title from the deal + trigger context."""
        masker = self._masker(ctx)
        prompt = _TASK_PROMPT.format(
            context=self._context_block(ctx, masker, "create_task")
        )
        title = await self._generate(prompt, masker, fallback=self._task_fallback(ctx))
        # A task title is one line — defend against a chatty model.
        return title.splitlines()[0].strip().strip('"') if title else self._task_fallback(ctx)

    # -- Non-LLM directives (the Draft agent owns email prose) -----------

    def email_directive(self, ctx: "TaskContext") -> str:
        """The 'why + what + manner' the Draft agent needs — not the email itself.

        We tell the drafter why we're reaching out (from the email triage), what
        this follow-up is trying to achieve (from the plan's headline), and the
        specific next step + manner the next-step agent recommended. The drafter
        writes the actual prose and owns the tone.
        """
        deal = ctx.deal_context
        why = _REASON_BY_TYPE.get((ctx.classification or {}).get("type", ""), "")
        goal = _ACTION_GOAL.get(ctx.plan.headline_action, "keep the deal moving")
        parts = [f"Email the contact about {deal.opportunity_name}."]
        if why:
            parts.append(f"Reason: {why}.")
        parts.append(f"Goal: {goal}.")
        concern = self._top_concern(deal)
        if concern:
            parts.append(f"Be sure to address: {concern}")
        # The next-step agent decides what to do and in what manner — carry that
        # guidance into the directive so its intent actually shapes the email.
        parts.extend(self._next_step_guidance(ctx, "draft_email", "book_meeting"))
        # If a meeting slot is in play, state its exact length so the prose can't
        # contradict the booked slot (e.g. a 15-min slot called a 30-min meeting).
        minutes = self._meeting_duration_minutes(ctx)
        if minutes:
            parts.append(f"If proposing a meeting, it is {minutes} minutes long.")
        return " ".join(parts)

    def meeting_context(self, ctx: "TaskContext") -> str:
        """A deliberately minimal meeting brief: the people, the reason, the length.

        The duration is read from the chosen calendar slot so the email's stated
        length always matches the slot that will be booked (single source of truth).
        """
        deal = ctx.deal_context
        names = ", ".join(c.name for c in deal.contacts if c.name) or "the contact"
        reason = self._top_concern(deal) or f"discuss {deal.opportunity_name}"
        brief = f"Meeting with {names}. Reason: {reason}."
        minutes = self._meeting_duration_minutes(ctx)
        if minutes:
            brief += f" Length: {minutes} minutes (state this exact duration)."
        return brief

    # -- Internals -------------------------------------------------------

    def _masker(self, ctx: "TaskContext") -> ProfileMasker:
        contacts = [
            {"id": c.crm_id, "name": c.name, "email": c.email}
            for c in ctx.deal_context.contacts
        ]
        return ProfileMasker().register(contacts=contacts)

    async def _generate(
        self, prompt: str, masker: ProfileMasker, *, fallback: str
    ) -> str:
        # A flaky author must not break the run — fall back to a deterministic
        # one-liner, matching the defensive style of the rest of the pipeline.
        try:
            response = await self._deps.pipeline.get_chat_llm().ainvoke(
                [HumanMessage(content=prompt)]
            )
            text = (getattr(response, "content", "") or "").strip()
            return masker.unmask(text) if text else fallback
        except Exception:  # noqa: BLE001
            return fallback

    def _context_block(
        self, ctx: "TaskContext", masker: ProfileMasker, *kinds: str
    ) -> str:
        deal = ctx.deal_context
        lines = [
            f"Opportunity: {deal.opportunity_name} ({deal.company_name})",
            f"Stage: {deal.deal_stage}",
            f"Recommended action: {ctx.plan.headline_action}",
        ]
        if deal.risk_score is not None:
            lines.append(f"Risk score: {deal.risk_score:.2f}")
        classification = ctx.classification or {}
        if classification.get("type"):
            lines.append(f"Inbound email type: {classification['type']}")
        concern = self._top_concern(deal)
        if concern:
            lines.append(f"Open concern: {concern}")
        if deal.profile_narrative:
            lines.append(f"Briefing: {deal.profile_narrative}")
        trigger = ctx.state.get("trigger") or {}
        if trigger.get("body"):
            lines.append(f"Latest message: {trigger['body']}")
        # When a meeting has been scheduled (or just moved on a revise), surface
        # the chosen time so the note/task the author writes references the right
        # date instead of a stale one.
        meeting_time = self._meeting_time(ctx)
        if meeting_time:
            lines.append(f"Scheduled meeting time: {meeting_time}")
        # The next-step agent's guidance + evidence for THIS step kind, so the
        # note/task the agent reviews is grounded in what the planner intended.
        lines.extend(self._next_step_guidance(ctx, *kinds))
        # Mask the whole block once: registered names + NER-discovered, plus emails.
        return masker.mask("\n".join(lines))

    @staticmethod
    def _matching_step(ctx: "TaskContext", kinds: tuple[str, ...]) -> Any:
        for step in ctx.plan.steps:
            if step.kind in kinds:
                return step
        return None

    def _next_step_guidance(self, ctx: "TaskContext", *kinds: str) -> list[str]:
        """The matching plan step's intent + recommended manner + evidence."""
        step = self._matching_step(ctx, kinds) if kinds else None
        if step is None:
            return []
        lines: list[str] = []
        if step.intent:
            lines.append(f"Recommended next step: {step.intent}")
        metadata = step.metadata or {}
        if metadata.get("approach"):
            lines.append(f"Suggested approach: {metadata['approach']}")
        evidence = metadata.get("evidence") or []
        if evidence:
            lines.append("Grounded in: " + "; ".join(str(item) for item in evidence[:3]))
        return lines

    @staticmethod
    def _meeting_time(ctx: "TaskContext") -> Optional[str]:
        """The first available/chosen calendar slot's start, if a meeting is set."""
        calendar = ctx.calendar
        if calendar is None or not getattr(calendar, "available_slots", None):
            return None
        chosen = next(
            (slot for slot in calendar.available_slots if getattr(slot, "available", False)),
            None,
        )
        return chosen.start if chosen is not None else None

    @staticmethod
    def _meeting_duration_minutes(ctx: "TaskContext") -> Optional[int]:
        """Minutes between the chosen slot's start and end, if a meeting is set."""
        from datetime import datetime

        calendar = ctx.calendar
        if calendar is None or not getattr(calendar, "available_slots", None):
            return None
        chosen = next(
            (slot for slot in calendar.available_slots if getattr(slot, "available", False)),
            None,
        )
        if chosen is None:
            return None
        try:
            start = datetime.fromisoformat(str(chosen.start).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(chosen.end).replace("Z", "+00:00"))
        except ValueError:
            return None
        minutes = int((end - start).total_seconds() // 60)
        return minutes if minutes > 0 else None

    @staticmethod
    def _top_concern(deal: Any) -> Optional[str]:
        if deal.open_concerns:
            return deal.open_concerns[0].get("content")
        return None

    def _note_fallback(self, ctx: "TaskContext") -> str:
        deal = ctx.deal_context
        return (
            f"Follow-up on {deal.opportunity_name} ({deal.deal_stage}). "
            f"Recommended action: {ctx.plan.headline_action}."
        )

    def _task_fallback(self, ctx: "TaskContext") -> str:
        return f"Follow up on {ctx.deal_context.opportunity_name}"


__all__ = ["ContentAuthor"]
