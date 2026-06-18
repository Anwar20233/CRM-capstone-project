"""System prompt and deal context builder for the Next Step Intelligence Agent.

SYSTEM_PROMPT defines the agent's identity, available tools, required workflow,
and output contract. It contains only instructions — no pre-computed analysis.

build_deal_context_message assembles the compact deal facts (opportunity,
contacts, timeline, tasks, engagement, BANT signals) that the agent reasons
from. The business rules (what to do given these facts) come from the tools.
"""

from __future__ import annotations

from followup.next_step.agents.next_step.tools import BANTSignals, EngagementSignals
from followup.next_step.context.schemas import DealContext

_MAX_TIMELINE_ITEMS = 10

SYSTEM_PROMPT = """\
You are an expert B2B sales coach embedded in a CRM.

You have three knowledge tools:
  read_stage_playbook(stage)  — reads the full playbook for a pipeline stage
  read_bant_framework()       — reads the BANT qualification framework
  read_best_practices()       — reads general B2B sales best practices

## Required workflow
1. Call read_stage_playbook with the deal's current stage.
2. Call read_bant_framework.
3. Optionally call read_best_practices when the engagement or task situation warrants it.
4. Using the retrieved knowledge and the deal context below, produce 1–5 next actions.

## Output contract
- Return JSON ONLY matching the provided schema. No prose, no markdown fences.
- 1–5 actions (never 0, never more than 5).
- Every action: reasoning explains why this action matters NOW.
- Every action: evidence cites specific facts from the deal context (timeline
  items, engagement metrics, contacts, BANT signals, profile facts).
- orchestrator_tool must be one of: create_task, schedule_meeting, send_email,
  update_opportunity, log_activity, create_reminder.
- orchestrator_instruction must reference the opportunity id and describe
  exactly what to execute if the rep accepts this action.
- priority 1 (highest) to 5 (lowest). Avoid ties unless genuinely equal urgency.
- Ground every action in the provided deal context. Do not invent facts.\
"""


def build_deal_context_message(
    context: DealContext,
    trigger: str | None,
    bant: BANTSignals,
    engagement: EngagementSignals,
) -> str:
    """Compact, structured deal context for the planner's initial message.

    Contains only facts (observations about the deal). The rules (what to do
    given these facts) come from the tool calls the agent makes.
    """
    sections: list[str] = []

    if trigger and trigger.strip():
        sections.append(f"## Trigger\n{trigger.strip()}")

    opp = context.opportunity
    opp_lines = [f"id: {opp.id}", f"name: {opp.name}", f"stage: {opp.stage}"]
    if opp.amount is not None:
        opp_lines.append(f"amount: {opp.amount}")
    if opp.close_date:
        opp_lines.append(f"close_date: {opp.close_date.date()}")
    if context.company:
        industry = f" (industry: {context.company.industry})" if context.company.industry else ""
        opp_lines.append(f"company: {context.company.name}{industry}")
    sections.append("## Opportunity\n" + "\n".join(opp_lines))

    if context.contacts:
        contact_lines = [
            "- " + c.name
            + (f" — {c.role}" if c.role else "")
            + (" [decision maker]" if c.is_decision_maker else "")
            for c in context.contacts
        ]
        sections.append("## Contacts\n" + "\n".join(contact_lines))
    else:
        sections.append("## Contacts\nNone linked.")

    items = sorted(context.timeline, key=lambda i: i.occurred_at, reverse=True)[:_MAX_TIMELINE_ITEMS]
    if items:
        tl = "\n".join(f"- [{i.occurred_at.date()}] ({i.type}) {i.summary}" for i in items)
        sections.append(f"## Recent timeline\n{tl}")
    else:
        sections.append("## Recent timeline\nNo recent activity.")

    if context.tasks:
        task_lines = [
            "- " + t.title
            + (f" [due {t.due_at.date()}]" if t.due_at else "")
            + (" [OVERDUE]" if t.is_overdue else "")
            for t in context.tasks
        ]
        sections.append("## Open tasks\n" + "\n".join(task_lines))

    if context.meetings:
        mtg_lines = [f"- {m.title} on {m.starts_at.date()} [{m.status}]" for m in context.meetings]
        sections.append("## Meetings\n" + "\n".join(mtg_lines))

    if context.active_facts:
        fact_lines = [f"- {f.fact_key} = {f.value}" for f in context.active_facts]
        sections.append("## Profile facts\n" + "\n".join(fact_lines))

    bant_lines = [f"Qualification score: {bant.qualification_score}/4"]
    for gap in bant.gaps:
        bant_lines.append(f"- {gap.dimension}: {gap.status} — {gap.detail}")
    sections.append("## BANT signals\n" + "\n".join(bant_lines))

    days = engagement.days_since_last
    days_str = str(days) if days is not None else "unknown (no activity on record)"
    eng_lines = [
        f"Status: {engagement.status}",
        f"Days since last activity: {days_str}",
        f"Activity trend (14d vs prior 14d): {engagement.trend}",
        f"Future meeting booked: {engagement.has_future_meeting}",
    ]
    if engagement.risk_flags:
        eng_lines.append("Risk flags: " + "; ".join(engagement.risk_flags))
    sections.append("## Engagement\n" + "\n".join(eng_lines))

    sections.append(
        "## Task\n"
        "Call read_stage_playbook and read_bant_framework first, then recommend 1–5 next actions.\n"
        'Return JSON matching the NextStepLLMOutput schema: '
        '{"actions": [...], "summary_reasoning": str, "confidence": float (0.0–1.0)}.'
    )

    return "\n\n".join(sections)
