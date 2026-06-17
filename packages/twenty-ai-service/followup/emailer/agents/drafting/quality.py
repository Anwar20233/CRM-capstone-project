from followup.emailer.agents.drafting.schemas import (
    EMAIL_DRAFT_TYPES,
    DraftType,
    EmailDraft,
    PROPOSAL_DRAFT_TYPES,
    ProposalDraft,
)
from followup.emailer.context.schemas import DealContext
from followup.emailer.rag.service import RetrievedChunk

import json
import re


_PLACEHOLDER_PATTERN = re.compile(r"\[[A-Z][A-Z0-9 _]+\]")

_WORD_COUNT_MIN = 100
_WORD_COUNT_MAX = 800


def _word_count(text: str) -> int:
    return len(text.split())


def _draft_text(draft: EmailDraft | ProposalDraft) -> str:
    if isinstance(draft, EmailDraft):
        return f"{draft.subject}\n{draft.body}"
    section_text = "\n".join(
        f"{section.heading}\n{section.content}" for section in draft.sections
    )
    return f"{draft.title}\n{section_text}"


def _mentions_company_or_contact(text: str, context: DealContext) -> bool:
    lowered = text.lower()
    company_name = context.company.name.lower()
    contact_name = context.contact.name.lower()
    return company_name in lowered or contact_name in lowered


def _references_stage_or_meeting(text: str, context: DealContext) -> bool:
    lowered = text.lower()
    stage = context.opportunity.stage.lower()
    if stage and stage in lowered:
        return True
    for meeting in context.recent_meetings:
        if meeting.title.lower() in lowered:
            return True
        if meeting.summary and meeting.summary.lower() in lowered:
            return True
    return False


def _has_subject_or_title(draft: EmailDraft | ProposalDraft) -> bool:
    if isinstance(draft, EmailDraft):
        return bool(draft.subject.strip())
    return bool(draft.title.strip())


def score_draft_quality(
    draft: EmailDraft | ProposalDraft,
    context: DealContext,
) -> float:
    text = _draft_text(draft)
    score = 0.0

    if _has_subject_or_title(draft):
        score += 0.2
    if _mentions_company_or_contact(text, context):
        score += 0.2
    if _references_stage_or_meeting(text, context):
        score += 0.2

    word_count = _word_count(text)
    if _WORD_COUNT_MIN <= word_count <= _WORD_COUNT_MAX:
        score += 0.2

    if not _PLACEHOLDER_PATTERN.search(text):
        score += 0.2

    return min(score, 1.0)


def explain_draft_choices(
    draft: EmailDraft | ProposalDraft,
    context: DealContext,
) -> str:
    parts: list[str] = [
        f"Draft type: {draft.draft_type.value}.",
        f"Grounded in template: {draft.template_used or 'unknown'}.",
        f"Personalized for {context.contact.name} at {context.company.name}.",
    ]

    if context.opportunity.stage:
        parts.append(f"References opportunity stage: {context.opportunity.stage}.")

    if context.recent_meetings:
        latest_meeting = context.recent_meetings[0]
        parts.append(f"Incorporates recent meeting: {latest_meeting.title}.")

    if isinstance(draft, ProposalDraft) and context.company.industry:
        parts.append(f"Tailored to industry: {context.company.industry}.")

    return " ".join(parts)


def build_llm_schema_hint(draft_type: DraftType) -> str:
    if draft_type in EMAIL_DRAFT_TYPES:
        return json.dumps(
            {
                "subject": "string",
                "body": "string",
                "draft_type": draft_type.value,
            },
            indent=2,
        )
    return json.dumps(
        {
            "title": "string",
            "sections": [{"heading": "string", "content": "string"}],
            "draft_type": draft_type.value,
        },
        indent=2,
    )


def format_catalog_snippets(snippets: list[RetrievedChunk]) -> str:
    if not snippets:
        return "No catalog snippets available."
    return "\n\n".join(
        f"--- {snippet.document_id} ---\n{snippet.content}" for snippet in snippets
    )
