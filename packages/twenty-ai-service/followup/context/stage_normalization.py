from __future__ import annotations

from typing import Any

CANONICAL_CLOSED_STAGES = frozenset({"CLOSED_WON", "CLOSED_LOST"})

FALLBACK_PIPELINE_STAGES = [
    "NEW",
    "SCREENING",
    "MEETING",
    "PROPOSAL",
    "CUSTOMER",
    "CLOSED_WON",
    "CLOSED_LOST",
]

DEFAULT_STAGE_SLA_DAYS: dict[str, int] = {
    "NEW": 7,
    "SCREENING": 10,
    "MEETING": 10,
    "PROPOSAL": 14,
    "CUSTOMER": 365,
    "CLOSED_WON": 365,
    "CLOSED_LOST": 365,
    "DISCOVERY": 14,
    "NEGOTIATION": 21,
    "UNKNOWN": 14,
}

_STAGE_ALIASES: dict[str, str] = {
    "new": "NEW",
    "screening": "SCREENING",
    "meeting": "MEETING",
    "proposal": "PROPOSAL",
    "customer": "CUSTOMER",
    "closed won": "CLOSED_WON",
    "closed-won": "CLOSED_WON",
    "closed_won": "CLOSED_WON",
    "closed lost": "CLOSED_LOST",
    "closed-lost": "CLOSED_LOST",
    "closed_lost": "CLOSED_LOST",
    "discovery": "DISCOVERY",
    "negotiation": "NEGOTIATION",
}


def _alias_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def normalize_stage(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, dict):
        nested = value.get("value") or value.get("label")
        return normalize_stage(nested)
    if not isinstance(value, str):
        return "UNKNOWN"

    stripped = value.strip()
    if not stripped:
        return "UNKNOWN"

    alias_key = _alias_key(stripped)
    if alias_key in _STAGE_ALIASES:
        return _STAGE_ALIASES[alias_key]

    compact = stripped.upper().replace("-", "_").replace(" ", "_")
    if compact in DEFAULT_STAGE_SLA_DAYS or compact in FALLBACK_PIPELINE_STAGES:
        return compact
    return compact or "UNKNOWN"


def is_closed_stage(stage: str) -> bool:
    return normalize_stage(stage) in CANONICAL_CLOSED_STAGES


def stage_index_in_pipeline(stage: str, pipeline_stages: list[str]) -> int:
    normalized = normalize_stage(stage)
    normalized_pipeline = [normalize_stage(pipeline_stage) for pipeline_stage in pipeline_stages]
    try:
        return normalized_pipeline.index(normalized)
    except ValueError:
        return -1


def stage_at_or_after(
    stage: str,
    reference_stage: str,
    pipeline_stages: list[str],
) -> bool:
    current_index = stage_index_in_pipeline(stage, pipeline_stages)
    reference_index = stage_index_in_pipeline(reference_stage, pipeline_stages)
    if current_index < 0:
        return False
    if reference_index < 0:
        return current_index > 0
    return current_index >= reference_index


def stage_after(
    stage: str,
    reference_stage: str,
    pipeline_stages: list[str],
) -> bool:
    current_index = stage_index_in_pipeline(stage, pipeline_stages)
    reference_index = stage_index_in_pipeline(reference_stage, pipeline_stages)
    if current_index < 0:
        return False
    if reference_index < 0:
        return current_index > 0
    return current_index > reference_index


def build_stage_sla_days(stages: list[str]) -> dict[str, int]:
    return {
        stage: DEFAULT_STAGE_SLA_DAYS.get(stage, 14)
        for stage in stages
    }
