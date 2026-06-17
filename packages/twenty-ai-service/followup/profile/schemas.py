"""Read-path data contracts for the Follow-Up Agent's profile synthesis.

These dataclasses are the frozen output of ``ProfileService`` (Step 3): a
``ProfileNarrative`` (the synthesized briefing plus the structured graph behind
it) and a ``DealContext`` (the richer bundle downstream agents consume). Steps
4/6 read these field names directly, so do not rename fields without coordinating
with them.

Repository records carry ``uuid.UUID`` and ``datetime`` values, neither of which
is JSON-serializable. Every ``list[dict]`` field below is produced by
``_row_to_dict`` so Step 7's API can serialize the objects as-is.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime


@dataclass
class ContactSummary:
    crm_id: str
    name: str
    role: str | None
    email: str | None
    facts: list[dict]  # active profile_facts rows for this contact (_row_to_dict)


@dataclass
class DealContext:
    opportunity_id: str
    opportunity_name: str
    deal_stage: str
    deal_value: float  # dollars (reader converts amountMicros → float)
    company_name: str
    profile_narrative: str  # the synthesized briefing text
    contacts: list[ContactSummary]
    recent_activities: list[dict]
    key_relationships: list[dict]
    open_concerns: list[dict]  # facts where fact_type=='concern', not superseded
    risk_score: float | None  # latest from risk_snapshots, else None


@dataclass
class ProfileNarrative:
    opportunity_id: str
    narrative: str
    contacts: list[ContactSummary]
    key_facts: list[dict]  # top 20 active facts, newest first
    relationships: list[dict]
    risk_score: float | None
    generated_at: datetime  # timezone-aware


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
