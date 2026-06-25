Step 3 — Profile Synthesis (Knowledge Graph Read Path)
Context
Build the read path for the Follow-Up Agent's knowledge graph. Given an opportunity id, assemble all profile data (CRM records, extracted facts, relationships, shadow entities, latest risk score) and synthesize a natural-language briefing for downstream agents.

Prerequisites (done): Step 1 (followup/store/repositories.py) and Step 2 (followup/profile/). You reuse those repositories and the PipelineDeps bundle + CRMReader adapter built in Step 2.

This step does NOT build: the orchestrator, API endpoints, or agent logic. It produces only ProfileNarrative and DealContext.

Conventions this step MUST follow (project reality)
Dependency injection via PipelineDeps. ProfileService takes the same PipelineDeps Step 2 uses — it already bundles crm_reader, facts, relationships, shadows repos, and get_chat_llm(). Do not instantiate repositories or readers ad hoc.
The reader returns plain dicts, not objects. crm_reader.get_opportunity(...) returns {"id", "name", "stage", "value", "company_id"}; contacts are {"id", "name", "role", "email", "company"}. Use dict access (deal["company_id"]), never deal.company_id.
Repository records are dataclasses (ProfileFact, ProfileRelationship, ShadowEntity). They have no .to_dict() — use the _row_to_dict helper (Part A) which asdict()s and makes uuid/datetime JSON-safe.
Reader access stays in one owner. As in Step 2, only ProfileService talks to the reader, through the injected CRMReader. No direct bridge calls.
LLM calls follow the project standard — followup.profile.llm.build_chat_llm / deps.get_chat_llm() (a langchain ChatOpenAI reading the shared LLM_* env). Synthesis is a single await chat_llm.ainvoke(...) returning free text (not JSON), so no parse_json_response and no LangGraph — one-shot prose, mirroring Orchestrator._summarize.
Timezone-aware timestamps: datetime.now(timezone.utc), never datetime.utcnow().
File Structure

followup/profile/
├── schemas.py     # ProfileNarrative, DealContext, ContactSummary, _row_to_dict
├── service.py     # ProfileService.build_profile_narrative() + build_deal_context()
└── synthesis.py   # synthesize_profile() — one ChatLLM call
Part A — Data Schemas (schemas.py)
Frozen contract — do not rename fields without coordinating with Steps 4/6.


from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class ContactSummary:
    crm_id: str
    name: str
    role: str | None
    email: str | None
    facts: list[dict]            # active profile_facts rows for this contact (_row_to_dict)

@dataclass
class DealContext:
    opportunity_id: str
    opportunity_name: str
    deal_stage: str
    deal_value: float            # dollars (reader converts amountMicros → float)
    company_name: str
    profile_narrative: str       # the synthesized briefing text
    contacts: list[ContactSummary]
    recent_activities: list[dict]
    key_relationships: list[dict]
    open_concerns: list[dict]    # facts where fact_type=='concern', not superseded
    risk_score: float | None     # latest from risk_snapshots, else None

@dataclass
class ProfileNarrative:
    opportunity_id: str
    narrative: str
    contacts: list[ContactSummary]
    key_facts: list[dict]        # top 20 active facts, newest first
    relationships: list[dict]
    risk_score: float | None
    generated_at: datetime       # timezone-aware
Add the serialization helper here (records carry uuid.UUID and datetime, which are not JSON-serializable):


import uuid
from dataclasses import asdict, is_dataclass

def _row_to_dict(record) -> dict:
    """Dataclass record → JSON-safe dict (uuid→str, datetime→isoformat)."""
    raw = asdict(record) if is_dataclass(record) else dict(record)
    return {k: _jsonify(v) for k, v in raw.items()}

