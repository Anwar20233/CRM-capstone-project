"""EXPERIMENT (no production changes): last-4-of-id masking for the follow-up LLM.

Today the follow-up extraction prompt labels entities with their FULL uuid
(``crm_<uuid>``) and real names. This script tries an alternative: mask each
entity as ``<kind>_<last4>`` (e.g. ``person_6db1``) and mask not-in-CRM entities
(shadows) with a "new" marker (``newperson_<last4>``), then:

  1. builds the masked reference map and checks for last-4 COLLISIONS,
  2. renders a masked KNOWN ENTITIES block,
  3. runs the REAL extraction LLM on a sample email with that block,
  4. decodes the handles the model echoes back to real ids,
  5. reports whether every referenced handle round-tripped (and any the model
     invented).

It does NOT touch references.py / prompts.py / the pipeline. Read-only on the DB.

Run from packages/twenty-ai-service:
    .venv/bin/python scripts/followup_mask_experiment.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import uuid
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

from followup.profile.dependencies import PipelineDeps  # noqa: E402
from followup.profile.llm import parse_json_response  # noqa: E402
from followup.store.repositories import Database  # noqa: E402

OPPORTUNITY = "a45a119e-376d-5eb2-8b96-33c2631660ea"  # Airbnb — Platform Integration

SAMPLE_EMAIL = (
    "Hi Sarah, thanks for the revised proposal for the Platform Integration. "
    "I'm concerned about the integration timeline — Q3 is tight given our "
    "engineering freeze in August. Our new VP of Engineering, Rachel Torres, "
    "will need to sign off on the security review before we commit. Budget is "
    "approved up to $250k. We're also evaluating Segment, mostly on price. Can "
    "we get a revised SOW by June 25th? Lisa will loop in procurement lead David "
    "Kim. Best, John"
)


# ===========================================================================
# Masking scheme under test
# ===========================================================================


def _last4(value: object) -> str:
    """Last 4 hex chars of an id (dashes stripped)."""
    return str(value).replace("-", "")[-4:]


@dataclass
class MaskMap:
    """Forward/reverse handle map for one prompt, with collision detection."""

    forward: dict[str, str]  # real_id -> handle
    reverse: dict[str, tuple[str, str]]  # handle -> (kind, real_id)
    collisions: list[str]

    @classmethod
    def build(cls, entities: list[tuple[str, str, bool]]) -> "MaskMap":
        # entities: (kind, real_id, is_new). kind in person/company/opportunity.
        forward: dict[str, str] = {}
        reverse: dict[str, tuple[str, str]] = {}
        collisions: list[str] = []
        for kind, real_id, is_new in entities:
            prefix = f"new{kind}" if is_new else kind
            handle = f"{prefix}_{_last4(real_id)}"
            if handle in reverse and reverse[handle][1] != real_id:
                collisions.append(handle)
            forward[real_id] = handle
            reverse[handle] = (kind, real_id)
        return cls(forward, reverse, collisions)

    def decode(self, handle: str) -> tuple[str, str] | None:
        return self.reverse.get(handle)


# ===========================================================================
# Masked prompt rendering (mirrors prompts.render_known_entities, masked ids)
# ===========================================================================


def _render_masked_block(
    contacts: list[dict], company: dict | None, opportunity: dict, shadows: list, mask: MaskMap
) -> str:
    lines = ["KNOWN ENTITIES (reference these by their handle, do NOT invent new ones):"]
    lines.append("  Contacts (existing CRM people):")
    for contact in contacts:
        handle = mask.forward[contact["id"]]
        lines.append(
            f"    - handle: {handle}, name: {contact.get('name')}, role: {contact.get('role') or 'unknown'}"
        )
    lines.append("  Shadow entities (mentioned before, NOT yet confirmed CRM contacts):")
    for shadow in shadows:
        handle = mask.forward[str(shadow.id)]
        lines.append(
            f"    - handle: {handle}, name: {shadow.name}, possible_role: {shadow.title_or_role or 'unknown'}"
        )
    if company:
        lines.append(f"  Company:\n    - handle: {mask.forward[company['id']]}, name: {company.get('name')}")
    lines.append(
        f"  Opportunity:\n    - handle: {mask.forward[opportunity['id']]}, "
        f"name: \"{opportunity.get('name')}\", stage: {opportunity.get('stage')}"
    )
    return "\n".join(lines)


def _build_prompt(block: str, email: str) -> str:
    return f"""\
