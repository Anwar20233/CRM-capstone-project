"""Grounding + execution helpers for opportunity field updates (stage, close date).

Why this module exists
----------------------
The next-step planner can recommend a CRM update — advance the deal's stage, push
the close date. Historically every such recommendation was handed to the writer
LLM as a free-text intent ("advance to Negotiation"), with two failure modes:

* the planner could name a stage that does NOT exist in the workspace, so the
  writer either picked the wrong stage or wrote nothing; and
* every opportunity edit collapsed into a *stage* edit, so a close-date push was
  sent to the writer as "set the stage to match 'push the close date'" — which it
  could not do, and the write silently did nothing.

This module fixes both at the source: it resolves a recommended change against the
REAL pipeline metadata into a concrete ``{field, value}`` the executor writes
*deterministically* (no LLM guessing), and when a change cannot be grounded it
returns an explicit ``valid=False`` + ``reason`` so the failure is surfaced, never
silent.

The same resolver runs twice: once at plan time (so the rep reviews a real,
validated change) and again as a safety net at accept time (so a stale or
hand-edited change can never write a value the pipeline does not have).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)

# The two plan step kinds that map to an opportunity field write. ``update_stage``
# is the historical kind (kept so old pending actions still execute); non-stage
# field updates use ``update_opportunity``.
OPP_UPDATE_KINDS: frozenset[str] = frozenset({"update_stage", "update_opportunity"})

# CRM opportunity fields this path may write, keyed by the canonical bridge field
# name. Deliberately small: stage + close date are the changes the planner makes.
# Anything else is rejected with a clear reason rather than guessed at — we never
# write a field we cannot ground.
ALLOWED_FIELDS: frozenset[str] = frozenset({"stage", "closeDate"})

# Accept a few aliases the LLM / legacy callers might emit for the field name.
_FIELD_ALIASES: dict[str, str] = {
    "stage": "stage",
    "deal_stage": "stage",
    "pipeline_stage": "stage",
    "closedate": "closeDate",
    "close_date": "closeDate",
    "close": "closeDate",
    "expected_close_date": "closeDate",
}

# Intent phrasing that means "move to the next stage in the pipeline" rather than
# naming a specific target stage.
_ADVANCE_HINTS = ("advance", "next stage", "move forward", "progress", "push forward")


def canonical_field(field: Optional[str]) -> Optional[str]:
    """Normalize an LLM/legacy field name to a canonical bridge field, or None."""
    if not field:
        return None
    return _FIELD_ALIASES.get(str(field).strip().lower())


def kind_for_field(field: Optional[str]) -> str:
    """The plan step kind for a given canonical field (stage keeps its own kind)."""
    return "update_stage" if canonical_field(field) == "stage" else "update_opportunity"


# ---------------------------------------------------------------------------
# Pipeline stage metadata
# ---------------------------------------------------------------------------


async def fetch_pipeline_stages() -> list[dict[str, Any]]:
    """Read the opportunity ``stage`` field options from Twenty's metadata API.

    Returns a list of ``{value, label, position}`` dicts, or ``[]`` when the
    metadata read fails. We deliberately do NOT fall back to a static stage list:
    a wrong list would let us write a stage the workspace does not have, which is
    exactly the bug this module exists to prevent. An empty list makes the caller
    mark the change invalid (with a reason) instead.
    """
    try:
        from agent.tool_scope import READER_SCOPE
        from agent.tools.composite_reads import _exec, _identity
        from bridge_client import forward

        ident = _identity(READER_SCOPE)
        obj_result = await forward(
            "execute",
            _exec("get_object_metadata", {"limit": 100}, ident),
        )
        if not obj_result.get("ok"):
            logger.warning("fetch_pipeline_stages: get_object_metadata failed: %s", obj_result.get("error"))
            return []

        objects = obj_result.get("data") or []
        opp_id = None
        for obj in objects:
            if obj.get("nameSingular") == "opportunity":
                opp_id = obj.get("id")
                break

        if not opp_id:
            logger.warning("fetch_pipeline_stages: opportunity object metadata not found")
            return []

        fields_result = await forward(
            "execute",
            _exec(
                "get_field_metadata",
                {"objectMetadataId": opp_id, "limit": 100},
                ident,
            ),
        )
        if not fields_result.get("ok"):
            logger.warning("fetch_pipeline_stages: get_field_metadata failed: %s", fields_result.get("error"))
            return []

        fields = fields_result.get("data") or []
        options = None
        for f in fields:
            if f.get("name") == "stage":
                options = f.get("options")
                break

        if not isinstance(options, list):
            logger.warning("fetch_pipeline_stages: stage field or its options list not found")
            return []

        return [o for o in options if isinstance(o, dict) and o.get("value")]
    except Exception as error:  # noqa: BLE001 — a read failure is non-fatal
        logger.warning("fetch_pipeline_stages: bridge read failed: %s", error)
        return []


def stage_values(stages: list[dict[str, Any]]) -> list[str]:
    """The bare stage values (e.g. ``["NEW", "SCREENING", ...]``)."""
    return [str(s["value"]) for s in stages if s.get("value")]


def format_stage_options(stages: list[dict[str, Any]]) -> str:
    """Human-readable ``label (VALUE)`` list for prompts and error reasons."""
    parts = []
    for s in sorted(stages, key=lambda s: s.get("position", 0)):
        value = s.get("value")
        label = s.get("label") or value
        parts.append(f"{label} ({value})" if label != value else str(value))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _squash(text: str) -> str:
    """Lowercase + strip non-alphanumerics, so 'Closed Won' == 'CLOSED_WON'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def normalize_stage(
    proposed: Optional[str],
    stages: list[dict[str, Any]],
    *,
    current_value: Optional[str] = None,
    intent: str = "",
) -> tuple[Optional[str], Optional[str]]:
    """Resolve a proposed stage to a real pipeline stage value.

    Tries, in order: exact value, exact label, squashed (punctuation-insensitive)
    value/label, then containment either way. If nothing matches but the intent
    asks to *advance*, returns the next stage by position after ``current_value``.
    Returns ``(value, None)`` on success or ``(None, reason)`` when it cannot be
    grounded — the reason names the valid options so the failure is actionable.
    """
    if not stages:
        return None, "could not read the pipeline's stage options"

    options = format_stage_options(stages)
    candidate = (proposed or "").strip()

    if candidate:
        squashed = _squash(candidate)
        # 1) exact value / label, case-insensitive.
        for stage in stages:
            value = str(stage["value"])
            label = str(stage.get("label") or value)
            if candidate.lower() in (value.lower(), label.lower()):
                return value, None
        # 2) punctuation-insensitive value / label.
        for stage in stages:
            value = str(stage["value"])
            label = str(stage.get("label") or value)
            if squashed and squashed in (_squash(value), _squash(label)):
                return value, None
        # 3) containment either direction (e.g. "negotiation" within a label).
        for stage in stages:
            value = str(stage["value"])
            label = str(stage.get("label") or value)
            haystacks = (_squash(value), _squash(label))
            if squashed and any(squashed in h or h in squashed for h in haystacks):
                return value, None

    # 4) "advance to the next stage" with no resolvable target.
    if _wants_advance(candidate, intent):
        nxt = _next_stage(current_value, stages)
        if nxt is not None:
            return nxt, None
        return None, f"the deal is already at the final stage ({current_value})"

    if candidate:
        return None, f"'{candidate}' is not a valid stage. Valid stages: {options}"
    return None, f"no target stage was specified. Valid stages: {options}"


