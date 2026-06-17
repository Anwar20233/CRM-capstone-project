from __future__ import annotations

import logging

from agent.llm_client import LLMClient
from followup.context.crm_fetch import fetch_opportunity_bundle
from followup.context.crm_identity import resolve_crm_identity
from followup.context.enrich import enrich_context
from followup.context.errors import ContextLoadError, LlmExtractError
from followup.context.llm_extract import extract_deal_context
from followup.context.map_crm import map_deal_context_fallback
from followup.context.schemas import DealContext

logger = logging.getLogger(__name__)


async def load_deal_context(
    opportunity_id: str,
    workspace_id: str,
    user_id: str,
    *,
    role_id: str | None = None,
    use_llm: bool = True,
    llm_client: LLMClient | None = None,
) -> DealContext:
    identity = resolve_crm_identity(
        workspace_id,
        user_id,
        role_id=role_id,
    )
    bundle = await fetch_opportunity_bundle(opportunity_id, identity)

    context: DealContext | None = None
    provenance = "crm_fallback"

    if use_llm:
        try:
            context = await extract_deal_context(bundle, llm_client=llm_client)
            provenance = "hybrid"
        except LlmExtractError as error:
            logger.info(
                "Falling back to deterministic CRM mapping for %s: %s",
                opportunity_id,
                error,
            )

    if context is None:
        try:
            context = map_deal_context_fallback(bundle)
        except Exception as error:
            raise ContextLoadError(
                "MAP_FAILED",
                f"Failed to map opportunity {opportunity_id}",
                detail={"bundle": bundle.model_dump(mode="json")},
            ) from error

    return enrich_context(context, provenance=provenance)
