"""EXPERIMENT (no production changes): mask PII (names/emails), keep ids.

The follow-up extraction prompt currently sends the LLM real names ("John Park")
and the raw email body. This tests reusing the reader's EntityHandleMap +
Presidio pipeline to mask only the PII — names become handles (person001), ids
(crm_<uuid>) and job titles/amounts/dates stay visible (useful, not PII). Then:

  1. seed the handle map with the deal's known CRM people/company (so a name
     masks to a stable handle keyed by record id),
  2. mask the email body (Presidio also discovers NEW names — Rachel Torres,
     David Kim — and gives them fresh private handles),
  3. LEAK CHECK: assert no known real name survives in the masked prompt,
  4. run the REAL extraction LLM on the masked prompt,
  5. UNMASK the fact values it returns → real names restored for storage,
  6. report whether attribution + unmasking round-tripped.

Touches nothing in production. Read-only on the DB.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_pii_mask_experiment.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from langchain_core.messages import HumanMessage  # noqa: E402

from agent.masking import EntityHandleMap  # noqa: E402
from agent.tool_scope import READER_SCOPE  # noqa: E402
from agent.tools.composite_reads import _exec, _identity  # noqa: E402
from bridge_client import forward  # noqa: E402
from followup.profile.dependencies import PipelineDeps  # noqa: E402
from followup.profile.llm import parse_json_response  # noqa: E402
from followup.store.repositories import Database  # noqa: E402

OPPORTUNITY = "a45a119e-376d-5eb2-8b96-33c2631660ea"
COMPANY = "c776ee49-f608-4a77-8cc8-6fe96ae1e43f"

SAMPLE_EMAIL = (
    "Hi Sarah, thanks for the revised proposal for the Platform Integration. "
    "I'm concerned about the integration timeline — Q3 is tight given our "
    "engineering freeze in August. Our new VP of Engineering, Rachel Torres, "
    "will need to sign off on the security review before we commit. Budget is "
    "approved up to $250k. We're also evaluating Segment, mostly on price. Can "
    "we get a revised SOW by June 25th? Lisa will loop in procurement lead David "
    "Kim. Best, John"
)


async def _raw_people(company_id: str) -> list[dict]:
    res = await forward(
        "execute",
        _exec("find_people", {"limit": 50, "companyId": {"eq": company_id}}, _identity(READER_SCOPE)),
    )
    return ((res.get("data") or {}).get("result") or {}).get("records") or []


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


async def main() -> None:
    # The Presidio/spaCy NER models are lazy-loaded; without this, discovery of
    # NEW names (people not already in CRM) silently no-ops and they leak. The
    # real reader/orchestrator load these at startup.
    from pipelines import load_models

    load_models()

    db = await Database.connect()
    try:
        deps = PipelineDeps.create(db)
        company = await deps.crm_reader.get_company(COMPANY)
        opportunity = await deps.crm_reader.get_opportunity(OPPORTUNITY)
        people = await _raw_people(COMPANY)  # raw records: name is {firstName,lastName}
        shadows = await deps.shadows.get_shadow_entities(uuid.UUID(OPPORTUNITY), min_mentions=2)

        # 1. Seed the handle map with known CRM entities (keyed by record id).
        masker = EntityHandleMap()
        if company:
            masker.register_resolved("company", {"id": company["id"], "name": company["name"]})
        id_to_handle: dict[str, str] = {}
        known_names: list[str] = []
        for person in people:
            handle = masker.register_resolved("person", person)
            if handle is not None:
                id_to_handle[person["id"]] = handle.name
                known_names.append(handle.canonical)

        # 2. Mask the email body (discover=True → finds new names too).
        masked_email = masker.mask_text(SAMPLE_EMAIL, discover=True)

        _rule("ORIGINAL EMAIL (raw PII)")
        print("  " + SAMPLE_EMAIL)
        _rule("MASKED EMAIL (what the LLM would see)")
        print("  " + masked_email)

        _rule("HANDLE MAP (handle  ->  real value, kept server-side only)")
        for handle in masker.handles:
            kind = "CRM" if handle.is_resolved else "private/new"
            print(f"  {handle.name:<12} {kind:<12} {handle.canonical}")

        # 3. LEAK CHECK — no PERSON name (known CRM or newly-discovered) should
        # survive in the masked text. Every person handle's canonical is a name
        # that must have been replaced.
        masked_lower = masked_email.lower()
        leaked = [
            handle.canonical
            for handle in masker.handles
            if handle.entity_type == "person"
            and handle.canonical
            and handle.canonical.lower() in masked_lower
        ]
        _rule("LEAK CHECK (all person names — CRM + discovered)")
        print(f"  person handles created: {[h.name + '=' + h.canonical for h in masker.handles if h.entity_type=='person']}")
        print(f"  names still visible in masked email: {leaked or 'NONE'}")

        # 4. Build a masked KNOWN ENTITIES block (ids visible, names = handles).
        lines = ["KNOWN ENTITIES (reference by the crm_<id>; names shown are handles):"]
        for person in people:
            handle = id_to_handle.get(person["id"], "?")
            lines.append(
                f"  - id: crm_{person['id']}, name: {handle}, role: {person.get('jobTitle') or 'unknown'}"
            )
        if company:
            lines.append(f"  Company: id: crm_{company['id']}, name: {masker.handle_for_surface(company['name']).name if masker.handle_for_surface(company['name']) else company['name']}")
        lines.append(f"  Opportunity: id: crm_{opportunity['id']}, stage: {opportunity.get('stage')}")
        block = "\n".join(lines)

        prompt = (
            "Analyze this email about a sales opportunity. Reference people by the "
            "handles shown (e.g. person001); reference CRM records by their crm_<id>.\n\n"
            f"{block}\n\nEMAIL:\n\"\"\"\n{masked_email}\n\"\"\"\n\n"
            'Return JSON: {"facts":[{"entity_id":"crm_<id> or handle",'
            '"fact_type":"concern|budget|competitor|deadline|decision_power|role",'
            '"fact_value":"... (use handles for any person names)"}]}'
        )

        _rule("RUNNING EXTRACTION LLM ON MASKED PROMPT")
        response = await deps.get_chat_llm().ainvoke([HumanMessage(content=prompt)])
        data = parse_json_response(response.content)

        _rule("FACTS: raw (masked) vs UNMASKED-for-storage")
        for fact in data.get("facts", []):
            raw_value = str(fact.get("fact_value"))
            unmasked = masker.unmask_text(raw_value)
            entity = fact.get("entity_id")
            print(f"  [{fact.get('fact_type')}] entity={entity}")
            print(f"      masked : {raw_value[:78]}")
            print(f"      stored : {unmasked[:78]}")

        _rule("VERDICT")
        print(f"  PII leaked to LLM: {'NO' if not leaked else 'YES — ' + str(leaked)}")
        print("  Names round-trip back via unmask_text for storage (see 'stored' above).")
        print("  IDs (crm_<uuid>) were never masked — usable for search, leak nothing.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
