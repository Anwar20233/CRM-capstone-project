"""Stubbed safety + utility functions for the writer.

These mirror the ``Safety`` tools in the CRM Agent — Tool Registry:

``_lookup_action_tier`` is a **raw async function** called directly by the
``WritePolicy`` middleware — it is NOT exposed as a LangChain tool; the LLM
never sees it.  It looks up an action's base tier from the static catalog and
applies escalation when an entity was resolved without rep confirmation.

``_check_conflicts`` is a **rule-based, deterministic** guardrail (no LLM) that
compares proposed writes against current CRM state.  It is meant to run before
Tier 2+ writes.  The raw conflict rules live here; *when* to call it is the
orchestrator's responsibility (not built yet).

``resolve_date`` is the one stub exposed to the LLM (via ``build_utility_tools``);
it is a data-transformation helper the writer uses for natural-language dates.

All use the same ``{ ok, data }`` / ``{ ok, error }`` envelope as the bridge so
swapping in real implementations later is a drop-in replacement.

Out of scope here (owned by the orchestrator, not built yet): duplicate
detection, ``old_value`` capture, and session memory.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langchain_core.tools import StructuredTool


# Module-level flag making it obvious which tools are placeholders.
STUB = True


# Action → tier map built from the "CRM Agent — Tool Registry".
# Only Write tools are listed here; Read tools never pass through WritePolicy.
#
# Tiering logic (inherited from the original map and extended consistently):
#   Tier 1 — low-risk single-record create/update of contacts, tasks, notes.
#   Tier 2 — medium-risk create/update of opportunities & companies, and
#            relationship links (show diff).
#   Tier 3 — high-risk: deletes, terminal/irreversible stage moves, and bulk
#            operations (large blast radius) → draft + confirmation token.

_ACTION_TIER_MAP: dict[str, dict[str, Any]] = {
    # ── Tier 1 — low-risk, execute immediately. ─────────────────────────

    "create_note": {
        "tier": 1,
        "required_fields": ["content","entityType","entityId"],
        "escalated": False,
    },
    "create_task": {
        "tier": 1,
        "required_fields": ["title"],
        "escalated": False,
    },

    # ── Tier 2 — medium risk, show diff. ────────────────────────────────
    "create_person": {
        "tier": 1,
        "required_fields": ["firstName", "lastName","companyId"],
        "escalated": False,
    },
    "update_person": {
        "tier": 2,
        "required_fields": ["id"],
        "escalated": False,
    },
    "create_opportunity": {
        "tier": 2,
        "required_fields": ["name","stage"],
        "escalated": False,
    },
    "update_opportunity": {
        "tier": 2,
        "required_fields": ["id"],
        "escalated": False,
    },
    "create_company": {
        "tier": 2,
        "required_fields": ["name"],
        "escalated": False,
    },
    "update_company": {
        "tier": 2,
        "required_fields": ["id"],
        "escalated": False,
    },
    "create_many_people": {
        "tier": 2,
        "required_fields": ["records"],
        "escalated": False,
    },
    # "link_opportunity_to_company": { #not found
    #     "tier": 2,
    #     "required_fields": [],
    #     "escalated": False,
    # },
    # "transfer_contact_to_company": { #not found
    #     "tier": 2,
    #     "required_fields": [],
    #     "escalated": False,
    # },
    # ── Tier 3 — high risk, draft + confirmation token required. ────────
    "delete_person": {
        "tier": 3,
        "required_fields": ["id"],
        "escalated": False,
    },
    "delete_company": {
        "tier": 3,
        "required_fields": ["id"],
        "escalated": False,
    },
    "delete_opportunity": {
        "tier": 3,
        "required_fields": ["id"],
        "escalated": False,
    },
    "delete_task": {
        "tier": 3,
        "required_fields": ["id"],
        "escalated": False,
    },
    "delete_note": {
        "tier": 3,
        "required_fields": ["id"],
        "escalated": False,
    },
    "advance_deal_stage": {
        "tier": 3,
        "required_fields": ["dealId","targetStage","confirmationToken"],
        "escalated": False,
    },
    # Bulk writes touch many records at once → highest friction.

    "update_many_people": {
        "tier": 3,
        "required_fields": ["filter","data"],
        "escalated": False,
    },
    "update_many_opportunities": {
        "tier": 3,
        "required_fields": ["filter","data"],
        "escalated": False,
    },
}

# Hardcoded date resolutions for common relative phrases found in cases_data.json.
_DATE_MAP: dict[str, str] = {
    "next friday": "2026-06-12T00:00:00Z",
    "next monday": "2026-06-15T00:00:00Z",
    "next tuesday": "2026-06-16T00:00:00Z",
    "next week": "2026-06-15T00:00:00Z",
    "in 2 weeks": "2026-06-22T00:00:00Z",
    "in two weeks": "2026-06-22T00:00:00Z",
    "tomorrow": "2026-06-09T00:00:00Z",
    "end of month": "2026-06-30T00:00:00Z",
    "end of week": "2026-06-12T00:00:00Z",
    "today": "2026-06-08T00:00:00Z",
}

# Stub "now" anchor — relative phrases resolve against this date.
_RESOLUTION_ANCHOR = "2026-06-08"
_ANCHOR_DATE = date(2026, 6, 8)

# Field-name fragments that mark a date field as future-oriented (a past date
# on one of these is suspicious — e.g. a close date in the past).
_FUTURE_DATE_FIELD_HINTS = (
    "due",
    "close",
    "expected",
    "deadline",
    "renewal",
    "start",
    "next",
    "date",
)

# Field names that hold a pipeline stage (used for stage-regression checks).
_STAGE_FIELDS = ("stage", "deal_stage", "pipeline_stage", "pipelineStage")


# ---------------------------------------------------------------------------
# Raw functions — called directly by WritePolicy, NOT by the LLM
# ---------------------------------------------------------------------------

async def _lookup_action_tier(
    action: str,
    entity_was_ambiguous: bool = False,
) -> dict:
    """Return the tier for *action*, applying escalation when entities were ambiguous.

    Logic (from the registry's ``lookup_action_tier`` spec):
      1. Look up the action in the static catalog → base tier.
      2. If ``entity_was_ambiguous`` → ``tier = min(base_tier + 1, 3)``.
      3. Unknown action → return an error (fail safe; never default to Tier 1).

    The LLM does not decide tiers — this function does.  ``WritePolicy`` only
    reads ``data.tier``; unknown actions yield an error envelope, which the
    policy treats as tier 3 (highest friction).
    """
    entry = _ACTION_TIER_MAP.get(action)
    if entry is None:
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_ACTION",
                "message": (
                    f"Action '{action}' is not in the tier catalog; "
                    "refusing to default to a low tier."
                ),
            },
        }

    base_tier = entry["tier"]
    escalated = bool(entity_was_ambiguous)
    tier = min(base_tier + 1, 3) if escalated else base_tier
    reason = (
        f"Tier {base_tier} action escalated because entity was ambiguous"
        if escalated
        else f"Tier {base_tier} action; no escalation"
    )

    return {
        "ok": True,
        "data": {
            "tier": tier,
            "base_tier": base_tier,
            "escalated": escalated,
            "reason": reason,
            "required_fields": entry.get("required_fields", []),
            "mcp_tool": entry.get("mcp_tool"),
        },
    }


async def _check_conflicts(
    proposed_writes: list[dict[str, Any]],
    pipeline_stages: list[str] | None = None,
) -> dict:
    """Flag proposed writes that conflict with current CRM state.

    Rule-based and deterministic (no LLM).  Each entry in *proposed_writes* is
    ``{ "field", "current_value", "proposed_value" }``.  *pipeline_stages* is the
    ordered stage list (from ``get_pipeline_stages``) needed to detect
    regressions.

    Conflict rules (each flagged write short-circuits to one conflict):
      * order-of-magnitude value change (10x+ difference),
      * stage regression (proposed stage earlier than current),
      * past date on a future-oriented field.
    Routine changes (value increases, stage advances) pass silently.
    """
    conflicts: list[dict[str, Any]] = []

    for write in proposed_writes:
        field = write.get("field", "")
        current_value = write.get("current_value")
        proposed_value = write.get("proposed_value")

        conflict = (
            _magnitude_conflict(field, current_value, proposed_value)
            or _stage_regression(field, current_value, proposed_value, pipeline_stages)
            or _past_date_conflict(field, proposed_value)
        )
        if conflict is not None:
            conflicts.append(conflict)

    return {"ok": True, "data": {"conflicts": conflicts}}


def _as_number(value: Any) -> float | None:
    """Coerce a numeric value (int/float or numeric string) to float, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _magnitude_conflict(
    field: str,
    current_value: Any,
    proposed_value: Any,
) -> dict[str, Any] | None:
    current_number = _as_number(current_value)
    proposed_number = _as_number(proposed_value)
    if current_number is None or proposed_number is None:
        return None
    if current_number == 0 or proposed_number == 0:
        return None

    ratio = max(current_number, proposed_number) / min(
        abs(current_number), abs(proposed_number)
    )
    if ratio < 10:
        return None

    direction = "decrease" if proposed_number < current_number else "increase"
    factor = int(ratio) if ratio.is_integer() else round(ratio, 1)
    return {
        "field": field,
        "type": "magnitude_change",
        "detail": f"{factor}x {direction}: {current_value} → {proposed_value}",
    }


def _stage_regression(
    field: str,
    current_value: Any,
    proposed_value: Any,
    pipeline_stages: list[str] | None,
) -> dict[str, Any] | None:
    if not pipeline_stages or field not in _STAGE_FIELDS:
        return None
    if current_value not in pipeline_stages or proposed_value not in pipeline_stages:
        return None
    if pipeline_stages.index(proposed_value) < pipeline_stages.index(current_value):
        return {
            "field": field,
            "type": "stage_regression",
            "detail": f"stage moved backward: {current_value} → {proposed_value}",
        }
    return None


def _past_date_conflict(field: str, proposed_value: Any) -> dict[str, Any] | None:
    if not _is_future_oriented_field(field):
        return None
    proposed_date = _parse_iso_date(proposed_value)
    if proposed_date is None or proposed_date >= _ANCHOR_DATE:
        return None
    return {
        "field": field,
        "type": "past_date",
        "detail": f"past date on future-oriented field: {proposed_value}",
    }


def _is_future_oriented_field(field: str) -> bool:
    field_lower = field.lower()
    return any(hint in field_lower for hint in _FUTURE_DATE_FIELD_HINTS)


def _parse_iso_date(value: Any) -> date | None:
    """Parse the date portion (YYYY-MM-DD) of an ISO string, else None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


async def _resolve_date(text: str, timezone: str = "Asia/Riyadh") -> dict:
    """Resolve a relative date expression to an absolute ISO date.

    Pure deterministic logic (no LLM); uses a hardcoded map and returns an error
    for unrecognised phrases.  Always returns the original phrase and the
    resolution anchor so a rep can easily correct a wrong guess.

    *timezone* (default ``Asia/Riyadh``) is accepted for parity with the real
    resolver; the stub map is timezone-agnostic.
    """
    normalised = text.strip().lower()
    result = _DATE_MAP.get(normalised)

    if result is not None:
        return {
            "ok": True,
            "data": {
                "iso": result,
                "resolved": result[:10],
                "original_phrase": text,
                "resolution_anchor": _RESOLUTION_ANCHOR,
            },
        }

    return {
        "ok": False,
        "error": {
            "code": "UNKNOWN_DATE_PHRASE",
            "message": f"Cannot resolve '{text}' to an absolute date",
        },
    }


# ---------------------------------------------------------------------------
# Utility tools — the only stub tools exposed to the LLM
# ---------------------------------------------------------------------------

def build_utility_tools() -> list[StructuredTool]:
    """Return utility tools the LLM can call directly.

    Currently only ``resolve_date`` — a data-transformation helper, not a
    safety gate.
    """
    return [
        StructuredTool.from_function(
            coroutine=_resolve_date,
            name="resolve_date",
            description=(
                "Resolve a relative date expression (e.g. 'next Friday', 'in 2 weeks', "
                "'end of month') to an absolute ISO date. Optionally pass the rep's "
                "timezone (default Asia/Riyadh)."
            ),
        ),
    ]
