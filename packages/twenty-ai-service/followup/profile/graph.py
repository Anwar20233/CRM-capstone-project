"""The extraction LangGraph — reader-resolved context, then one extraction LLM.

Topology::

    resolve_context ──► (ok?) ──► extract ──► END
                          └────────────────► END   (halt: no sender / no deal)

``resolve_context`` is the ONLY place the follow-up agent talks to the reader. It
anchors on the email sender: sender email → person → company → the company's open
deals (candidates) and contacts, plus our own shadow table. No LLM. Everything
downstream reuses that context and never contacts the reader again.

``extract`` (one LLM call) selects which candidate deal the message is about and
mines facts/relationships/unknown-persons for it. If the sender is unknown or the
company has no open deals, the graph halts before the LLM ever runs — there is
nothing to attribute facts to.

Persistence is NOT a node: the graph only produces structured output;
``extraction.py`` selects the final deal, validates, and writes to the database.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage

from followup.profile.dependencies import PipelineDeps
from followup.profile.llm import parse_json_response
from followup.profile.masking import ProfileMasker
from followup.profile.prompts import build_extraction_prompt, render_known_entities

# Free-text fields of the LLM's extraction output that may carry masked names and
# so must be unmasked before persistence. Id-bearing fields are excluded.
_FACT_TEXT_FIELDS = ("fact_value", "context_snippet", "source_snippet")
_RELATIONSHIP_TEXT_FIELDS = ("description",)
_PERSON_TEXT_FIELDS = ("name", "apparent_role", "company_context", "context_snippet")

# resolution_status values the graph can produce.
STATUS_OK = "ok"
STATUS_UNKNOWN_SENDER = "unknown_sender"
STATUS_NO_OPPORTUNITY = "no_opportunity"


class ExtractionState(TypedDict, total=False):
    # Inputs — exactly one of opportunity_id (direct) or sender_email (email).
    opportunity_id: Optional[str]
    sender_email: Optional[str]
    workspace_id: str
    source_type: str
    source_id: str
    source_text: str
    # Filled by resolve_context.
    sender: Optional[dict[str, Any]]
    company: Optional[dict[str, Any]]
    candidate_opportunities: list[dict[str, Any]]
    contacts: list[dict[str, Any]]
    shadows: list[Any]
    known_entities_block: str
    resolution_status: str
    # Filled by extract (LLM).
    opportunity_choice: Optional[str]
    facts: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    unknown_persons: list[dict[str, Any]]


async def _ainvoke_json(deps: PipelineDeps, prompt: str) -> dict[str, Any]:
    response = await deps.get_chat_llm().ainvoke([HumanMessage(content=prompt)])
    content = response.content if isinstance(response.content, str) else str(response.content)
    return parse_json_response(content)


def build_extraction_graph(deps: PipelineDeps):
    """Compile and return the extraction StateGraph bound to ``deps``."""
    from langgraph.graph import END, START, StateGraph

    async def resolve_context_node(state: ExtractionState) -> dict[str, Any]:
        if state.get("opportunity_id"):
            return await _resolve_direct(deps, state["opportunity_id"])
        return await _resolve_from_sender(deps, state.get("sender_email"))

    def route_after_resolve(state: ExtractionState) -> Literal["extract", "__end__"]:
        return "extract" if state.get("resolution_status") == STATUS_OK else END

    async def extract_node(state: ExtractionState) -> dict[str, Any]:
        # Mask PII (names/emails) before the LLM sees it; ids stay visible. Real
        # values are restored on the output so persistence stores real names.
        masker = ProfileMasker().register(
            contacts=state.get("contacts"), shadows=state.get("shadows")
        )
        prompt = build_extraction_prompt(
            state["source_type"],
            masker.mask(state["source_text"]),
            masker.mask_known(state["known_entities_block"]),
        )
        data = await _ainvoke_json(deps, prompt)
        return {
            "opportunity_choice": data.get("opportunity_id"),
            "facts": masker.unmask_fields(_as_list(data.get("facts")), _FACT_TEXT_FIELDS),
            "relationships": masker.unmask_fields(
                _as_list(data.get("relationships")), _RELATIONSHIP_TEXT_FIELDS
            ),
            "unknown_persons": masker.unmask_fields(
                _as_list(data.get("unknown_persons")), _PERSON_TEXT_FIELDS
            ),
        }

    builder = StateGraph(ExtractionState)
    builder.add_node("resolve_context", resolve_context_node)
    builder.add_node("extract", extract_node)

    builder.add_edge(START, "resolve_context")
    builder.add_conditional_edges(
        "resolve_context", route_after_resolve, {"extract": "extract", END: END}
    )
    builder.add_edge("extract", END)

    return builder.compile()


async def _resolve_direct(deps: PipelineDeps, opportunity_id: str) -> dict[str, Any]:
    """Direct path: the deal is already known (a single candidate)."""
    opportunity = await deps.crm_reader.get_opportunity(opportunity_id) or {
        "id": opportunity_id,
        "name": None,
        "stage": None,
    }
    company = None
    company_id = opportunity.get("company_id")
    if company_id:
        company = await deps.crm_reader.get_company(company_id)
    contacts = await deps.crm_reader.get_contacts_for_company(company_id) if company_id else []
    shadows = await _load_shadows(deps, [opportunity_id])

    return _resolved(
        sender=None,
        company=company,
        candidates=[opportunity],
        contacts=contacts,
        shadows=shadows,
        status=STATUS_OK,
    )


async def _resolve_from_sender(
    deps: PipelineDeps, sender_email: Optional[str]
) -> dict[str, Any]:
    """Email path: anchor on the sender, then derive company and candidate deals."""
    sender = await deps.crm_reader.get_person_by_email(sender_email) if sender_email else None
    if sender is None:
        return _resolved(None, None, [], [], [], STATUS_UNKNOWN_SENDER)

    company_id = sender.get("company_id")
    company = await deps.crm_reader.get_company(company_id) if company_id else None
    if company is None and company_id:
        company = {"id": company_id, "name": sender.get("company_name")}

    candidates = (
        await deps.crm_reader.get_open_opportunities_for_company(company_id)
        if company_id
        else []
    )
    if not candidates:
        return _resolved(sender, company, [], [], [], STATUS_NO_OPPORTUNITY)

    contacts = await deps.crm_reader.get_contacts_for_company(company_id)
    shadows = await _load_shadows(deps, [c.get("id") for c in candidates])
    return _resolved(sender, company, candidates, contacts, shadows, STATUS_OK)


async def _load_shadows(deps: PipelineDeps, opportunity_ids: list[Any]) -> list[Any]:
    """Union of active shadows across the candidate opportunities."""
    seen: dict[Any, Any] = {}
    for raw_id in opportunity_ids:
        opportunity_uuid = _coerce_uuid(raw_id)
        if opportunity_uuid is None:
            continue
        for shadow in await deps.shadows.list_active(opportunity_uuid):
            seen[shadow.id] = shadow
    return list(seen.values())


def _resolved(
    sender: Optional[dict[str, Any]],
    company: Optional[dict[str, Any]],
    candidates: list[dict[str, Any]],
    contacts: list[dict[str, Any]],
    shadows: list[Any],
    status: str,
) -> dict[str, Any]:
    return {
        "sender": sender,
        "company": company,
        "candidate_opportunities": candidates,
        "contacts": contacts,
        "shadows": shadows,
        "resolution_status": status,
        "known_entities_block": render_known_entities(
            sender, contacts, company, candidates, shadows
        ),
    }


def _as_list(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else []


def _coerce_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
