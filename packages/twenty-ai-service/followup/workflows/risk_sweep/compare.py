from datetime import datetime, timezone

from followup.agents.risk.rules import risk_level_for_score
from followup.agents.risk.schemas import (
    RiskFactor,
    RiskLevel,
    RiskScoreComparison,
    RiskScoreSnapshot,
)
from followup.store.risk_snapshot_store import RiskSnapshotStore

LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}
DELTA_RE_ENGAGEMENT_THRESHOLD = 10


def _level_crossed_up(
    previous_level: RiskLevel | None,
    new_level: RiskLevel,
) -> bool:
    if previous_level is None:
        return new_level in {"medium", "high"}
    return LEVEL_RANK[new_level] > LEVEL_RANK[previous_level]


def _level_crossed_down(
    previous_level: RiskLevel | None,
    new_level: RiskLevel,
) -> bool:
    if previous_level is None:
        return False
    return LEVEL_RANK[new_level] < LEVEL_RANK[previous_level]


def _threshold_crossed(
    previous_score: int | None,
    new_score: int,
    previous_level: RiskLevel | None,
    new_level: RiskLevel,
) -> bool:
    if previous_score is None:
        return new_score >= 40
    delta = new_score - previous_score
    if delta >= DELTA_RE_ENGAGEMENT_THRESHOLD:
        return True
    if delta <= -DELTA_RE_ENGAGEMENT_THRESHOLD:
        return False
    return _level_crossed_up(previous_level, new_level)


def build_score_comparison(
    previous_score: int | None,
    new_score: int,
    *,
    previous_level: RiskLevel | None = None,
    new_level: RiskLevel | None = None,
) -> RiskScoreComparison:
    current_level = new_level or risk_level_for_score(new_score)
    delta = new_score - previous_score if previous_score is not None else None
    crossed_threshold = _threshold_crossed(
        previous_score,
        new_score,
        previous_level,
        current_level,
    )
    significant_delta = (
        delta is not None and abs(delta) >= DELTA_RE_ENGAGEMENT_THRESHOLD
    )
    worsened = (
        previous_score is None and new_score >= 40
    ) or (
        delta is not None and delta > 0 and significant_delta
    ) or _level_crossed_up(previous_level, current_level)

    reason_parts: list[str] = []
    if previous_score is None:
        reason_parts.append("First risk snapshot recorded.")
    elif delta is not None:
        if delta > 0:
            reason_parts.append(f"Risk score increased by {delta}.")
        elif delta < 0:
            reason_parts.append(f"Risk score decreased by {abs(delta)}.")
        else:
            reason_parts.append("Risk score unchanged.")

    if _level_crossed_up(previous_level, current_level):
        reason_parts.append(f"Risk level crossed up to {current_level.upper()}.")
    if _level_crossed_down(previous_level, current_level):
        reason_parts.append(f"Risk level improved to {current_level.upper()}.")

    return RiskScoreComparison(
        previous_score=previous_score,
        current_score=new_score,
        delta=delta,
        previous_level=previous_level,
        current_level=current_level,
        crossed_threshold=crossed_threshold,
        significant_delta=significant_delta,
        should_trigger_reengagement=worsened and crossed_threshold,
        reason=" ".join(reason_parts).strip(),
    )


def needs_re_engagement_draft(snapshot: RiskScoreSnapshot) -> bool:
    if snapshot.delta is not None and snapshot.delta < 0:
        return False
    return snapshot.threshold_crossed or snapshot.level_crossed_up


async def compare_score_to_previous(
    opportunity_id: str,
    workspace_id: str,
    new_score: int,
    factors: list[RiskFactor],
    snapshot_store: RiskSnapshotStore,
    source: str = "event",
    *,
    now: datetime | None = None,
) -> RiskScoreSnapshot:
    previous = await snapshot_store.get_latest_snapshot(
        opportunity_id,
        workspace_id,
    )
    previous_score = previous.score if previous else None
    previous_level = previous.level if previous else None
    new_level = risk_level_for_score(new_score)
    comparison = build_score_comparison(
        previous_score,
        new_score,
        previous_level=previous_level,
        new_level=new_level,
    )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    snapshot = RiskScoreSnapshot(
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        score=new_score,
        level=new_level,
        previous_score=previous_score,
        delta=comparison.delta,
        level_crossed_up=_level_crossed_up(previous_level, new_level),
        threshold_crossed=comparison.should_trigger_reengagement,
        source=source if source in {"event", "daily_sweep"} else "event",  # type: ignore[arg-type]
        factors=factors,
        computed_at=current_time,
    )
    return await snapshot_store.save_snapshot(snapshot)
