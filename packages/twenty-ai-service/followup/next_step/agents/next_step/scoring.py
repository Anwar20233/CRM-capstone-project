"""Internal scoring and ranking for the Next Step Intelligence Agent.

All scoring logic is private to this module. Callers receive only the
sorted, capped list of RecommendedAction — no weights, boost formulas,
or intermediate scores are ever exposed externally.
"""

from __future__ import annotations

from followup.next_step.agents.next_step.schemas import RecommendedAction
from followup.next_step.context.schemas import DealContext

_MIN_PRIORITY = 1
_MAX_PRIORITY = 5
_MAX_RECOMMENDATIONS = 5
_ENGAGEMENT_GAP_THRESHOLD_DAYS = 7

_ENGAGEMENT_KEYWORDS = ("re-engage", "follow up", "follow-up", "no activity", "cold", "stalled", "days since")
_OVERDUE_KEYWORDS = ("overdue", "reschedule", "stalled task", "missed deadline")
_BANT_KEYWORDS = ("budget", "authority", "decision maker", "economic buyer", "timeline", "close date", "bant")


def _text_blob(action: RecommendedAction) -> str:
    return " ".join([action.title, action.description, action.reasoning] + action.evidence).lower()


def _matches_current_stage(action: RecommendedAction, context: DealContext) -> bool:
    return context.opportunity.stage.lower() in _text_blob(action)


def _addresses_engagement_gap(action: RecommendedAction, context: DealContext) -> bool:
    if context.engagement.days_since_last_activity < _ENGAGEMENT_GAP_THRESHOLD_DAYS:
        return False
    return any(kw in _text_blob(action) for kw in _ENGAGEMENT_KEYWORDS)


def _addresses_overdue_task(action: RecommendedAction, context: DealContext) -> bool:
    if not any(t.is_overdue for t in context.tasks):
        return False
    return any(kw in _text_blob(action) for kw in _OVERDUE_KEYWORDS)


def _addresses_bant_gap(action: RecommendedAction, context: DealContext) -> bool:
    return any(kw in _text_blob(action) for kw in _BANT_KEYWORDS)


def _boosted_priority(action: RecommendedAction, context: DealContext) -> int:
    boosts = sum([
        _matches_current_stage(action, context),
        _addresses_engagement_gap(action, context),
        _addresses_overdue_task(action, context),
        _addresses_bant_gap(action, context),
    ])
    return max(_MIN_PRIORITY, action.priority - boosts)


def score_recommendations(
    actions: list[RecommendedAction],
    context: DealContext,
) -> list[RecommendedAction]:
    """Rank and cap recommendations using internal scoring logic.

    Returns at most 5 actions sorted by ascending priority (1 = most urgent).
    Scoring details are intentionally not exposed to callers.
    """
    boosted = [
        action.model_copy(update={"priority": _boosted_priority(action, context)})
        for action in actions
    ]
    boosted.sort(key=lambda a: a.priority)
    return boosted[:_MAX_RECOMMENDATIONS]