You are analyzing an email about a sales opportunity.

{block}

EMAIL:
\"\"\"
{email}
\"\"\"

Extract facts as JSON. Reference each entity by its HANDLE exactly as shown
above (e.g. "person_6db1"). For a person named in the email who is NOT in KNOWN
ENTITIES, use the handle "unknown" and give their name in "subject_name".

{{
  "facts": [
    {{"entity": "<handle or 'unknown'>", "subject_name": "<name if unknown>",
      "fact_type": "concern|budget|competitor|deadline|decision_power|role",
      "fact_value": "..."}}
  ]
}}
"""


# ===========================================================================
# Experiment
# ===========================================================================


async def main() -> None:
    db = await Database.connect()
    try:
        deps = PipelineDeps.create(db)

        opportunity = await deps.crm_reader.get_opportunity(OPPORTUNITY)
        company = await deps.crm_reader.get_company(opportunity["company_id"])
        contacts = await deps.crm_reader.get_contacts_for_company(opportunity["company_id"])
        shadows = await deps.shadows.get_shadow_entities(uuid.UUID(OPPORTUNITY), min_mentions=2)

        # Build the masked map: CRM records are not new; shadows are "new".
        entities: list[tuple[str, str, bool]] = [("opportunity", opportunity["id"], False)]
        if company:
            entities.append(("company", company["id"], False))
        for contact in contacts:
            entities.append(("person", contact["id"], False))
        for shadow in shadows:
            entities.append(("person", str(shadow.id), True))
        mask = MaskMap.build(entities)

        print("=" * 78)
        print("  MASKED REFERENCE MAP (real id  ->  handle)")
        print("=" * 78)
        for kind, real_id, is_new in entities:
            tag = " [NEW/shadow]" if is_new else ""
            print(f"  {real_id}  ->  {mask.forward[real_id]}{tag}")
        print(f"\n  last-4 COLLISIONS: {mask.collisions or 'none'}  "
              f"(handle space per type = 16^4 = 65,536)")

        block = _render_masked_block(contacts, company, opportunity, shadows, mask)
        print("\n" + "=" * 78)
        print("  MASKED KNOWN ENTITIES BLOCK (what the LLM sees instead of uuids)")
        print("=" * 78)
        print(block)

        print("\n" + "=" * 78)
        print("  RUNNING EXTRACTION LLM WITH MASKED HANDLES")
        print("=" * 78)
        response = await deps.get_chat_llm().ainvoke(
            [__import__("langchain_core.messages", fromlist=["HumanMessage"]).HumanMessage(
                content=_build_prompt(block, SAMPLE_EMAIL)
            )]
        )
        data = parse_json_response(response.content)

        print("\n  DECODING the handles the model returned:")
        ok = bad = unknown = 0
        for fact in data.get("facts", []):
            handle = fact.get("entity")
            decoded = mask.decode(handle) if handle else None
            if handle == "unknown":
                unknown += 1
                status = f"NEW person '{fact.get('subject_name')}' (correctly flagged unmapped)"
            elif decoded:
                ok += 1
                status = f"OK -> {decoded[0]}:{decoded[1]}"
            else:
                bad += 1
                status = "!! INVALID handle (model invented it)"
            print(f"    [{fact.get('fact_type')}] {handle}  =>  {status}")
            print(f"        {str(fact.get('fact_value'))[:70]}")

        print("\n" + "=" * 78)
        print("  VERDICT")
        print("=" * 78)
        print(f"  decoded-to-real: {ok}   flagged-new(unknown): {unknown}   invalid: {bad}")
        print(f"  collisions in this prompt: {len(mask.collisions)}")
        print("  Round-trip works if invalid==0 and collisions==0.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
