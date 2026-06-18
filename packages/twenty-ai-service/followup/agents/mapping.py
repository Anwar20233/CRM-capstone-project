"""Anti-corruption mappers between the orchestrator's profile context and the
two specialist subagents' own context shapes.

The orchestrator owns one ground-truth context (``followup.profile.schemas
.DealContext``). The next-step and drafting agents were built independently and
each carries its own Pydantic ``DealContext``. Rather than rewrite either side,
these mappers translate the orchestrator's in-memory context (NO database reads)
into each agent's input shape. All translation lives here, in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from followup.emailer.context.schemas import (
    CompanyContext,
    ContactContext,
    DealContext as DraftingDealContext,
    NoteSummary,
    OpportunityContext,
)
from followup.next_step.context.schemas import (
    CompanySnapshot,
    ContactSnapshot,
    DealContext as NextStepDealContext,
    EngagementMetrics,
    FactCategory,
    OpportunitySnapshot,
    ProfileFact,
    TaskSnapshot,
    TimelineItem,
)
from followup.profile.schemas import DealContext as ProfileDealContext

# Window the next-step agent's engagement metrics are computed over.
_ENGAGEMENT_WINDOW_DAYS = 14


def _parse_dt(value: Any) -> datetime | None:
    """Best-effort ISO-8601 → aware datetime (the orchestrator stores ISO strings)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _activity_text(activity: dict[str, Any]) -> str:
    return str(activity.get("summary") or activity.get("content") or "").strip()


# ===========================================================================
# Engagement metrics — derived from the profile's recent_activities
# ===========================================================================


def _engagement_from_activities(
    activities: list[dict[str, Any]], *, now: datetime
) -> EngagementMetrics:
    """Derive the next-step agent's engagement signals from activity timestamps.

    The orchestrator's profile context does not carry pre-computed engagement
    metrics, so we approximate them from the recent-activity timeline. Best
    effort: a quiet deal (no dated activity) reads as a large gap.
    """
    dates = sorted(
        (dt for dt in (_parse_dt(a.get("date")) for a in activities) if dt),
        reverse=True,
    )
    if not dates:
        # No activity on record — report recency as unknown (None), never a
        # fabricated sentinel. Counts are genuinely zero; has_future_meeting too.
        return EngagementMetrics(
            days_since_last_activity=None,
            activity_count_14d=0,
            activity_count_prior_14d=0,
            has_future_meeting=False,
        )

    days_since_last = max((now - dates[0]).days, 0)
    recent = sum(1 for dt in dates if (now - dt).days <= _ENGAGEMENT_WINDOW_DAYS)
    prior = sum(
        1
        for dt in dates
        if _ENGAGEMENT_WINDOW_DAYS < (now - dt).days <= 2 * _ENGAGEMENT_WINDOW_DAYS
    )
    has_future_meeting = any(dt > now for dt in dates)
    return EngagementMetrics(
        days_since_last_activity=days_since_last,
        activity_count_14d=recent,
        activity_count_prior_14d=prior,
        has_future_meeting=has_future_meeting,
    )


# ===========================================================================
# profile.DealContext → next_step.context.DealContext
# ===========================================================================


def _next_step_facts(deal: ProfileDealContext) -> list[ProfileFact]:
    """Open concerns + contact facts → the next-step agent's active_facts.

    Concerns map to the RISK category; everything else stays generic
    (DERIVED_INSIGHTS) since the orchestrator's facts are not pre-categorized.
    """
    facts: list[ProfileFact] = []
    for index, concern in enumerate(deal.open_concerns or []):
        value = str(concern.get("content") or concern.get("fact_value") or "").strip()
        if not value:
            continue
        facts.append(
            ProfileFact(
                fact_id=str(concern.get("id") or f"concern-{index}"),
                category=FactCategory.RISK,
                fact_key=str(concern.get("fact_type") or "open_concern"),
                value=value,
                confidence=float(concern.get("confidence") or 1.0),
            )
        )
    return facts


