"""LLM narrative synthesis for the Follow-Up Agent's read path.

One shot of prose, not structured output: given the assembled knowledge graph
(deal, contacts and their facts, shadow entities, relationships, open concerns,
last activity, risk score), the model writes a 1–3 paragraph briefing a sales rep
could read before a call. This mirrors ``Orchestrator._summarize`` — a single
``ainvoke`` returning free text, no JSON parsing and no LangGraph.

People are always referenced by name, never by id: the structured ids stay in the
``ProfileNarrative`` fields; the narrative is for humans.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from followup.profile.masking import ProfileMasker
from followup.profile.schemas import ContactSummary
from followup.store.repositories import ProfileFact, ProfileRelationship, ShadowEntity

_SYSTEM_PROMPT = (
    "You are a sales intelligence analyst briefing an account executive before "
    "they engage a deal. Write tight, factual prose a busy rep can skim — no "
    "headings, no bullet lists, no preamble. Reference people by name, never by "
    "id. Only state what the data supports; do not invent facts."
)


async def synthesize_profile(
    *,
    deal: dict,
    company: dict | None,
    contacts: list[ContactSummary],
    shadows: list[ShadowEntity],
    facts: list[ProfileFact],
    relationships: list[ProfileRelationship],
    risk_score: float | None,
    chat_llm,  # deps.get_chat_llm()
) -> str:
    """Synthesize a natural-language deal briefing from the loaded graph."""
    prompt = _build_prompt(
        deal=deal,
        company=company,
        contacts=contacts,
        shadows=shadows,
        facts=facts,
        relationships=relationships,
        risk_score=risk_score,
    )
    # Mask PII before the LLM, then restore real names in the briefing — the
    # narrative is for a human rep, so it must read with real names.
    masker = ProfileMasker().register(
        contacts=[{"id": c.crm_id, "name": c.name, "email": c.email} for c in contacts],
        shadows=shadows,
    )
    response = await chat_llm.ainvoke(
        [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=masker.mask(prompt))]
    )
    return masker.unmask((response.content or "")).strip()


# ===========================================================================
# Prompt construction
# ===========================================================================


def _build_prompt(
    *,
    deal: dict,
    company: dict | None,
    contacts: list[ContactSummary],
    shadows: list[ShadowEntity],
    facts: list[ProfileFact],
    relationships: list[ProfileRelationship],
    risk_score: float | None,
) -> str:
    has_intelligence = bool(facts or contacts or shadows or relationships)
    if not has_intelligence:
        return _empty_prompt(deal, company)

    company_name = (company or {}).get("name") or "an unknown company"
    value = deal.get("value")
    value_part = f", worth roughly ${value:,.0f}" if value else ""
    header = (
        f'DEAL: "{deal.get("name") or "unknown"}" with {company_name} '
        f'(stage: {deal.get("stage") or "unknown"}{value_part}).'
    )

    sections: list[str] = [header]

    sections.append("CONTACTS AND WHAT WE KNOW ABOUT THEM:")
    if contacts:
        sections.extend(_contact_block(contact) for contact in contacts)
    else:
        sections.append("  (no CRM contacts on this deal yet)")

    concerns = [f for f in facts if f.fact_type == "concern"]
    if concerns:
        sections.append("OPEN CONCERNS / UNRESOLVED ISSUES:")
        sections.extend(f"  - {_fact_text(f)}" for f in concerns)

    notable = [
        f for f in facts if f.fact_type in ("competitor", "budget", "deadline")
    ]
    if notable:
        sections.append("COMPETITORS, BUDGET, AND DEADLINES:")
        sections.extend(f"  - {_fact_text(f)}" for f in notable)

    if shadows:
        sections.append("SHADOW ENTITIES (people mentioned but not yet CRM contacts):")
        sections.extend(f"  - {_shadow_text(s)}" for s in shadows)

    if relationships:
        sections.append("RELATIONSHIPS:")
        sections.extend(f"  - {_relationship_text(r)}" for r in relationships)

    last_activity = _latest_activity_date(facts, relationships)
    if last_activity:
        sections.append(f"MOST RECENT ACTIVITY DATE: {last_activity}")

    if risk_score is not None:
        sections.append(f"RISK SCORE: {risk_score:g} (higher means more at risk).")

    sections.append(
        "\nWrite a 1–3 paragraph briefing covering: a one-line deal summary; the "
        "primary contacts and their stance (champion, skeptic, decision-maker); "
        "the key risks and unresolved concerns; the last activity (date and what "
        "it was) if known; any noteworthy shadow entities, especially anyone with "
        "decision authority; the risk score in context if present; and any "
        "competitor mentions. Reference everyone by name."
    )
    return "\n".join(sections)


def _empty_prompt(deal: dict, company: dict | None) -> str:
    company_name = (company or {}).get("name") or "an unknown company"
    return (
        f'DEAL: "{deal.get("name") or "unknown"}" with {company_name} '
        f'(stage: {deal.get("stage") or "unknown"}).\n'
        "No contacts, facts, relationships, or other intelligence have been "
        "gathered for this deal yet.\n\n"
        "In one or two sentences, state plainly that this is an early-stage deal "
        "with little intelligence gathered so far, naming the deal and company. "
        "Do not speculate beyond that."
    )


def _contact_block(contact: ContactSummary) -> str:
    role = contact.role or "unknown role"
    line = f"  - {contact.name} ({role})"
    if not contact.facts:
        return line + ": no facts recorded."
    fact_lines = "; ".join(_fact_dict_text(f) for f in contact.facts)
    return f"{line}: {fact_lines}"


def _fact_dict_text(fact: dict) -> str:
    value = fact.get("fact_value") or ""
    fact_type = fact.get("fact_type") or "fact"
    sentiment = fact.get("sentiment")
    sentiment_part = f" [{sentiment}]" if sentiment else ""
    return f"{fact_type}: {value}{sentiment_part}"


def _fact_text(fact: ProfileFact) -> str:
    sentiment_part = f" [{fact.sentiment}]" if fact.sentiment else ""
    return f"{fact.fact_value}{sentiment_part}"


def _shadow_text(shadow: ShadowEntity) -> str:
    role = shadow.title_or_role or "role unknown"
    return f"{shadow.name} ({role}), mentioned {shadow.mention_count}x"


def _relationship_text(rel: ProfileRelationship) -> str:
    detail = f" — {rel.description}" if rel.description else ""
    return f"{rel.relationship_type}{detail}"


def _latest_activity_date(
    facts: list[ProfileFact], relationships: list[ProfileRelationship]
) -> str | None:
    """Most recent timestamp across facts/relationships, as an ISO date string."""
    timestamps = [f.extracted_at for f in facts if f.extracted_at is not None]
    timestamps += [r.last_seen_at for r in relationships if r.last_seen_at is not None]
    if not timestamps:
        return None
    return max(timestamps).date().isoformat()


__all__ = ["synthesize_profile"]
