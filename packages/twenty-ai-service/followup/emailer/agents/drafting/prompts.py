from followup.emailer.agents.drafting.quality import build_llm_schema_hint, format_catalog_snippets
from followup.emailer.agents.drafting.schemas import DraftType
from followup.emailer.context.schemas import DealContext
from followup.emailer.rag.service import RetrievedChunk


def _format_meetings(context: DealContext) -> str:
    if not context.recent_meetings:
        return "No recent meetings."
    lines: list[str] = []
    for meeting in context.recent_meetings[:3]:
        summary = meeting.summary or "No summary provided."
        attendees = ", ".join(meeting.attendees) if meeting.attendees else "Unknown"
        lines.append(
            f"- {meeting.title} (attendees: {attendees}): {summary}"
        )
    return "\n".join(lines)


def _format_notes(context: DealContext) -> str:
    if not context.recent_notes:
        return "No recent notes."
    lines: list[str] = []
    for note in context.recent_notes[:3]:
        body = note.body or note.title or "No content."
        lines.append(f"- {body}")
    return "\n".join(lines)


def build_draft_prompt(
    context: DealContext,
    template: str,
    catalog_snippets: list[RetrievedChunk],
    draft_type: DraftType,
) -> str:
    schema_hint = build_llm_schema_hint(draft_type)
    catalog_text = format_catalog_snippets(catalog_snippets)

    return f"""You are a sales drafting assistant for a CRM follow-up system.
Generate a single draft as JSON matching this schema:
{schema_hint}

Draft type: {draft_type.value}

Deal context:
- Opportunity ID: {context.opportunity.id}
- Stage: {context.opportunity.stage}
- Amount: {context.opportunity.amount}
- Company: {context.company.name} (industry: {context.company.industry or "unknown"})
- Contact: {context.contact.name} ({context.contact.email or "no email"})
- Recent meetings:
{_format_meetings(context)}
- Recent notes:
{_format_notes(context)}

Template guidance:
{template}

Catalog snippets (use when relevant for proposals):
{catalog_text}

Rules:
- Return valid JSON only, no markdown fences.
- Personalize with real names and deal details from context.
- Do not leave bracket placeholders like [INSERT NAME].
- Body or section content should be substantive (roughly 100-800 words total).
- Set draft_type to "{draft_type.value}".
"""