def to_next_step_context(
    deal: ProfileDealContext, *, inbound_signal: dict[str, Any] | None = None
) -> NextStepDealContext:
    """Translate the orchestrator's in-memory deal picture for the next-step agent.

    ``inbound_signal`` is the activity that triggered this run (the inbound
    email). Folding it into the timeline keeps engagement honest: a deal whose
    buyer just emailed is actively engaged, not cold. Shape: ``{type, date,
    summary}`` — the same as a ``recent_activities`` item.
    """
    now = datetime.now(timezone.utc)
    activities = list(deal.recent_activities or [])
    if inbound_signal:
        activities = [inbound_signal, *activities]

    opportunity = OpportunitySnapshot(
        id=str(deal.opportunity_id),
        name=deal.opportunity_name,
        stage=deal.deal_stage,
        amount=deal.deal_value,
        close_date=_parse_dt(deal.close_date),
    )
    # The next-step agent skips deals with no linked company, so always supply
    # one from the name the orchestrator resolved (id is informational here).
    company = CompanySnapshot(id=str(deal.opportunity_id), name=deal.company_name or "Unknown")

    contacts = [
        ContactSnapshot(
            id=str(contact.crm_id),
            name=contact.name or "Unknown",
            role=contact.role,
            is_decision_maker=contact.is_decision_maker,
        )
        for contact in deal.contacts
    ]

    timeline = [
        TimelineItem(
            type=str(activity.get("type") or "activity"),
            title=_activity_text(activity)[:80] or "Activity",
            summary=_activity_text(activity) or "(no detail)",
            occurred_at=_parse_dt(activity.get("date")) or now,
        )
        for activity in activities
    ]

    # Already relevance-filtered upstream (ProfileService drops DONE/abandoned),
    # so every task here is a live signal the agent's engagement check reads.
    tasks = [
        TaskSnapshot(
            id=str(task.get("id") or f"task-{index}"),
            title=str(task.get("title") or "Task"),
            status=str(task.get("status") or "open"),
            due_at=_parse_dt(task.get("due_at")),
            is_overdue=bool(task.get("is_overdue")),
        )
        for index, task in enumerate(deal.tasks or [])
    ]

    return NextStepDealContext(
        opportunity=opportunity,
        company=company,
        contacts=contacts,
        timeline=timeline,
        tasks=tasks,
        engagement=_engagement_from_activities(activities, now=now),
        active_facts=_next_step_facts(deal),
        loaded_at=now,
    )


# ===========================================================================
# profile.DealContext → emailer.context.DealContext
# ===========================================================================


def _primary_contact(deal: ProfileDealContext, recipient_email: str | None) -> ContactContext:
    """The drafter addresses one contact: the requested recipient, else the first
    contact with an email, else the first contact."""
    if recipient_email:
        for contact in deal.contacts:
            if contact.email == recipient_email:
                return ContactContext(
                    id=str(contact.crm_id),
                    name=contact.name or "there",
                    email=contact.email,
                    title=contact.role,
                )
    for contact in deal.contacts:
        if contact.email:
            return ContactContext(
                id=str(contact.crm_id),
                name=contact.name or "there",
                email=contact.email,
                title=contact.role,
            )
    if deal.contacts:
        first = deal.contacts[0]
        return ContactContext(
            id=str(first.crm_id), name=first.name or "there", email=recipient_email, title=first.role
        )
    return ContactContext(id=None, name="there", email=recipient_email, title=None)


def to_drafting_context(
    deal: ProfileDealContext, *, recipient_email: str | None = None
) -> DraftingDealContext:
    """Translate the orchestrator's in-memory deal picture for the drafting agent."""
    opportunity = OpportunityContext(
        id=str(deal.opportunity_id),
        stage=deal.deal_stage,
        amount=deal.deal_value,
    )
    company = CompanyContext(name=deal.company_name or "Unknown")
    recent_notes = [
        NoteSummary(
            id=str(activity.get("id") or f"note-{index}"),
            title=_activity_text(activity)[:80] or None,
            body=_activity_text(activity) or None,
            created_at=_parse_dt(activity.get("date")),
        )
        for index, activity in enumerate(deal.recent_activities or [])
        if str(activity.get("type") or "").lower() in ("", "note", "activity", "email")
    ]
    return DraftingDealContext(
        opportunity=opportunity,
        company=company,
        contact=_primary_contact(deal, recipient_email),
        recent_notes=recent_notes,
    )


__all__ = ["to_next_step_context", "to_drafting_context"]
