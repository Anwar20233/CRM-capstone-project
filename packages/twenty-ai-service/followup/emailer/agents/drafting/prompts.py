from followup.emailer.agents.drafting.quality import build_llm_schema_hint, format_catalog_snippets
from followup.emailer.agents.drafting.schemas import DraftType
from followup.emailer.context.schemas import DealContext
from followup.emailer.rag.service import RetrievedChunk

# The static drafting instructions (role + rules). Hoisted to a module constant
# so the DSPy prompt optimizer (optimization/followup) can swap a candidate in
# via the ``system_prompt`` arg below without touching context assembly. Editing
# this string changes production behavior; the optimizer never edits it in place.
DRAFTING_SYSTEM_PROMPT = """\
You are a sales drafting assistant for a CRM follow-up system.
Generate a single draft as JSON matching the schema provided below.

Rules:
- Return valid JSON only, no markdown fences.
- Personalize contact, company, and deal details from context.
- End the body with this sign-off block on separate lines: "Best regards,", then "[Your Name]" (or "[INSERT NAME]" if no sender name is in context), then "BeamData".
- Do not leave other bracket placeholders in the body.
- Body or section content should be substantive (roughly 100-800 words total).
- Set draft_type to the draft type stated below."""


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
    system_prompt: str | None = None,
) -> str:
    schema_hint = build_llm_schema_hint(draft_type)
    catalog_text = format_catalog_snippets(catalog_snippets)
    instructions = system_prompt or DRAFTING_SYSTEM_PROMPT

    return f"""{instructions}

Schema:
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
"""
