"""Stubbed safety + utility functions for the writer.

``_lookup_action_tier`` is a **raw async function** called directly by the
``WritePolicy`` middleware — it is NOT exposed as a LangChain tool; the LLM
never sees it.

``resolve_date`` is the one stub exposed to the LLM (via ``build_utility_tools``);
it is a data-transformation helper the writer uses for natural-language dates.

Both use the same ``{ ok, data }`` / ``{ ok, error }`` envelope as the bridge so
swapping in real implementations later is a drop-in replacement.

Out of scope here (owned by the orchestrator, not built yet): duplicate
detection, data-conflict checks, ``old_value`` capture, and session memory.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool


# Module-level flag making it obvious which tools are placeholders.
STUB = True


# A deliberately small hardcoded action → tier map exercising tiers 1/2/3.
_ACTION_TIER_MAP: dict[str, dict[str, Any]] = {
    # Tier 1 — low-risk, execute immediately.
    "create_person": {
        "tier": 1,
        "required_fields": ["name.firstName", "name.lastName"],
        "escalated": False,
    },
    "create_note": {
        "tier": 1,
        "required_fields": ["title"],
        "escalated": False,
    },
    "create_task": {
        "tier": 1,
        "required_fields": ["title"],
        "escalated": False,
    },
    "update_person": {
        "tier": 1,
        "required_fields": [],
        "escalated": False,
    },
    # Tier 2 — medium risk, show diff.
    "create_opportunity": {
        "tier": 2,
        "required_fields": ["name"],
        "escalated": False,
    },
    "update_opportunity": {
        "tier": 2,
        "required_fields": [],
        "escalated": False,
    },
    "create_company": {
        "tier": 2,
        "required_fields": ["name"],
        "escalated": False,
    },
    "update_company": {
        "tier": 2,
        "required_fields": [],
        "escalated": False,
    },
    # Tier 3 — high risk, draft + confirmation token required.
    "delete_person": {
        "tier": 3,
        "required_fields": [],
        "escalated": False,
    },
    "delete_company": {
        "tier": 3,
        "required_fields": [],
        "escalated": False,
    },
    "delete_opportunity": {
        "tier": 3,
        "required_fields": [],
        "escalated": False,
    },
    "advance_deal_stage": {
        "tier": 3,
        "required_fields": ["id", "stage"],
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


# ---------------------------------------------------------------------------
# Raw functions — called directly by WritePolicy, NOT by the LLM
# ---------------------------------------------------------------------------

async def _lookup_action_tier(action: str) -> dict:
    """Return the tier (1/2/3), required fields, and escalation flag for *action*.

    Unknown actions fail safe to tier 3 (highest friction).
    """
    entry = _ACTION_TIER_MAP.get(action)
    if entry is not None:
        return {"ok": True, "data": entry}

    return {
        "ok": True,
        "data": {
            "tier": 3,
            "required_fields": [],
            "escalated": True,
        },
    }


async def _resolve_date(text: str, reference_date: str | None = None) -> dict:
    """Resolve a relative date phrase to an absolute ISO-8601 timestamp.

    Uses a hardcoded map; returns an error for unrecognised phrases.
    """
    normalised = text.strip().lower()
    result = _DATE_MAP.get(normalised)

    if result is not None:
        return {"ok": True, "data": {"iso": result, "original": text}}

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
                "Resolve a relative date phrase (e.g. 'next Friday', 'in 2 weeks') "
                "to an absolute ISO-8601 timestamp."
            ),
        ),
    ]
