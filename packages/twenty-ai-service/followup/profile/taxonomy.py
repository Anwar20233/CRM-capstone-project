"""Constrained taxonomies and source-priority rules for the extraction pipeline.

These mirror the CHECK constraints declared in
``followup/store/migrations/001_initial.sql``. The extraction LLM is asked to
emit only these values; anything outside them is dropped before it reaches the
database (a bad enum would otherwise fail the INSERT). Keeping the lists here —
not inline in the prompt or the persistence code — means the prompt, validation,
and conflict rules all read from one source of truth.
"""

from __future__ import annotations

# Allowed `fact_type` values the extraction prompt may emit. This is the spec
# taxonomy; the DB additionally tolerates a few legacy seeded values, but the
# pipeline only ever produces these ten.
FACT_TYPES: frozenset[str] = frozenset(
    {
        "role",
        "sentiment",
        "concern",
        "commitment",
        "preference",
        "deadline",
        "budget",
        "competitor",
        "decision_power",
        "buying_signal",
    }
)

# Allowed `relationship_type` values.
RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "reports_to",
        "manages",
        "works_with",
        "champions",
        "blocks",
        "decides",
        "influences",
        "introduced",
        "replaced",
    }
)

# Allowed `sentiment` values (NULL is also valid and handled separately).
SENTIMENTS: frozenset[str] = frozenset({"positive", "negative", "neutral"})

# The entry point speaks of a 'crm_update' source, but the database column (and
# the priority ladder below) names it 'crm_record'. Normalise on the way in so
# the stored value satisfies the source_type CHECK constraint.
_SOURCE_TYPE_ALIASES: dict[str, str] = {"crm_update": "crm_record"}

# Conflict resolution priority: when two facts about the same entity/fact_type
# disagree across different sources, the higher number wins. 'inferred' is the
# floor for low-confidence model guesses that did not come from a real channel.
_SOURCE_PRIORITY: dict[str, int] = {
    "crm_record": 5,
    "email": 4,
    "meeting": 3,
    "note": 2,
    "inferred": 1,
}

# Title fragments that, on their own, justify auto-promoting a shadow entity to
# a real CRM contact (matched case-insensitively as substrings). These denote
# seniority or buying authority — the people a rep cannot afford to leave
# untracked. See ``shadow.check_and_auto_promote``.
PROMOTION_TITLE_KEYWORDS: tuple[str, ...] = (
    "director",
    "vp",
    "vice president",
    "c-level",
    "ceo",
    "cto",
    "cfo",
    "coo",
    "cio",
    "cmo",
    "head of",
    "president",
    "decision maker",
    "owner",
    "founder",
    "partner",
)


def normalize_source_type(source_type: str) -> str:
    """Map an entry-point source label onto its stored column value."""
    return _SOURCE_TYPE_ALIASES.get(source_type, source_type)


def source_priority(source_type: str) -> int:
    """Return the conflict-resolution priority of a source (higher wins)."""
    return _SOURCE_PRIORITY.get(normalize_source_type(source_type), 0)