def _jsonify(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
Part B — Profile Service (service.py)

class ProfileService:
    def __init__(self, deps: PipelineDeps) -> None:
        self._deps = deps
        # risk_snapshots is a separate table P3 writes; see Interface Additions.
        self._risk = RiskSnapshotRepository(deps.executor)

    async def build_profile_narrative(self, opportunity_id: str) -> ProfileNarrative: ...
    async def build_deal_context(self, opportunity_id: str) -> DealContext: ...
Both methods share one internal load (_load(opportunity_id)), so the reader/DB work happens once. This resolves the original spec's gap where DealContext needed fields (deal_value, company_name, recent_activities, open_concerns) that ProfileNarrative doesn't carry.

Load steps (real signatures)
Opportunity + company.

deal = await deps.crm_reader.get_opportunity(opportunity_id)   # None → raise ProfileNotFound
company = await deps.crm_reader.get_company(deal["company_id"]) if deal.get("company_id") else None
Contacts — opportunity contacts come via the company (the reader is company-scoped after Step 2):

contacts = await deps.crm_reader.get_contacts_for_company(deal["company_id"]) if deal.get("company_id") else []
(Caveat carried from Step 2: this is the company roster, not strictly deal-linked people. Narrowing to pointOfContact + linked people is a later refinement.)
ContactSummary per contact, with only active facts:

facts = await deps.facts.get_facts_for_entity(entity_crm_id=contact["id"])
active = [f for f in facts if f.superseded_by is None]
ContactSummary(contact["id"], contact["name"], contact.get("role"),
               contact.get("email"), [_row_to_dict(f) for f in active])
All opportunity facts: await deps.facts.get_facts(uuid(opportunity_id), exclude_superseded=True, limit=100) (already newest-first).
Relationships: await deps.relationships.get_relationships(uuid(opportunity_id)).
Shadow entities: await deps.shadows.get_shadow_entities(uuid(opportunity_id), min_mentions=2) (also returns any with title_or_role set).
Latest risk score: await self._risk.get_latest_score(uuid(opportunity_id)) → float | None (None until P3 has run — handle gracefully).
Open concerns: filter step-4 facts for fact_type == "concern" (already superseded-excluded).
Recent activities: await deps.crm_reader.get_activities_for_opportunity(opportunity_id, limit=10) (new reader method — Interface Additions).
Synthesize (Part C), passing the loaded data + deps.get_chat_llm().
Assemble ProfileNarrative (key_facts = [_row_to_dict(f) for f in all_facts[:20]], generated_at = datetime.now(timezone.utc)) and/or DealContext (deal_value = deal.get("value", 0.0), company_name = company["name"] if company else "", open_concerns/key_relationships/recent_activities via _row_to_dict).
Edge cases (return a simpler valid object, never raise except missing opportunity)
No contacts → contacts=[]. No facts/relationships/shadows → empty lists. No risk → None. The synthesis prompt must degrade to a short "early-stage / little data" briefing.

Part C — LLM Narrative Synthesis (synthesis.py)

async def synthesize_profile(
    *, deal: dict, company: dict | None,
    contacts: list[ContactSummary], shadows: list[ShadowEntity],
    facts: list[ProfileFact], relationships: list[ProfileRelationship],
    risk_score: float | None, chat_llm,                # deps.get_chat_llm()
) -> str:
Build one prompt embedding the structured data (deal name/stage/value/company; each contact + their facts and sentiment; shadows with roles/mention counts; relationships; open concerns; competitor/budget/deadline facts; most recent activity date; risk score). Reference people by name, not ids.
response = await chat_llm.ainvoke([SystemMessage(...), HumanMessage(prompt)]); return response.content.strip().
For more natural prose, build the model with a small temperature (build_chat_llm(deps.model, temperature=0.3)); pass it in rather than the default-0.0 client if desired.
Ask for 1–3 paragraphs of prose (not structured), covering: deal summary; primary contacts and stances (champion / skeptic / decision-maker); key risks & unresolved concerns; last activity (date + what); noteworthy shadow entities (esp. authority/decision power); risk-score context if present; competitor mentions.
Empty-data branch: if there are no facts/contacts, instruct the model to state plainly that the deal is early-stage with little intelligence gathered — keep it to a sentence or two.
(Keep the worked example from the original spec as the target style/length.)

Interface Additions required by this step
CRMReader.get_activities_for_opportunity(opportunity_id, limit=10) -> list[dict] — add to the CRMReader Protocol and ReaderAgentCRMReader (delegates to the reader agent's timeline composite, e.g. get_entity_timeline(entity_id, entity_type="opportunity")). Each item ≈ {"type", "date", "summary"}. TODO: confirm the reader's timeline field shapes against live output; fakes inject in tests.
CRMReader.get_opportunity enrichment — include "value" (float dollars; the adapter converts Twenty's amount.amountMicros / 1_000_000). Keep existing id/name/stage/company_id.
RiskSnapshotRepository (in followup/store/repositories.py) reading the existing followup_agent.risk_snapshots table (P3 writes it):

async def get_latest_score(self, opportunity_id: uuid.UUID) -> float | None:
    # SELECT score FROM followup_agent.risk_snapshots
    # WHERE opportunity_id=$1 ORDER BY computed_at DESC LIMIT 1  → float | None
Optionally add it to PipelineDeps.__post_init__ as self.risk_snapshots for consistency with the other repos.
Done When
ProfileService(deps).build_profile_narrative(opportunity_id) returns a ProfileNarrative with a readable narrative and all structured fields populated; build_deal_context(...) returns a fully-populated DealContext.
All list[dict] fields are JSON-safe (via _row_to_dict), since Step 7's API will serialize them.
Edge cases (no contacts / facts / relationships / risk) yield a simpler but valid object, not an error.
Reuses PipelineDeps, the real repository signatures, and ReaderAgentCRMReader — no new bridge calls, no .to_dict(), no datetime.utcnow().
Unit-testable with the same in-memory fakes pattern as tests/test_followup_extraction.py (inject a fake CRMReader, fake repos, and a fake chat model returning canned narrative text).
Key changes from the original I'd flag: get_contacts_for_opportunity/get_activities_for_opportunity didn't exist (mapped to get_contacts_for_company + a new timeline method); risk score reads the real risk_snapshots table rather than a fact_type='risk_score' fact (that value isn't in the fact_type CHECK); .to_dict()/utcnow() don't fit our dataclass records; and I added build_deal_context() so DealContext is actually constructible (the original "Done When" claimed it was buildable from ProfileNarrative, but that object lacks deal_value/company_name/recent_activities/open_concerns).

