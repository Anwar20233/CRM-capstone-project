"""Extraction pipeline entry points — the knowledge-graph write path.

Two ways in:

* ``extract_from_email`` — the real trigger. An email arrives; we know only the
  sender's address. The reader resolves sender → company → candidate deals, the
  extraction LLM picks the deal and mines it, and we persist. When the sender is
  unknown, the company has no open deal, or the deal is genuinely ambiguous, the
  pipeline HALTS and returns a structured outcome (the seam a later step uses to
  ask the rep which deal this is about).
* ``extract_from_source`` — the deal is already known (a direct request or risk
  sweep). Skips sender resolution; always produces an ``ExtractionResult``.

The LangGraph (``graph.py``) does the reader + LLM work; everything here is
deterministic deal-selection and persistence, so it stays unit-testable with
in-memory fakes (no LLM, no DB).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from followup.profile.dependencies import PipelineDeps
from followup.profile.graph import (
    STATUS_NO_OPPORTUNITY,
    STATUS_UNKNOWN_SENDER,
    build_extraction_graph,
)
from followup.profile.references import crm_label, parse_label, shadow_label
from followup.profile.resolution import resolve_unknown_persons
from followup.profile.taxonomy import (
    FACT_TYPES,
    RELATIONSHIP_TYPES,
    SENTIMENTS,
    normalize_source_type,
    source_priority,
)
from tracing import get_traceable

traceable = get_traceable()

_CONFIDENCE_PENALTY = 0.1
_DEFAULT_CONFIDENCE = 0.8

# Terminal statuses an email run can land on.
STATUS_EXTRACTED = "extracted"
STATUS_AMBIGUOUS = "ambiguous_opportunity"


@dataclass
class ExtractionResult:
    extraction_id: str
    facts_created: int = 0
    facts_superseded: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    shadows_created: int = 0
    shadows_updated: int = 0
    unresolved_mentions: int = 0


@dataclass
class FollowupExtractionOutcome:
    """Result of an email-triggered run — extraction OR a reason it halted.

    On a halt, ``candidate_opportunity_ids`` and ``reason`` carry everything a
    follow-up step needs to ask the rep a clarifying question (the escalation
    itself is out of scope here).
    """

    status: str  # extracted | unknown_sender | no_opportunity | ambiguous_opportunity
    extraction: Optional[ExtractionResult] = None
    opportunity_id: Optional[str] = None
    sender_crm_id: Optional[str] = None
    company_crm_id: Optional[str] = None
    candidate_opportunity_ids: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class _EntityRef:
    entity_type: str  # 'contact' | 'company' | 'opportunity' | 'shadow'
    crm_id: Optional[uuid.UUID]
    shadow_id: Optional[uuid.UUID]


# ===========================================================================
# Entry points
# ===========================================================================


@traceable(name="extract_from_email", run_type="chain")
async def extract_from_email(
    workspace_id: str,
    source_type: str,
    source_id: str,
    source_text: str,
    sender_email: str,
    *,
    deps: Optional[PipelineDeps] = None,
) -> FollowupExtractionOutcome:
    """Resolve the deal from the sender, then extract — or halt with a reason."""
    return await _with_deps(
        deps,
        lambda d: _resolve_and_extract(
            d,
            workspace_id=workspace_id,
            source_type=source_type,
            source_id=source_id,
            source_text=source_text,
            sender_email=sender_email,
            opportunity_id=None,
        ),
    )


@traceable(name="extract_from_source", run_type="chain")
async def extract_from_source(
    opportunity_id: str,
    workspace_id: str,
    source_type: str,
    source_id: str,
    source_text: str,
    *,
    deps: Optional[PipelineDeps] = None,
) -> ExtractionResult:
    """Extract for an already-known opportunity (direct/internal trigger)."""
    outcome = await _with_deps(
        deps,
        lambda d: _resolve_and_extract(
            d,
            workspace_id=workspace_id,
            source_type=source_type,
            source_id=source_id,
            source_text=source_text,
            sender_email=None,
            opportunity_id=opportunity_id,
        ),
    )
    if outcome.extraction is None:
        # The direct path always has exactly one candidate, so this is reachable
        # only if the run could not produce a log row — surface it loudly.
        raise RuntimeError(f"extraction did not complete: {outcome.status} ({outcome.reason})")
    return outcome.extraction


async def _with_deps(deps: Optional[PipelineDeps], run) -> FollowupExtractionOutcome:
    """Run ``run(deps)``, building+closing a default dependency bundle if needed."""
    if deps is not None:
        return await run(deps)
    from followup.store.repositories import Database

    database = await Database.connect()
    try:
        return await run(PipelineDeps.create(database))
    finally:
        await database.close()


# ===========================================================================
# Core flow
# ===========================================================================


@traceable(name="resolve_and_extract", run_type="chain")
async def _resolve_and_extract(
    deps: PipelineDeps,
    *,
    workspace_id: str,
    source_type: str,
    source_id: str,
    source_text: str,
    sender_email: Optional[str],
    opportunity_id: Optional[str],
) -> FollowupExtractionOutcome:
    graph = build_extraction_graph(deps)
    state = await graph.ainvoke(
        {
            "opportunity_id": opportunity_id,
            "sender_email": sender_email,
            "workspace_id": workspace_id,
            "source_type": source_type,
            "source_id": source_id,
            "source_text": source_text,
        }
    )

    sender = state.get("sender") or {}
    company = state.get("company") or {}
    candidates = state.get("candidate_opportunities") or []
    base = {
        "sender_crm_id": sender.get("id"),
        "company_crm_id": company.get("id"),
        "candidate_opportunity_ids": [str(c.get("id")) for c in candidates],
    }

    status = state.get("resolution_status")
    if status == STATUS_UNKNOWN_SENDER:
        return FollowupExtractionOutcome(
            status=STATUS_UNKNOWN_SENDER,
            reason=f"No CRM contact owns the sender email '{sender_email}'.",
            **base,
        )
    if status == STATUS_NO_OPPORTUNITY:
        return FollowupExtractionOutcome(
            status=STATUS_NO_OPPORTUNITY,
            reason="The sender's company has no open opportunity to attribute this to.",
            **base,
        )

    chosen_id = _select_opportunity(state.get("opportunity_choice"), candidates)
    if chosen_id is None:
        return FollowupExtractionOutcome(
            status=STATUS_AMBIGUOUS,
            reason="The message matches several open deals and gives no signal to choose between them.",
            **base,
        )

    result = await _persist(
        deps,
        state=state,
        chosen_opportunity_id=chosen_id,
        workspace_id=workspace_id,
        source_type=source_type,
        source_id=source_id,
        source_text=source_text,
    )
    return FollowupExtractionOutcome(
        status=STATUS_EXTRACTED,
        extraction=result,
        opportunity_id=chosen_id,
        **base,
    )


def _select_opportunity(
    choice_label: Any, candidates: list[dict[str, Any]]
) -> Optional[str]:
    """Resolve the extractor's deal choice to a candidate id.

    Honour an explicit, valid choice; fall back to the sole candidate; otherwise
    abstain (``None``) so the run halts rather than guessing.
    """
    candidate_ids = {str(c.get("id")) for c in candidates}
    parsed = parse_label(choice_label)
    if parsed is not None and parsed[0] == "crm" and parsed[1] in candidate_ids:
        return parsed[1]
    if len(candidates) == 1:
        return str(candidates[0].get("id"))
    return None


# ===========================================================================
# Persistence
# ===========================================================================


async def _persist(
    deps: PipelineDeps,
    *,
    state: dict[str, Any],
    chosen_opportunity_id: str,
    workspace_id: str,
    source_type: str,
    source_id: str,
    source_text: str,
) -> ExtractionResult:
    contacts = state.get("contacts") or []
    company = state.get("company")
    # Only shadows scoped to the chosen deal are valid fact/relationship targets.
    scoped_shadows = [
        s for s in (state.get("shadows") or []) if str(s.opportunity_id) == str(chosen_opportunity_id)
    ]
    references = _build_reference_index(
        contacts, company, {"id": chosen_opportunity_id}, scoped_shadows
    )

    opportunity_uuid = _coerce_uuid(chosen_opportunity_id)
    stored_source_type = normalize_source_type(source_type)
    source_uuid = _coerce_uuid(source_id)

    facts_created, facts_superseded = await _persist_facts(
        deps, state.get("facts") or [], references, opportunity_uuid, stored_source_type, source_uuid
    )
    relationships_created, relationships_updated = await _persist_relationships(
        deps, state.get("relationships") or [], references, opportunity_uuid, stored_source_type, source_uuid
    )

    resolution = await resolve_unknown_persons(
        deps,
        opportunity_id=chosen_opportunity_id,
        workspace_id=workspace_id,
        unknown_persons=state.get("unknown_persons") or [],
        source_type=source_type,
        source_id=source_id,
        contacts=contacts,
        company=company,
    )

    log = await deps.extractions.create(
        {
            "opportunity_id": opportunity_uuid,
            "workspace_id": _coerce_uuid(workspace_id),
            "trigger_type": source_type,
            "trigger_id": source_id,
            "input_summary": _summarize(source_text),
            "entities_found": len(contacts) + len(scoped_shadows),
            "facts_extracted": facts_created,
            "relationships_extracted": relationships_created,
            "shadow_entities_created": resolution.shadows_created,
            "unresolved_mentions": resolution.unresolved,
            "llm_model": _model_name(deps),
        }
    )

    return ExtractionResult(
        extraction_id=str(log.id),
        facts_created=facts_created,
        facts_superseded=facts_superseded,
        relationships_created=relationships_created,
        relationships_updated=relationships_updated,
        shadows_created=resolution.shadows_created,
        shadows_updated=resolution.shadow_matches,
        unresolved_mentions=resolution.unresolved,
    )


def _build_reference_index(
    contacts: list[dict[str, Any]],
    company: Optional[dict[str, Any]],
    opportunity: dict[str, Any],
    shadows: list[Any],
) -> dict[str, _EntityRef]:
    """Map every ``crm_``/``shadow_`` label to its resolved entity reference."""
    index: dict[str, _EntityRef] = {}
    for contact in contacts:
        crm_id = _coerce_uuid(contact.get("id"))
        if crm_id is not None:
            index[crm_label(contact.get("id"))] = _EntityRef("contact", crm_id, None)
    if company and company.get("id") is not None:
        crm_id = _coerce_uuid(company.get("id"))
        if crm_id is not None:
            index[crm_label(company.get("id"))] = _EntityRef("company", crm_id, None)
    opportunity_uuid = _coerce_uuid(opportunity.get("id"))
    if opportunity_uuid is not None:
        index[crm_label(opportunity.get("id"))] = _EntityRef("opportunity", opportunity_uuid, None)
    for shadow in shadows:
        shadow_id = _coerce_uuid(getattr(shadow, "id", None))
        if shadow_id is not None:
            index[shadow_label(shadow.id)] = _EntityRef("shadow", None, shadow_id)
    return index


async def _persist_facts(
    deps: PipelineDeps,
    raw_facts: list[dict[str, Any]],
    references: dict[str, _EntityRef],
    opportunity_id: uuid.UUID,
    source_type: str,
    source_id: Optional[uuid.UUID],
) -> tuple[int, int]:
    created_count = 0
    superseded_count = 0

    for raw in raw_facts:
        fact_type = raw.get("fact_type")
        if fact_type not in FACT_TYPES:
            continue
        reference = references.get(raw.get("entity_id"))
        if reference is None:
            continue
        fact_value = (raw.get("fact_value") or "").strip()
        if not fact_value:
            continue

        confidence, superseded = await _resolve_fact_conflicts(
            deps, reference, fact_type, fact_value, source_type
        )
        if confidence is None:
            continue  # identical fact already present

        created = await deps.facts.create(
            {
                "opportunity_id": opportunity_id,
                "entity_type": reference.entity_type,
                "entity_crm_id": reference.crm_id,
                "shadow_entity_id": reference.shadow_id,
                "fact_type": fact_type,
                "fact_value": fact_value,
                "confidence": confidence,
                "sentiment": _clean_sentiment(raw.get("sentiment")),
                "source_type": source_type,
                "source_id": source_id,
                "source_snippet": raw.get("context_snippet") or raw.get("source_snippet"),
            }
        )
        created_count += 1
        for old in superseded:
            await deps.facts.supersede(old.id, created.id)
            superseded_count += 1

    return created_count, superseded_count


async def _resolve_fact_conflicts(
    deps: PipelineDeps,
    reference: _EntityRef,
    fact_type: str,
    fact_value: str,
    source_type: str,
) -> tuple[Optional[float], list[Any]]:
    """Apply the conflict rules; return ``(confidence_or_None, facts_to_supersede)``."""
    base_confidence = _clamp_confidence(_DEFAULT_CONFIDENCE)
    existing = [
        fact
        for fact in await deps.facts.get_facts_for_entity(
            entity_crm_id=reference.crm_id,
            shadow_entity_id=reference.shadow_id,
            fact_type=fact_type,
        )
        if fact.superseded_by is None
    ]

    if any(fact.fact_value == fact_value for fact in existing):
        return None, []

    conflicts = [fact for fact in existing if fact.fact_value != fact_value]
    to_supersede: list[Any] = []
    discount_new = False

    for old in conflicts:
        if old.source_type == source_type:
            to_supersede.append(old)  # same channel → newer wins
            continue
        new_priority = source_priority(source_type)
        old_priority = source_priority(old.source_type)
        if new_priority > old_priority:
            to_supersede.append(old)
        elif new_priority < old_priority:
            discount_new = True  # new is weaker → keep both, discount new
        else:
            discount_new = True  # ambiguous tie → keep both, discount both
            old.confidence = _clamp_confidence((old.confidence or _DEFAULT_CONFIDENCE) - _CONFIDENCE_PENALTY)
            await deps.facts.save(old)

    confidence = base_confidence - _CONFIDENCE_PENALTY if discount_new else base_confidence
    return _clamp_confidence(confidence), to_supersede


async def _persist_relationships(
    deps: PipelineDeps,
    raw_relationships: list[dict[str, Any]],
    references: dict[str, _EntityRef],
    opportunity_id: uuid.UUID,
    source_type: str,
    source_id: Optional[uuid.UUID],
) -> tuple[int, int]:
    created_count = 0
    updated_count = 0

    existing = await deps.relationships.get_relationships(opportunity_id)
    existing_by_key = {_relationship_key_of(rel): rel for rel in existing}

    for raw in raw_relationships:
        relationship_type = raw.get("type")
        if relationship_type not in RELATIONSHIP_TYPES:
            continue
        from_ref = references.get(raw.get("from_id"))
        to_ref = references.get(raw.get("to_id"))
        if from_ref is None or to_ref is None:
            continue

        key = (from_ref.crm_id, from_ref.shadow_id, to_ref.crm_id, to_ref.shadow_id, relationship_type)
        duplicate = existing_by_key.get(key)
        if duplicate is not None:
            duplicate.last_seen_at = _utcnow()
            await deps.relationships.save(duplicate)
            updated_count += 1
            continue

        created = await deps.relationships.create(
            {
                "opportunity_id": opportunity_id,
                "from_entity_crm_id": from_ref.crm_id,
                "from_shadow_id": from_ref.shadow_id,
                "to_entity_crm_id": to_ref.crm_id,
                "to_shadow_id": to_ref.shadow_id,
                "relationship_type": relationship_type,
                "description": raw.get("description"),
                "confidence": _DEFAULT_CONFIDENCE,
                "source_type": source_type,
                "source_id": source_id,
            }
        )
        existing_by_key[key] = created
        created_count += 1

    return created_count, updated_count


def _relationship_key_of(rel: Any) -> tuple:
    return (
        rel.from_entity_crm_id,
        rel.from_shadow_id,
        rel.to_entity_crm_id,
        rel.to_shadow_id,
        rel.relationship_type,
    )


# ===========================================================================
# Small helpers
# ===========================================================================


def _clean_sentiment(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value in SENTIMENTS else None


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _coerce_uuid(value: object) -> Optional[uuid.UUID]:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _summarize(text: str, limit: int = 280) -> str:
    return " ".join((text or "").split())[:limit]


def _model_name(deps: PipelineDeps) -> Optional[str]:
    if deps.model:
        return deps.model
    model = getattr(deps.chat_llm, "model_name", None) or getattr(deps.chat_llm, "model", None)
    return str(model) if model else None


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


__all__ = [
    "ExtractionResult",
    "FollowupExtractionOutcome",
    "extract_from_email",
    "extract_from_source",
]
