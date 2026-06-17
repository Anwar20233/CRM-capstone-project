"""Prompt construction for the Next Step Intelligence Agent.

Runs three internal analysis tools (stage playbook, BANT assessment,
engagement health) and assembles their outputs into a single structured
prompt for call_llm_json.

No RAG retrieval — all context is either deal data or deterministic
tool output generated from that deal data.
"""

from __future__ import annotations

from followup.next_step.agents.next_step.tools import (
    BANTAssessment,
    EngagementHealth,
    StageGuidance,
    assess_engagement_health,
    evaluate_bant_gaps,
    lookup_stage_playbook,
)
from followup.next_step.context.schemas import DealContext

MAX_TIMELINE_ITEMS = 10

SYSTEM_INSTRUCTIONS = """You are an expert B2B sales coach embedded in a CRM.
Given the deal context and analysis below, recommend 1-5 specific,
high-leverage next actions for the sales rep on THIS deal.

Rules:
- Output JSON ONLY, matching the provided schema. No prose, no markdown fences.
- Recommend between 1 and 5 actions (never 0, never more than 5).
- Every action must include `reasoning` that explains why it matters now.
- Every action must include `evidence`: short strings citing specific facts
  from the deal context (timeline items, engagement metrics, contacts,
  profile facts).
- `orchestrator_tool` must be one of: create_task, schedule_meeting,
  send_email, update_opportunity, log_activity, create_reminder.
- `orchestrator_instruction` must reference the opportunity id and describe
  exactly what the Orchestrator should execute if the rep accepts this action.
- `priority` is 1 (highest) to 5 (lowest). Do not assign the same priority
  to two actions unless they are genuinely equal in urgency.
- Ground every action in the deal context provided — do not invent facts."""


def _format_opportunity(context: DealContext) -> str:
    opp = context.opportunity
    lines = [f"id: {opp.id}", f"name: {opp.name}", f"stage: {opp.stage}"]
    if opp.amount is not None:
        lines.append(f"amount: {opp.amount}")
    if opp.close_date is not None:
        lines.append(f"close_date: {opp.close_date.isoformat()}")
    company = context.company
    lines.append(
        f"company: {company.name} (industry: {company.industry or 'unknown'})"
        if company else "company: none linked"
    )
    return "\n".join(lines)


def _format_contacts(context: DealContext) -> str:
    if not context.contacts:
        return "No contacts linked."
    lines = []
    for c in context.contacts:
        dm = " (decision maker)" if c.is_decision_maker else ""
        lines.append(f"- {c.name} — {c.role or 'unknown role'}{dm}")
    return "\n".join(lines)


def _format_timeline(context: DealContext) -> str:
    if not context.timeline:
        return "No recent timeline activity."
    items = sorted(context.timeline, key=lambda i: i.occurred_at, reverse=True)[:MAX_TIMELINE_ITEMS]
    return "\n".join(
        f"- [{i.occurred_at.isoformat()}] ({i.type}) {i.title}: {i.summary}"
        for i in items
    )


def _format_tasks(context: DealContext) -> str:
    if not context.tasks:
        return "No open tasks."
    lines = []
    for t in context.tasks:
        overdue = " [OVERDUE]" if t.is_overdue else ""
        due = f", due {t.due_at.isoformat()}" if t.due_at else ""
        lines.append(f"- {t.title} ({t.status}{due}){overdue}")
    return "\n".join(lines)


def _format_meetings(context: DealContext) -> str:
    if not context.meetings:
        return "No meetings scheduled."
    return "\n".join(
        f"- {m.title} at {m.starts_at.isoformat()} ({m.status})"
        for m in context.meetings
    )


def _format_profile_facts(context: DealContext) -> str:
    if not context.active_facts:
        return "No active profile facts."
    return "\n".join(
        f"- [{f.fact_id}] ({f.category.value}) {f.fact_key} = {f.value}"
        for f in context.active_facts
    )


def _format_playbook(playbook: StageGuidance) -> str:
    lines = [
        f"Stage objective: {playbook.objective}",
        "Key activities:",
        *[f"  - {a}" for a in playbook.key_activities],
        "Exit criteria:",
        *[f"  - {c}" for c in playbook.exit_criteria],
        "Common pitfalls:",
        *[f"  - {p}" for p in playbook.common_pitfalls],
    ]
    return "\n".join(lines)


def _format_bant(bant: BANTAssessment) -> str:
    lines = [f"Qualification score: {bant.qualification_score}/4"]
    for gap in bant.gaps:
        lines.append(f"- {gap.dimension}: {gap.status} — {gap.detail}")
    if bant.is_fully_qualified:
        lines.append("All BANT dimensions confirmed.")
    else:
        missing = [g.dimension for g in bant.gaps if g.status != "confirmed"]
        lines.append(f"Unconfirmed BANT dimensions: {', '.join(missing)}")
    return "\n".join(lines)


def _format_engagement(health: EngagementHealth) -> str:
    lines = [
        f"Status: {health.status}",
        f"Days since last activity: {health.days_since_last}",
        f"Activity trend (14d vs prior 14d): {health.trend}",
        f"Future meeting booked: {health.has_future_meeting}",
    ]
    if health.risk_flags:
        lines.append("Risk flags:")
        lines.extend(f"  - {flag}" for flag in health.risk_flags)
    return "\n".join(lines)


def build_next_step_prompt(
    context: DealContext, trigger_context: str | None = None
) -> str:
    """Build the full structured prompt for the Next Step Intelligence Agent.

    Runs internal analysis tools and assembles all deal context into a
    single prompt string ready for call_llm_json.

    Args:
        context: The full deal context provided by the Orchestrator.
        trigger_context: Optional description of what triggered this run (the
            inbound message / query / signal). Rendered as its own section so
            the recommendations respond to the cause, not just the deal state.

    Returns:
        A single prompt string requesting 1-5 structured recommendations.
    """
    playbook = lookup_stage_playbook(context.opportunity.stage)
    bant = evaluate_bant_gaps(context)
    engagement = assess_engagement_health(context)

    trigger_section: list[str] = []
    if trigger_context and trigger_context.strip():
        trigger_section = ["## What triggered this", trigger_context.strip()]

    sections = [
        SYSTEM_INSTRUCTIONS,
        *trigger_section,
        "## Opportunity",
        _format_opportunity(context),
        "## Contacts",
        _format_contacts(context),
        "## Recent timeline",
        _format_timeline(context),
        "## Open tasks",
        _format_tasks(context),
        "## Meetings",
        _format_meetings(context),
        "## Active client profile facts",
        _format_profile_facts(context),
        "## Stage playbook",
        _format_playbook(playbook),
        "## BANT assessment",
        _format_bant(bant),
        "## Engagement health",
        _format_engagement(engagement),
        (
            "## Task\n"
            "Recommend 1-5 next actions for this deal. Return JSON matching "
            "the NextStepLLMOutput schema: {\"actions\": [...], "
            "\"summary_reasoning\": str, \"confidence\": float (0.0-1.0)}."
        ),
    ]
    return "\n\n".join(sections)
