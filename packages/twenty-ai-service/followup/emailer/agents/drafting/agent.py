from followup.emailer.agents.drafting.prompts import build_draft_prompt
from followup.emailer.agents.drafting.quality import explain_draft_choices, score_draft_quality
from followup.emailer.agents.drafting.resolver import resolve_draft_types
from followup.emailer.agents.drafting.schemas import (
    EMAIL_DRAFT_TYPES,
    PROPOSAL_DRAFT_TYPES,
    DraftType,
    DraftingAgentResult,
    EmailDraft,
    ProposalDraft,
)
from followup.emailer.agents.risk.schemas import RiskScore
from agent.llm_client import call_llm_json
from followup.emailer.context.schemas import DealContext
from followup.emailer.events.schemas import FollowUpEvent
from followup.emailer.rag.collections import CollectionName
from followup.emailer.rag.service import RetrievedChunk, RetrievalService

_MAX_EMAIL_DRAFTS = 1
_MAX_PROPOSAL_DRAFTS = 1


def _template_collection(draft_type: DraftType) -> CollectionName:
    if draft_type in EMAIL_DRAFT_TYPES:
        return CollectionName.EMAIL_TEMPLATES
    return CollectionName.PROPOSAL_TEMPLATES


def _catalog_collection(draft_type: DraftType) -> CollectionName:
    if draft_type == DraftType.SERVICE_PROPOSAL:
        return CollectionName.SERVICE_CATALOG
    return CollectionName.PRODUCT_CATALOG


async def _get_template(
    draft_type: DraftType,
    retrieval: RetrievalService,
) -> tuple[str, str]:
    chunks = await retrieval.retrieve_documents(
        query=draft_type.value,
        collection=_template_collection(draft_type),
        top_k=1,
    )
    if not chunks:
        return "", draft_type.value
    return chunks[0].content, chunks[0].metadata.get("file_name", chunks[0].document_id)


async def _get_product_catalog_snippets(
    industry: str | None,
    draft_type: DraftType,
    retrieval: RetrievalService,
) -> list[RetrievedChunk]:
    query = industry or "general"
    return await retrieval.retrieve_documents(
        query=query,
        collection=_catalog_collection(draft_type),
        top_k=2,
    )


async def _generate_email_draft(
    context: DealContext,
    draft_type: DraftType,
    template: str,
    template_name: str,
    catalog_snippets: list[RetrievedChunk],
    model: str | None = None,
) -> EmailDraft:
    prompt = build_draft_prompt(context, template, catalog_snippets, draft_type)
    llm_draft = await call_llm_json(prompt, EmailDraft, model=model)
    draft = llm_draft.model_copy(
        update={
            "draft_type": draft_type,
            "template_used": template_name,
        }
    )
    draft.quality_score = score_draft_quality(draft, context)
    draft.reasoning = explain_draft_choices(draft, context)
    return draft


async def _generate_proposal_draft(
    context: DealContext,
    draft_type: DraftType,
    template: str,
    template_name: str,
    catalog_snippets: list[RetrievedChunk],
    model: str | None = None,
) -> ProposalDraft:
    prompt = build_draft_prompt(context, template, catalog_snippets, draft_type)
    llm_draft = await call_llm_json(prompt, ProposalDraft, model=model)
    draft = llm_draft.model_copy(
        update={
            "draft_type": draft_type,
            "template_used": template_name,
        }
    )
    draft.quality_score = score_draft_quality(draft, context)
    draft.reasoning = explain_draft_choices(draft, context)
    return draft


def _apply_output_caps(
    email_drafts: list[EmailDraft],
    proposal_drafts: list[ProposalDraft],
) -> tuple[list[EmailDraft], list[ProposalDraft]]:
    return (
        email_drafts[:_MAX_EMAIL_DRAFTS],
        proposal_drafts[:_MAX_PROPOSAL_DRAFTS],
    )


def _build_summary_reasoning(
    email_drafts: list[EmailDraft],
    proposal_drafts: list[ProposalDraft],
    resolved_types: list[DraftType],
) -> str:
    generated = [draft.draft_type.value for draft in email_drafts]
    generated.extend(draft.draft_type.value for draft in proposal_drafts)
    if not generated:
        return "No drafts needed"
    return (
        f"Resolved types: {', '.join(type_.value for type_ in resolved_types)}. "
        f"Generated: {', '.join(generated)}."
    )


async def run_drafting_agent(
    context: DealContext,
    event: FollowUpEvent,
    draft_types: list[DraftType] | None,
    retrieval: RetrievalService,
    risk_score: RiskScore | None = None,
    model: str | None = None,
) -> DraftingAgentResult:
    resolved_types = draft_types or resolve_draft_types(event, context, risk_score)

    if not resolved_types:
        return DraftingAgentResult(
            email_drafts=[],
            proposal_drafts=[],
            reasoning="No drafts needed",
            skipped=True,
        )

    email_drafts: list[EmailDraft] = []
    proposal_drafts: list[ProposalDraft] = []

    for draft_type in resolved_types:
        template, template_name = await _get_template(draft_type, retrieval)
        catalog_snippets: list[RetrievedChunk] = []
        if draft_type in PROPOSAL_DRAFT_TYPES:
            catalog_snippets = await _get_product_catalog_snippets(
                context.company.industry,
                draft_type,
                retrieval,
            )

        if draft_type in EMAIL_DRAFT_TYPES and len(email_drafts) < _MAX_EMAIL_DRAFTS:
            email_drafts.append(
                await _generate_email_draft(
                    context,
                    draft_type,
                    template,
                    template_name,
                    catalog_snippets,
                    model,
                )
            )
        elif draft_type in PROPOSAL_DRAFT_TYPES and len(proposal_drafts) < _MAX_PROPOSAL_DRAFTS:
            proposal_drafts.append(
                await _generate_proposal_draft(
                    context,
                    draft_type,
                    template,
                    template_name,
                    catalog_snippets,
                    model,
                )
            )

    email_drafts, proposal_drafts = _apply_output_caps(email_drafts, proposal_drafts)

    return DraftingAgentResult(
        email_drafts=email_drafts,
        proposal_drafts=proposal_drafts,
        reasoning=_build_summary_reasoning(
            email_drafts,
            proposal_drafts,
            resolved_types,
        ),
        skipped=False,
    )
