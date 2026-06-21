"""Prompt construction for the extraction LLM.

The reader stage has already done the deterministic id work: the email sender is
resolved to a CRM person, their company is known, and the company's open deals
are listed as candidates. This prompt asks the model to do the two things only it
can: decide WHICH of those deals the message is about (from context), and mine
the facts/relationships/unknown-persons for that deal.

Selecting the deal is the model's job because the email almost never names it —
it has to be inferred from products, amounts, people, and timing. When several
deals are plausible and the text gives no signal, the model must abstain (return
a null opportunity) rather than guess; the pipeline then halts instead of
attributing facts to the wrong deal.
"""

from __future__ import annotations

from typing import Any, Optional

from followup.profile.references import crm_label, shadow_label
from followup.profile.taxonomy import FACT_TYPES, RELATIONSHIP_TYPES

_FACT_TYPE_LIST = " | ".join(sorted(FACT_TYPES))
_RELATIONSHIP_TYPE_LIST = " | ".join(sorted(RELATIONSHIP_TYPES))


def _contact_line(contact: dict[str, Any]) -> str:
    label = crm_label(contact.get("id"))
    name = contact.get("name") or "unknown"
    role = contact.get("role") or "unknown"
    company = contact.get("company") or "unknown"
    email = contact.get("email")
    email_part = f", email: {email}" if email else ""
    return f"    - id: {label}, name: {name}, role: {role}, company: {company}{email_part}"


def _shadow_line(shadow: Any) -> str:
    label = shadow_label(getattr(shadow, "id", None))
    name = getattr(shadow, "name", None) or "unknown"
    role = getattr(shadow, "title_or_role", None) or "unknown"
    mentions = getattr(shadow, "mention_count", 0)
    return f"    - id: {label}, name: {name}, possible_role: {role}, mentions: {mentions}"


def _opportunity_line(opportunity: dict[str, Any]) -> str:
    label = crm_label(opportunity.get("id"))
    name = opportunity.get("name") or "unknown"
    stage = opportunity.get("stage") or "unknown"
    return f'    - id: {label}, name: "{name}", stage: "{stage}"'


def render_known_entities(
    sender: Optional[dict[str, Any]],
    contacts: list[dict[str, Any]],
    company: Optional[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    shadows: list[Any],
) -> str:
    """Render the KNOWN ENTITIES block the extractor references by id."""
    lines: list[str] = ["KNOWN ENTITIES (reference these by their IDs, do NOT create new ones):"]

    lines.append("  Sender (who wrote this message):")
    if sender and sender.get("id"):
        role = sender.get("role") or "unknown"
        lines.append(
            f"    - id: {crm_label(sender.get('id'))}, name: {sender.get('name') or 'unknown'}, role: {role}"
        )
    else:
        lines.append("    (sender email did not match a known CRM contact)")

    lines.append("  Contacts:")
    if contacts:
        lines.extend(_contact_line(contact) for contact in contacts)
    else:
        lines.append("    (none on file for this company)")

    lines.append("  Shadow entities:")
    if shadows:
        lines.extend(_shadow_line(shadow) for shadow in shadows)
    else:
        lines.append("    (none tracked yet)")

    if company:
        lines.append("  Company:")
        lines.append(f'    - id: {crm_label(company.get("id"))}, name: "{company.get("name") or "unknown"}"')

    lines.append("  Candidate opportunities (pick the ONE this message is about):")
    if opportunities:
        lines.extend(_opportunity_line(opportunity) for opportunity in opportunities)
    else:
        lines.append("    (none)")
    return "\n".join(lines)


# The static extraction instructions (decision rules + JSON schema + mining
# rules). Hoisted to a module constant so the DSPy prompt optimizer
# (optimization/followup) can swap a candidate in via ``system_prompt`` without
# touching the dynamic header / known-entities / source-text assembly. Phrased in
# terms of "this message" so it needs no ``.format`` substitution — a GEPA
# candidate with stray braces can't break rendering.
EXTRACTION_INSTRUCTIONS = f"""\
STEP 1 — Decide which opportunity this message is about ("opportunity_id"):
- If exactly one candidate opportunity is listed, use its id.
- If several are listed, pick the one the text is about based on context \
(products, projects, amounts, people, timing, the sender's role).
- If several are listed and the text gives NO signal to choose between them, set \
"opportunity_id" to null and return empty facts and relationships. Do not guess.

STEP 2 — Extract for the chosen opportunity, as JSON:

{{
  "opportunity_id": "crm_XXX or null",
  "facts": [
    {{
      "entity_id": "crm_XXX or shadow_XXX",
      "fact_type": "{_FACT_TYPE_LIST}",
      "fact_value": "the specific fact",
      "confidence": 0.0-1.0,
      "sentiment": "positive | negative | neutral | null"
    }}
  ],
  "relationships": [
    {{
      "from_id": "entity_id",
      "to_id": "entity_id",
      "type": "{_RELATIONSHIP_TYPE_LIST}",
      "description": "optional detail"
    }}
  ],
  "unknown_persons": [
    {{
      "name": "full name if available, first name if not",
      "email": "if visible in headers or signature",
      "apparent_role": "if mentioned",
      "company_context": "which company they seem to belong to",
      "context_snippet": "the sentence where they were mentioned",
      "likely_matches_shadow": "shadow_XXX or null"
    }}
  ]
}}

RULES:
- The sender wrote this message; attribute facts about the author to the sender's id.
- Only extract facts that are specific and actionable for a sales rep.
- Do NOT extract greetings, pleasantries, or logistical noise.
- Match a person to a KNOWN ENTITY id ONLY when the NAME matches. A shared job \
title or role is NOT a match — two different people can both be a "VP of \
Engineering". Never reuse an existing contact/shadow id for a differently-named \
person just because their roles line up.
- If a person is named (e.g. a full name) but is NOT in KNOWN ENTITIES, they are \
NEW: add them to unknown_persons AND attribute any fact about them to the \
opportunity id — do NOT attach it to a similarly-titled existing contact or shadow.
- If someone is mentioned by first name only with no role, email, or other \
identifier, put them in unknown_persons.
- Only set likely_matches_shadow when the NAMES refer to the same person (e.g. \
a first-name-only mention matching a known shadow's full name); a matching role \
alone is not enough.
- Confidence: 0.9+ for direct statements, 0.6-0.8 for inferred, below 0.6 for \
speculation."""


def build_extraction_prompt(
    source_type: str,
    source_text: str,
    known_entities_block: str,
    system_prompt: str | None = None,
) -> str:
    """Build the extraction prompt: select the deal, then extract for it."""
    instructions = system_prompt or EXTRACTION_INSTRUCTIONS
    return f"""\
You are analyzing a {source_type} related to an active sales opportunity.

{known_entities_block}

SOURCE TEXT:
\"\"\"
{source_text}
\"\"\"

{instructions}"""
