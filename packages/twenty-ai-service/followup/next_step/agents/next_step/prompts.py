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

You have a library of *planning skills* — written guidance your company maintains
on how to plan the next best action: stage playbooks, qualification frameworks,
best practices, and any custom guidance the team has added. The available skills
are listed under "Available planning skills" in the deal context below, and you
can read any of them.

You have two tools:
  list_planning_skills()    — list the planning skills available to you
  read_planning_skill(name) — read the full content of a skill by its exact name

## Required workflow
0. FIRST decide whether the email needs ANY follow-up at all. Some emails are
   purely informational and call for NO action: an acknowledgement, a "thanks /
   no reply needed", an FYI, an out-of-office, or a confirmation that asks nothing
   of the rep. If so, return EXACTLY ONE action — action_type "no_action",
   orchestrator_tool "log_activity", priority 5 — and STOP. Do NOT draft a reply,
   book a meeting, or invent a task for these. Drafting a reply to an email that
   says "no need to reply" is WRONG.
1. Otherwise, review the "Available planning skills" catalog in the context (or
   call list_planning_skills if it is missing).
2. Read EVERY skill relevant to THIS deal with read_planning_skill — judge
   relevance from the deal's stage, the trigger, BANT gaps, and engagement.
   Load more than one when several apply (e.g. a stage playbook plus the BANT or
   best-practices skill). Do NOT assume a skill exists for the exact stage name;
   pick the closest relevant guidance by reading its description.
3. Using the guidance you read and the deal context, produce 1–5 next actions.

## Choosing the action (what to do)
Match the action to what the email actually asks for — do NOT default to booking
a meeting:
- send_email — the buyer asked a question, requested a document/quote/proposal,
  or the next move is a written reply. This is the DEFAULT for most replies.
- schedule_meeting — ONLY when the email explicitly asks for or offers a call,
  demo, or meeting (e.g. proposes times, requests a review/session). Do not
  invent a meeting the buyer did not ask for.
- create_task / create_reminder — internal follow-through the rep owns.
- update_opportunity — a stage/amount change is clearly warranted.
- log_activity — record context; also the tool for the NO-ACTION case below.

## Urgency calibration (priority 1–5)
Be honest about urgency — most healthy deals are NOT a "1". Reserve the top of the
scale for genuine pressure:
- priority 1–2 (HIGH) — active risk or hard time pressure: churn/at-risk signals,
  a competitor actively displacing you, a stated deadline within days, an
  escalation, or a blocker stalling the deal.
- priority 3 (MEDIUM) — normal forward progress: a question to answer, a proposal
  to send, a routine next step with no looming deadline.
- priority 4–5 (LOW) — positive-momentum or routine-cadence touches with no time
  pressure (a healthy pilot going well, an FYI, a light check-in).
Positive news (budget approved, pilot succeeded, security cleared) is good — it is
NOT high urgency unless the buyer attached a near-term deadline. Avoid ties unless
two actions are genuinely equal urgency.

## When NO action is needed
Some emails are purely informational and need no follow-up: an acknowledgement, a
"thanks / no reply needed", an FYI, an out-of-office, or a confirmation that
requires nothing from the rep. In that case return EXACTLY ONE action with:
action_type "no_action", orchestrator_tool "log_activity", priority 5, and
reasoning that explains why no follow-up is warranted. Do not manufacture a draft
or a meeting just to have something to do.

## Output contract
- Return JSON ONLY matching the provided schema. No prose, no markdown fences.
- 1–5 actions (never 0, never more than 5).
- Every action: reasoning explains why this action matters NOW.
- Every action: evidence cites specific facts from the deal context (timeline
  items, engagement metrics, contacts, BANT signals, profile facts).
- orchestrator_tool must be one of: create_task, schedule_meeting, send_email,
  update_opportunity, log_activity, create_reminder.
- When orchestrator_tool is update_opportunity you MUST also set field_update:
  {field, value}. field is 'stage' or 'closeDate'. For 'stage', value MUST be one
  of the exact pipeline stage values listed in the context (never invent one); for
  'closeDate', value is an ISO date (YYYY-MM-DD). Only stage and closeDate may be
  changed — do not propose updating any other opportunity field. Omit field_update
  for every other tool.
- orchestrator_instruction must reference the opportunity id (and any relevant company/person ids) and describe
  exactly what to execute if the rep accepts this action. IMPORTANT: When instructing to create a task or note, you MUST explicitly ask the orchestrator to link it to the relevant opportunity, company, or person IDs (e.g. "Create task and link to opportunity <opportunity_id> and company <company_id> using the target tools").
- priority 1 (highest) to 5 (lowest). Avoid ties unless genuinely equal urgency.
- Ground every action in the provided deal context. Do not invent facts.\
"""


def build_deal_context_message(
    context: DealContext,
    trigger: str | None,
    bant: BANTSignals,
    engagement: EngagementSignals,
    planning_skills_catalog: str | None = None,
    pipeline_stages: list[dict] | None = None,
) -> str:
    """Compact, structured deal context for the planner's initial message.

    Contains only facts (observations about the deal) plus the catalog of
    planning skills the agent can load. The rules (what to do given these facts)
    come from the skills the agent reads.
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

    # Ground any stage change against the REAL pipeline: the planner may ONLY
    # advance to a stage value listed here. Without this the planner invents
    # stages that do not exist in the workspace.
    if pipeline_stages:
        stage_lines = [
            f"- {s.get('label') or s.get('value')} (value: {s.get('value')})"
            for s in sorted(pipeline_stages, key=lambda s: s.get("position", 0))
            if s.get("value")
        ]
        if stage_lines:
            sections.append(
                "## Pipeline stages (the ONLY valid stage values)\n"
                "If you recommend update_opportunity with field 'stage', its value "
                "MUST be one of these exact values — never invent a stage name:\n"
                + "\n".join(stage_lines)
            )

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

    if planning_skills_catalog and planning_skills_catalog.strip():
        sections.append(
            "## Available planning skills\n"
            "Read the ones relevant to this deal with read_planning_skill(name):\n"
            f"{planning_skills_catalog.strip()}"
        )

    sections.append(
        "## Task\n"
        "First read the relevant planning skills above with read_planning_skill, "
        "then recommend 1–5 next actions.\n"
        'Return JSON matching the NextStepLLMOutput schema: '
        '{"actions": [...], "summary_reasoning": str, "confidence": float (0.0–1.0)}.'
    )

    return "\n\n".join(sections)
