from followup.context.crm_fetch import RawOpportunityBundle, fetch_opportunity_bundle
from followup.context.crm_identity import CrmIdentity, resolve_crm_identity
from followup.context.enrich import enrich_context
from followup.context.errors import ContextLoadError, LlmExtractError
from followup.context.loader import load_deal_context
from followup.context.llm_extract import extract_deal_context
from followup.context.map_crm import map_deal_context_fallback
from followup.context.protocols import DealContextExtractor
from followup.context.schemas import (
    CompanySnapshot,
    ContactSnapshot,
    DealContext,
    EngagementMetrics,
    MeetingSnapshot,
    OpportunitySnapshot,
    PipelineMeta,
    TaskSnapshot,
    TimelineItem,
)

__all__ = [
    "CompanySnapshot",
    "ContactSnapshot",
    "ContextLoadError",
    "CrmIdentity",
    "DealContext",
    "DealContextExtractor",
    "EngagementMetrics",
    "LlmExtractError",
    "MeetingSnapshot",
    "OpportunitySnapshot",
    "PipelineMeta",
    "RawOpportunityBundle",
    "TaskSnapshot",
    "TimelineItem",
    "enrich_context",
    "extract_deal_context",
    "fetch_opportunity_bundle",
    "load_deal_context",
    "map_deal_context_fallback",
    "resolve_crm_identity",
]
