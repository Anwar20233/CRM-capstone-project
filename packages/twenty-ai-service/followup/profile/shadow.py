"""Shadow entity lifecycle: create, merge, and auto-promotion.

A *shadow entity* is a person the agent has heard about but who is not yet in
the CRM. As more sources mention them — and as their seniority or buying power
becomes clear — a shadow crosses a threshold and is auto-promoted into a real
Twenty contact. This module owns those state transitions; ``resolution.py`` and
``extraction.py`` call into it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from followup.profile.dependencies import PipelineDeps
from followup.profile.taxonomy import PROMOTION_TITLE_KEYWORDS
from followup.store.repositories import ShadowEntity

# Auto-promote once this many distinct extraction runs have touched a shadow.
_MENTION_PROMOTION_THRESHOLD = 3

# Fact types that, on their own, signal a shadow is worth promoting.
_PROMOTION_FACT_TYPES = ("decision_power", "buying_signal")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _has_authority_title(role: Optional[str]) -> bool:
    """True if a role string contains any seniority/authority keyword."""
    if not role:
        return False
    lowered = role.casefold()
    return any(keyword in lowered for keyword in PROMOTION_TITLE_KEYWORDS)


async def create_shadow(
    deps: PipelineDeps,
    opportunity_id: str,
    workspace_id: str,
    name: str,
    email: Optional[str] = None,
    role: Optional[str] = None,
    company_crm_id: Optional[str] = None,
    aliases: Optional[list[str]] = None,
) -> ShadowEntity:
    """Create a shadow entity, or update the existing one with the same email.

    The ``(opportunity_id, email)`` uniqueness rule from the schema is honoured
    here in code so we never trip the DB constraint: a known email means we have
    seen this person before, so we enrich that row instead of inserting a clash.
    """
    if email:
        existing = await deps.shadows.find_by_email(_as_uuid(opportunity_id), email)
        if existing is not None:
            return await _enrich_existing(deps, existing, name, role, company_crm_id)

    shadow_data = {
        "opportunity_id": _as_uuid(opportunity_id),
        "workspace_id": _as_uuid(workspace_id),
        "name": name,
        "email_address": email,
        "title_or_role": role,
        "company_crm_id": _as_uuid(company_crm_id) if company_crm_id else None,
        "aliases": aliases or [],
        "mention_count": 1,
        "status": "shadow",
    }
    return await deps.shadows.create(shadow_data)


async def _enrich_existing(
    deps: PipelineDeps,
    existing: ShadowEntity,
    name: str,
    role: Optional[str],
    company_crm_id: Optional[str],
) -> ShadowEntity:
    """Fill in identity fields the existing shadow lacked; keep it current."""
    if role and not existing.title_or_role:
        existing.title_or_role = role
    if company_crm_id and not existing.company_crm_id:
        existing.company_crm_id = _as_uuid(company_crm_id)
    if name and name != existing.name:
        aliases = list(existing.aliases or [])
        if name not in aliases:
            aliases.append(name)
        existing.aliases = aliases
    existing.last_seen_at = _now()
    return await deps.shadows.save(existing)


async def merge_shadows(
    deps: PipelineDeps, keep_id: str, merge_id: str
) -> ShadowEntity:
    """Fold the shadow ``merge_id`` into ``keep_id`` and return the kept entity.

    Used when masking hid that two shadows were the same person (e.g. "John" and
    "John Smith"). All facts and relationships are repointed, identity fields are
    unioned, and the merged shadow is tombstoned with status ``merged``.
    """
    keep_uuid = _as_uuid(keep_id)
    merge_uuid = _as_uuid(merge_id)

    keep = await deps.shadows.get(keep_uuid)
    merged = await deps.shadows.get(merge_uuid)
    if keep is None or merged is None:
        raise ValueError("merge_shadows: both shadow ids must exist")

    # Repoint the knowledge graph from the merged entity to the kept one.
    await deps.facts.reassign_shadow(merge_uuid, keep_uuid)
    await deps.relationships.reassign_shadow(merge_uuid, keep_uuid)

    # Union aliases, and record the merged entity's name as an alias of the kept.
    aliases = list(keep.aliases or [])
    for alias in list(merged.aliases or []) + [merged.name]:
        if alias and alias not in aliases and alias != keep.name:
            aliases.append(alias)
    keep.aliases = aliases

    keep.mention_count = (keep.mention_count or 0) + (merged.mention_count or 0)
    keep.first_seen_at = _earliest(keep.first_seen_at, merged.first_seen_at)
    keep.last_seen_at = _latest(keep.last_seen_at, merged.last_seen_at)

    # Copy over identity fields the kept entity is missing.
    if not keep.email_address and merged.email_address:
        keep.email_address = merged.email_address
    if not keep.title_or_role and merged.title_or_role:
        keep.title_or_role = merged.title_or_role
    if not keep.company_crm_id and merged.company_crm_id:
        keep.company_crm_id = merged.company_crm_id

    merged.status = "merged"
    await deps.shadows.save(merged)
    return await deps.shadows.save(keep)


def _earliest(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    return min((d for d in (a, b) if d is not None), default=None)


def _latest(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    return max((d for d in (a, b) if d is not None), default=None)


async def check_and_auto_promote(deps: PipelineDeps, shadow_id: str) -> bool:
    """Promote a shadow to a real CRM contact if it crosses any threshold.

    Called after every extraction that touches a shadow. Returns ``True`` when a
    promotion happened. Triggers (any one suffices): an authority-signalling
    title, a decision-power/buying-signal fact, three or more mentions, or a now
    known email address.
    """
    shadow_uuid = _as_uuid(shadow_id)
    shadow = await deps.shadows.get(shadow_uuid)
    if shadow is None or shadow.status in ("promoted", "dismissed", "merged"):
        return False

    if not await _should_promote(deps, shadow):
        return False

    contact = await deps.crm_orchestrator.create_contact(
        name=shadow.name,
        email=shadow.email_address,
        role=shadow.title_or_role,
        company_id=str(shadow.company_crm_id) if shadow.company_crm_id else None,
        workspace_id=str(shadow.workspace_id),
        initiated_by="followup_agent",
    )
    crm_id = _as_uuid(contact["id"])

    # Tombstone the shadow as promoted, pointing at its new CRM record.
    shadow.status = "promoted"
    shadow.promoted_to_crm_id = crm_id
    shadow.promoted_at = _now()
    await deps.shadows.save(shadow)

    # Stamp the CRM id onto the shadow's facts/relationships (shadow id kept for audit).
    await deps.facts.attach_crm_id(shadow_uuid, crm_id)
    await deps.relationships.attach_crm_id(shadow_uuid, crm_id)

    await deps.notifier.notify_rep(
        workspace_id=str(shadow.workspace_id),
        opportunity_id=str(shadow.opportunity_id),
        event_type="contact_auto_added",
        payload={
            "shadow_entity_id": str(shadow_uuid),
            "crm_contact_id": str(crm_id),
            "name": shadow.name,
            "email": shadow.email_address,
            "role": shadow.title_or_role,
        },
    )
    return True


async def _should_promote(deps: PipelineDeps, shadow: ShadowEntity) -> bool:
    """Evaluate the promotion triggers for a shadow (any one is enough)."""
    if _has_authority_title(shadow.title_or_role):
        return True
    if (shadow.mention_count or 0) >= _MENTION_PROMOTION_THRESHOLD:
        return True
    if shadow.email_address:
        return True
    for fact_type in _PROMOTION_FACT_TYPES:
        facts = await deps.facts.get_facts_for_entity(
            shadow_entity_id=shadow.id, fact_type=fact_type
        )
        if facts:
            return True
    return False
