"""Entity resolution for the unknown persons an extraction surfaces.

The extraction LLM lists people it could not pin to a known id in
``unknown_persons``. This module decides, for each one, whether they are really
an existing CRM contact, an already-tracked shadow entity, or a brand-new shadow
worth creating — and never creates noise from a bare first name with nothing to
anchor it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from followup.profile.dependencies import PipelineDeps
from followup.profile.references import parse_label
from followup.profile.shadow import check_and_auto_promote, create_shadow
from followup.store.repositories import ShadowEntity

_ACTIVE_SHADOW_STATUSES = ("shadow", "detected", "pending_promotion")


@dataclass
class ResolutionResult:
    crm_matches: int = 0
    shadow_matches: int = 0
    shadows_created: int = 0
    unresolved: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _fuzzy_name_match(query: Optional[str], candidate: Optional[str]) -> bool:
    """Loose name match tolerant of first-name-only vs full-name mentions."""
    if not query or not candidate:
        return False
    query_normalized = query.casefold().strip()
    candidate_normalized = candidate.casefold().strip()
    if not query_normalized or not candidate_normalized:
        return False
    if query_normalized == candidate_normalized:
        return True
    if query_normalized in candidate_normalized or candidate_normalized in query_normalized:
        return True
    # First-name match (covers "John" vs "John Smith"); ignore single initials.
    query_first = query_normalized.split()[0]
    candidate_first = candidate_normalized.split()[0]
    return query_first == candidate_first and len(query_first) > 1


def _shadow_name_compatible(shadow: ShadowEntity, name: Optional[str]) -> bool:
    """Whether ``name`` could be the shadow's person — by name, never by title.

    Checks the canonical name and any aliases. A bare ``title_or_role`` is
    deliberately NOT consulted: two people can share a job title without being
    the same person.
    """
    if not name:
        return False
    candidates = [shadow.name, *(shadow.aliases or [])]
    return any(_fuzzy_name_match(name, candidate) for candidate in candidates)


async def resolve_unknown_persons(
    deps: PipelineDeps,
    opportunity_id: str,
    workspace_id: str,
    unknown_persons: list[dict[str, Any]],
    source_type: str,
    source_id: str,
    *,
    contacts: list[dict[str, Any]],
    company: Optional[dict[str, Any]],
) -> ResolutionResult:
    """Resolve each unknown person to a CRM contact, a shadow, or a new shadow.

    ``contacts`` and ``company`` are the known CRM context the ``load_context``
    node already fetched from the reader. They are passed in (never re-fetched)
    so the reader is contacted from exactly one place in the pipeline — the
    id-getting node — and resolution stays a pure post-processing step.
    """
    result = ResolutionResult()
    if not unknown_persons:
        return result

    for person in unknown_persons:
        await _resolve_one(deps, opportunity_id, workspace_id, person, contacts, company, result)
    return result


async def _resolve_one(
    deps: PipelineDeps,
    opportunity_id: str,
    workspace_id: str,
    person: dict[str, Any],
    contacts: list[dict[str, Any]],
    company: Optional[dict[str, Any]],
    result: ResolutionResult,
) -> None:
    name = (person.get("name") or "").strip()
    email = (person.get("email") or "").strip() or None
    role = (person.get("apparent_role") or "").strip() or None

    # 1. CRM contact match — strongest signal is an exact email, then a name.
    if _match_crm_contact(contacts, name, email) is not None:
        # Facts for this person were already attributed to their CRM id at
        # extraction time (the LLM had that id in context), so there is nothing
        # to re-attach here — recording the match is enough.
        result.crm_matches += 1
        return

    # 2. Existing shadow match.
    shadow = await _match_shadow(deps, opportunity_id, person, name, email)
    if shadow is not None:
        await _touch_shadow(deps, shadow, email, role)
        await check_and_auto_promote(deps, str(shadow.id))
        result.shadow_matches += 1
        return

    # 3. No match — create a shadow only with enough identifying information.
    company_crm_id = _company_crm_id_for(person, company)
    if name and (email or role or company_crm_id):
        created = await create_shadow(
            deps,
            opportunity_id=opportunity_id,
            workspace_id=workspace_id,
            name=name,
            email=email,
            role=role,
            company_crm_id=company_crm_id,
        )
        await check_and_auto_promote(deps, str(created.id))
        result.shadows_created += 1
        return

    # Just a bare first name with nothing to anchor it — count, don't persist.
    result.unresolved += 1


def _match_crm_contact(
    contacts: list[dict[str, Any]], name: str, email: Optional[str]
) -> Optional[dict[str, Any]]:
    if email:
        for contact in contacts:
            if (contact.get("email") or "").casefold() == email.casefold():
                return contact
    for contact in contacts:
        if _fuzzy_name_match(name, contact.get("name")):
            return contact
    return None


async def _match_shadow(
    deps: PipelineDeps,
    opportunity_id: str,
    person: dict[str, Any],
    name: str,
    email: Optional[str],
) -> Optional[ShadowEntity]:
    opportunity_uuid = _as_uuid(opportunity_id)

    # The LLM's explicit hint wins when it points at a still-active shadow — but
    # only if the NAMES are actually compatible. The model sometimes hints (or
    # attributes a fact to) a same-titled shadow for a clearly different person
    # ("Rachel Torres" → shadow "David", both "VP of Engineering"); a title is
    # not an identity, so we reject the hint unless the names line up.
    hinted = parse_label(person.get("likely_matches_shadow"))
    if hinted is not None and hinted[0] == "shadow":
        candidate = await deps.shadows.get(_as_uuid(hinted[1]))
        if (
            candidate is not None
            and candidate.status in _ACTIVE_SHADOW_STATUSES
            and _shadow_name_compatible(candidate, name)
        ):
            return candidate

    if email:
        by_email = await deps.shadows.find_by_email(opportunity_uuid, email)
        if by_email is not None and by_email.status in _ACTIVE_SHADOW_STATUSES:
            return by_email

    if name:
        for candidate in await deps.shadows.find_by_name_fuzzy(opportunity_uuid, name):
            if candidate.status in _ACTIVE_SHADOW_STATUSES:
                return candidate
    return None


async def _touch_shadow(
    deps: PipelineDeps,
    shadow: ShadowEntity,
    email: Optional[str],
    role: Optional[str],
) -> None:
    """Record a fresh mention and fill in newly-learned identity fields."""
    shadow.mention_count = (shadow.mention_count or 0) + 1
    shadow.last_seen_at = _now()
    if email and not shadow.email_address:
        shadow.email_address = email
    if role and not shadow.title_or_role:
        shadow.title_or_role = role
    await deps.shadows.save(shadow)


def _company_crm_id_for(
    person: dict[str, Any], company: Optional[dict[str, Any]]
) -> Optional[str]:
    """Map a person's free-text company_context onto the deal's known company."""
    if not company:
        return None
    context = (person.get("company_context") or "").strip()
    if context and _fuzzy_name_match(context, company.get("name")):
        return str(company.get("id"))
    return None
