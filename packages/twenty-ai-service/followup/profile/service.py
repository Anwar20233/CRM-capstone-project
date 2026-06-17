"""Profile synthesis service — the Follow-Up Agent's knowledge-graph read path.

Given an opportunity id, ``ProfileService`` assembles everything known about a
deal (CRM records via the reader, extracted facts, relationships, shadow
entities, the latest risk score) and synthesizes a natural-language briefing.

Two public methods share one internal load so the reader/DB work happens once:

* ``build_profile_narrative`` — the briefing plus the structured graph behind it.
* ``build_deal_context`` — the richer bundle (deal value, company, recent
  activities, open concerns) downstream agents consume.

Conventions (carried from Step 2): all dependencies arrive via ``PipelineDeps``;
only this service talks to the reader, through the injected ``CRMReader``; reader
records are plain dicts (``deal["company_id"]``); repository records are
dataclasses serialized with ``_row_to_dict``; timestamps are tz-aware.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from followup.profile.dependencies import PipelineDeps
from followup.profile.schemas import (
    ContactSummary,
    DealContext,
    ProfileNarrative,
    _row_to_dict,
)
from followup.profile.synthesis import synthesize_profile
from tracing import get_traceable

traceable = get_traceable()


class ProfileNotFound(Exception):
    """Raised when the opportunity id resolves to no CRM record."""


@dataclass
class _LoadedProfile:
    """Everything one load gathers, shared by both public methods."""

    deal: dict[str, Any]
    company: Optional[dict[str, Any]]
    contacts: list[ContactSummary]
    facts: list[Any]  # ProfileFact, newest-first
    relationships: list[Any]  # ProfileRelationship
    shadows: list[Any]  # ShadowEntity
    risk_score: Optional[float]
    activities: list[dict[str, Any]]
    narrative: str


class ProfileService:
    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps
        self._risk = deps.risk_snapshots

    @traceable(name="ProfileService.build_narrative", run_type="chain")
    async def build_profile_narrative(self, opportunity_id: str) -> ProfileNarrative:
        loaded = await self._load(opportunity_id)
        return ProfileNarrative(
            opportunity_id=opportunity_id,
            narrative=loaded.narrative,
            contacts=loaded.contacts,
            key_facts=[_row_to_dict(f) for f in loaded.facts[:20]],
            relationships=[_row_to_dict(r) for r in loaded.relationships],
            risk_score=loaded.risk_score,
            generated_at=datetime.now(timezone.utc),
        )

    @traceable(name="ProfileService.build_deal_context", run_type="chain")
    async def build_deal_context(
        self, opportunity_id: str, *, include_shadows: bool = True
    ) -> DealContext:
        # The orchestrator (Step 6) loads with include_shadows=False: shadow
        # entities have not yet earned a place in the deal picture the agents
        # reason over, so they are kept out of the synthesized narrative.
        loaded = await self._load(opportunity_id, include_shadows=include_shadows)
        open_concerns = [
            _row_to_dict(f) for f in loaded.facts if f.fact_type == "concern"
        ]
        return DealContext(
            opportunity_id=opportunity_id,
            opportunity_name=loaded.deal.get("name") or "",
            deal_stage=loaded.deal.get("stage") or "",
            deal_value=loaded.deal.get("value", 0.0),
            company_name=loaded.company["name"] if loaded.company else "",
            profile_narrative=loaded.narrative,
            contacts=loaded.contacts,
            recent_activities=loaded.activities,
            key_relationships=[_row_to_dict(r) for r in loaded.relationships],
            open_concerns=open_concerns,
            risk_score=loaded.risk_score,
        )

    # =======================================================================
    # Shared load
    # =======================================================================

    @traceable(name="ProfileService._load", run_type="retriever")
    async def _load(
        self, opportunity_id: str, *, include_shadows: bool = True
    ) -> _LoadedProfile:
        deps = self._deps
        opportunity_uuid = uuid.UUID(opportunity_id)

        deal = await deps.crm_reader.get_opportunity(opportunity_id)
        if deal is None:
            raise ProfileNotFound(f"No opportunity found for id {opportunity_id}")

        company_id = deal.get("company_id")
        company = await deps.crm_reader.get_company(company_id) if company_id else None
        raw_contacts = (
            await deps.crm_reader.get_contacts_for_company(company_id)
            if company_id
            else []
        )

        contacts = [await self._build_contact_summary(c) for c in raw_contacts]

        facts = await deps.facts.get_facts(
            opportunity_uuid, exclude_superseded=True, limit=100
        )
        relationships = await deps.relationships.get_relationships(opportunity_uuid)
        shadows = await deps.shadows.get_shadow_entities(
            opportunity_uuid, min_mentions=2
        )
        risk_score = await self._risk.get_latest_score(opportunity_uuid)
        activities = await deps.crm_reader.get_activities_for_opportunity(
            opportunity_id, limit=10
        )

        narrative = await synthesize_profile(
            deal=deal,
            company=company,
            contacts=contacts,
            shadows=shadows if include_shadows else [],
            facts=facts,
            relationships=relationships,
            risk_score=risk_score,
            chat_llm=deps.get_chat_llm(),
        )

        return _LoadedProfile(
            deal=deal,
            company=company,
            contacts=contacts,
            facts=facts,
            relationships=relationships,
            shadows=shadows,
            risk_score=risk_score,
            activities=activities,
            narrative=narrative,
        )

    async def _build_contact_summary(self, contact: dict[str, Any]) -> ContactSummary:
        facts = await self._deps.facts.get_facts_for_entity(
            entity_crm_id=contact["id"]
        )
        active = [f for f in facts if f.superseded_by is None]
        return ContactSummary(
            crm_id=contact["id"],
            name=contact["name"],
            role=contact.get("role"),
            email=contact.get("email"),
            facts=[_row_to_dict(f) for f in active],
        )


__all__ = ["ProfileService", "ProfileNotFound"]