def _wants_advance(candidate: str, intent: str) -> bool:
    text = f"{candidate} {intent}".lower()
    return any(hint in text for hint in _ADVANCE_HINTS)


def _next_stage(
    current_value: Optional[str], stages: list[dict[str, Any]]
) -> Optional[str]:
    """The stage immediately after ``current_value`` by position."""
    if not current_value:
        return None
    ordered = sorted(stages, key=lambda s: s.get("position", 0))
    for index, stage in enumerate(ordered):
        if _squash(str(stage["value"])) == _squash(current_value):
            if index + 1 < len(ordered):
                return str(ordered[index + 1]["value"])
            return None
    return None


def normalize_close_date(proposed: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse a proposed close date into an ISO ``YYYY-MM-DD`` string.

    Accepts explicit dates in common formats (ISO, ``MM/DD/YYYY``, ``July 1 2026``).
    Returns ``(iso_date, None)`` or ``(None, reason)`` — we do not guess relative
    phrases ("end of next month"); the planner is instructed to emit a real date.
    """
    candidate = (proposed or "").strip()
    if not candidate:
        return None, "no close date was specified"
    try:
        parsed = date_parser.parse(candidate, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None, f"'{candidate}' is not a recognizable date (use YYYY-MM-DD)"
    return parsed.date().isoformat(), None


# ---------------------------------------------------------------------------
# The resolver — used at plan time AND accept time
# ---------------------------------------------------------------------------


StagesProvider = Callable[[], Awaitable[list[dict[str, Any]]]]


async def resolve_change(
    *,
    field: Optional[str],
    value: Optional[str],
    intent: str = "",
    current_stage: Optional[str] = None,
    current_close_date: Optional[str] = None,
    stages_provider: Optional[StagesProvider] = None,
) -> dict[str, Any]:
    """Ground a recommended opportunity change into a concrete, writable change.

    Returns a canonical dict the executor and the UI both read::

        {field, value, valid, reason, display, current_value}

    ``valid=False`` always carries a human ``reason`` (never a silent no-op).
    ``display`` is a short human label for the workflow card.
    """
    provider = stages_provider or fetch_pipeline_stages
    # A missing field defaults to a stage change (the most common update); a field
    # that was given but is not recognized is rejected — never silently coerced.
    canonical = canonical_field(field) if field else "stage"
    if canonical is None:
        return _invalid(
            str(field),
            value,
            f"updating the '{field}' field is not supported (only stage and close date)",
        )

    if canonical == "stage":
        stages = await provider()
        resolved, reason = normalize_stage(
            value, stages, current_value=current_stage, intent=intent
        )
        if resolved is None:
            return _invalid("stage", value, reason, current_value=current_stage)
        label = _stage_label(resolved, stages)
        return {
            "field": "stage",
            "value": resolved,
            "valid": True,
            "reason": None,
            "display": f"Move stage to {label}",
            "current_value": current_stage,
        }

    # closeDate
    resolved, reason = normalize_close_date(value)
    if resolved is None:
        return _invalid("closeDate", value, reason, current_value=current_close_date)
    return {
        "field": "closeDate",
        "value": resolved,
        "valid": True,
        "reason": None,
        "display": f"Set close date to {resolved}",
        "current_value": current_close_date,
    }


def _invalid(
    field: str,
    value: Optional[str],
    reason: Optional[str],
    *,
    current_value: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "field": field,
        "value": value,
        "valid": False,
        "reason": reason or "the requested change could not be grounded",
        "display": f"Update {field} (needs review)",
        "current_value": current_value,
    }


def _stage_label(value: str, stages: list[dict[str, Any]]) -> str:
    for stage in stages:
        if str(stage.get("value")) == value:
            return str(stage.get("label") or value)
    return value


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def build_update_args(opportunity_id: str, change: dict[str, Any]) -> dict[str, Any]:
    """The ``update_opportunity`` bridge args for a grounded change."""
    return {"id": str(opportunity_id), change["field"]: change["value"]}


def proposed_from_step(step: dict[str, Any]) -> tuple[Optional[str], Optional[str], str]:
    """Extract ``(field, value, intent)`` a plan step proposed for an opp update.

    The structured change the planner emitted lives in ``metadata.change``; the
    free-text ``intent`` is kept as a fallback the resolver can read (e.g. to
    detect an "advance to the next stage" request with no explicit target).
    """
    meta = step.get("metadata") or {}
    change = meta.get("change") or {}
    return change.get("field"), change.get("value"), step.get("intent") or ""


__all__ = [
    "OPP_UPDATE_KINDS",
    "ALLOWED_FIELDS",
    "canonical_field",
    "kind_for_field",
    "fetch_pipeline_stages",
    "stage_values",
    "format_stage_options",
    "normalize_stage",
    "normalize_close_date",
    "resolve_change",
    "build_update_args",
    "proposed_from_step",
]
