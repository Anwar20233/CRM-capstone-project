"""Score batch e2e runs against per-scenario behavioral expectations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.followup_email_scenarios import ScenarioExpectations
    from scripts.followup_orchestrator_e2e import ScenarioRunResult

RiskBand = str  # "low" | "medium" | "high"

# RiskAssessment uses 0–100; tolerate legacy 0–1 values.
RISK_BAND_RANGES: dict[RiskBand, tuple[float, float]] = {
    "low": (0.0, 35.0),
    "medium": (35.0, 55.0),
    "high": (55.0, 100.0),
}

NEXT_STEP_FALLBACK_SOURCE = "next_step_fallback"
LLM_FAILURE_MARKER = "LLM call failed"


def plan_used_fallback(final_state: dict[str, Any]) -> bool:
    """True when the next-step agent failed and the orchestrator used a generic plan."""
    if final_state.get("plan_fallback") is True:
        return True
    plan = final_state.get("plan")
    if plan is None:
        return False
    metadata = getattr(plan, "metadata", None) or {}
    if metadata.get("source") == NEXT_STEP_FALLBACK_SOURCE:
        return True
    summary = getattr(plan, "summary", None) or ""
    return LLM_FAILURE_MARKER in summary


def normalize_risk_score(score: float | None) -> float | None:
    if score is None:
        return None
    if score <= 1.0:
        return score * 100.0
    return score


def risk_in_band(score: float | None, band: RiskBand) -> bool:
    normalized = normalize_risk_score(score)
    if normalized is None:
        return False
    low, high = RISK_BAND_RANGES[band]
    if band == "high":
        return normalized >= low
    if band == "low":
        return normalized < RISK_BAND_RANGES["medium"][0]
    return low <= normalized < high


@dataclass(frozen=True)
class BehavioralScore:
    scenario_name: str
    pipeline_match: bool
    plan_match: bool | None
    action_match: bool | None
    urgency_match: bool | None
    draft_match: bool | None
    calendar_match: bool | None
    risk_match: bool | None
    mismatches: tuple[str, ...]

    @property
    def behavioral_ok(self) -> bool:
        if not self.pipeline_match:
            return False
        for field_match in (
            self.plan_match,
            self.action_match,
            self.urgency_match,
            self.draft_match,
            self.calendar_match,
            self.risk_match,
        ):
            if field_match is False:
                return False
        return True


def score_behavior(
    expectations: ScenarioExpectations | None,
    result: ScenarioRunResult,
) -> BehavioralScore | None:
    if expectations is None:
        return None

    mismatches: list[str] = []
    pipeline_match = result.ok == expectations.expect_pipeline_ok
    if not pipeline_match:
        if result.plan_fallback and expectations.expect_pipeline_ok:
            mismatches.append(
                "pipeline: next-step used fallback plan (LLM call failed)"
            )
        elif result.status != "completed" or result.error:
            expected = "complete" if expectations.expect_pipeline_ok else "halt/fail"
            actual = "completed" if result.status == "completed" else "failed"
            mismatches.append(f"pipeline: expected {expected}, got {actual}")

    plan_match: bool | None = None
    if expectations.expect_pipeline_ok:
        plan_match = not result.plan_fallback
        if result.plan_fallback:
            mismatches.append("plan: expected real next-step plan, got fallback")

    action_match: bool | None = None
    if expectations.action_types:
        action_match = (result.action_type or "") in expectations.action_types
        if not action_match:
            mismatches.append(
                f"action: expected one of {sorted(expectations.action_types)}, "
                f"got {result.action_type!r}"
            )

    urgency_match: bool | None = None
    if expectations.urgency:
        urgency_match = (result.urgency or "") in expectations.urgency
        if not urgency_match:
            mismatches.append(
                f"urgency: expected one of {sorted(expectations.urgency)}, "
                f"got {result.urgency!r}"
            )

    draft_match: bool | None = None
    if expectations.expect_draft is not None:
        draft_match = result.has_draft == expectations.expect_draft
        if not draft_match:
            mismatches.append(
                f"draft: expected {expectations.expect_draft}, got {result.has_draft}"
            )

    calendar_match: bool | None = None
    if expectations.expect_calendar is not None:
        calendar_match = result.has_calendar == expectations.expect_calendar
        if not calendar_match:
            mismatches.append(
                f"calendar: expected {expectations.expect_calendar}, "
                f"got {result.has_calendar}"
            )

    risk_match: bool | None = None
    if expectations.risk_band:
        risk_match = risk_in_band(result.risk_score, expectations.risk_band)
        if not risk_match:
            normalized = normalize_risk_score(result.risk_score)
            mismatches.append(
                f"risk: expected band {expectations.risk_band!r}, "
                f"got {normalized!r}"
            )

    return BehavioralScore(
        scenario_name=result.name,
        pipeline_match=pipeline_match,
        plan_match=plan_match,
        action_match=action_match,
        urgency_match=urgency_match,
        draft_match=draft_match,
        calendar_match=calendar_match,
        risk_match=risk_match,
        mismatches=tuple(mismatches),
    )


def aggregate_behavioral_scores(
    scores: list[BehavioralScore],
) -> dict[str, tuple[int, int]]:
    """Return {metric: (passed, total)} for each checked dimension."""
    totals: dict[str, list[bool]] = {
        "behavioral": [],
        "pipeline": [],
        "plan": [],
        "action": [],
        "urgency": [],
        "draft": [],
        "calendar": [],
        "risk": [],
    }
    for score in scores:
        totals["behavioral"].append(score.behavioral_ok)
        totals["pipeline"].append(score.pipeline_match)
        if score.plan_match is not None:
            totals["plan"].append(score.plan_match)
        if score.action_match is not None:
            totals["action"].append(score.action_match)
        if score.urgency_match is not None:
            totals["urgency"].append(score.urgency_match)
        if score.draft_match is not None:
            totals["draft"].append(score.draft_match)
        if score.calendar_match is not None:
            totals["calendar"].append(score.calendar_match)
        if score.risk_match is not None:
            totals["risk"].append(score.risk_match)

    return {
        key: (sum(1 for value in values if value), len(values))
        for key, values in totals.items()
        if values
    }
