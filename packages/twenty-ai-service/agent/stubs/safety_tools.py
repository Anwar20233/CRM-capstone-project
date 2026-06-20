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

import calendar
import re
from datetime import date, datetime, timedelta, timezone as _tz
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
    # Linking a note/task to a person/company/opportunity is a low-risk join
    # write — must NOT hit the tier-3 confirmation gate. Unknown actions default
    # to tier 3, which was wrongly prompting the user to approve every link.
    "create_note_target": {
        "tier": 1,
        "required_fields": ["noteId"],
        "escalated": False,
    },
    "create_task_target": {
        "tier": 1,
        "required_fields": ["taskId"],
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

_MONTH_INDEX: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

# Matches "7 July", "July 7", "7th July", "July 7th", "7th of July".
_BARE_DATE_RE = re.compile(
    r"^(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)"
    r"|([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?)$"
)

_WEEKDAY_INDEX: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_WORD_TO_INT: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _next_weekday(from_date: date, target: int) -> date:
    days_ahead = target - from_date.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return from_date + timedelta(days=days_ahead)


def _parse_n(token: str) -> int | None:
    try:
        return int(token)
    except ValueError:
        return _WORD_TO_INT.get(token.lower())


def _last_day_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _add_months(d: date, n: int) -> date:
    month = d.month + n
    year = d.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)

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
    if proposed_date is None or proposed_date >= date.today():
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


async def _resolve_date(text: str, timezone: str = "Asia/Riyadh") -> dict:  # noqa: ARG001
    """Resolve a relative date expression to an absolute ISO date.

    Deterministic arithmetic anchored to today's actual date. Handles the common
    relative phrases sales reps write: weekday names, week/month offsets, and
    boundary shorthands. Returns an error for unrecognised phrases.

    *timezone* is accepted for interface parity; the implementation is calendar-
    arithmetic only and treats dates as wall-clock days (no TZ shift needed).
    """
    today = date.today()
    normalised = text.strip().lower()
    resolved: date | None = None

    if normalised == "today":
        resolved = today
    elif normalised == "tomorrow":
        resolved = today + timedelta(days=1)
    elif normalised == "yesterday":
        resolved = today - timedelta(days=1)
    elif normalised in ("end of month", "end of the month"):
        resolved = _last_day_of_month(today)
    elif normalised in ("end of week", "end of the week"):
        # Friday is the conventional sales end-of-week.
        resolved = _next_weekday(today, _WEEKDAY_INDEX["friday"])
    elif normalised == "next week":
        resolved = _next_weekday(today, _WEEKDAY_INDEX["monday"])
    elif normalised == "next month":
        resolved = _add_months(date(today.year, today.month, 1), 1)
    elif normalised.startswith("next "):
        day_name = normalised[5:]
        if day_name in _WEEKDAY_INDEX:
            resolved = _next_weekday(today, _WEEKDAY_INDEX[day_name])
    elif normalised.startswith("in "):
        parts = normalised[3:].split()
        # "in N days/weeks/months" or "in N business/working days"
        if len(parts) == 2:
            n = _parse_n(parts[0])
            unit = parts[1].rstrip("s")
            if n is not None:
                if unit == "day":
                    resolved = today + timedelta(days=n)
                elif unit == "week":
                    resolved = today + timedelta(weeks=n)
                elif unit == "month":
                    resolved = _add_months(today, n)
        elif len(parts) == 3 and parts[1] in ("business", "working"):
            n = _parse_n(parts[0])
            unit = parts[2].rstrip("s")
            if n is not None and unit == "day":
                d, count = today, 0
                while count < n:
                    d += timedelta(days=1)
                    if d.weekday() < 5:
                        count += 1
                resolved = d
    else:
        # Bare month/day without a year: "7 July", "July 7", "7th of July", etc.
        # Resolve to this year; if that date is already past, use next year.
        m = _BARE_DATE_RE.match(normalised)
        if m:
            day_str, month_str = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            month_num = _MONTH_INDEX.get((month_str or "").lower())
            try:
                day_num = int(day_str or 0)
            except ValueError:
                day_num = 0
            if month_num and 1 <= day_num <= 31:
                try:
                    candidate = date(today.year, month_num, day_num)
                    resolved = candidate if candidate >= today else date(today.year + 1, month_num, day_num)
                except ValueError:
                    pass  # invalid day for that month → leave resolved as None

    if resolved is None:
        return {
            "ok": False,
            "error": {
                "code": "UNKNOWN_DATE_PHRASE",
                "message": f"Cannot resolve '{text}' to an absolute date",
            },
        }

    iso = datetime(resolved.year, resolved.month, resolved.day, tzinfo=_tz.utc).isoformat()
    return {
        "ok": True,
        "data": {
            "iso": iso,
            "resolved": str(resolved),
            "original_phrase": text,
            "resolution_anchor": str(today),
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
