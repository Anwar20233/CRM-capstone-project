from followup.emailer.agents.drafting.schemas import DraftType
from followup.emailer.agents.risk.schemas import RiskScore
from followup.emailer.context.schemas import DealContext
from followup.emailer.events.schemas import (
    FollowUpEvent,
    OpportunityStageChangedPayload,
)

_PROPOSAL_STAGES = frozenset(
    {"proposal", "negotiation", "contract sent", "closed won"}
)

_PRIORITY_ORDER: list[DraftType] = [
    DraftType.MEETING_RECAP_EMAIL,
    DraftType.RE_ENGAGEMENT_EMAIL,
    DraftType.FOLLOW_UP_EMAIL,
    DraftType.PROPOSAL_DELIVERY_EMAIL,
    DraftType.REMINDER_EMAIL,
    DraftType.PRODUCT_PROPOSAL,
    DraftType.SERVICE_PROPOSAL,
    DraftType.INDUSTRY_PROPOSAL,
]

_MAX_TYPES_PER_RUN = 2


def _is_proposal_stage(stage: str) -> bool:
    return stage.strip().lower() in _PROPOSAL_STAGES


def _resolve_proposal_type(context: DealContext) -> DraftType:
    company_type = (context.company.company_type or "").strip().lower()
    if company_type == "service":
        return DraftType.SERVICE_PROPOSAL
    if context.company.industry:
        return DraftType.INDUSTRY_PROPOSAL
    return DraftType.PRODUCT_PROPOSAL


def _types_for_event(
    event: FollowUpEvent,
    context: DealContext,
    risk_score: RiskScore | None,
) -> list[DraftType]:
    event_type = event.event_type
    types: list[DraftType] = []

    if event_type == "meeting_completed":
        types.append(DraftType.MEETING_RECAP_EMAIL)

    if event_type == "proposal_sent":
        types.append(DraftType.FOLLOW_UP_EMAIL)

    if event_type == "opportunity_stage_changed":
        payload = event.payload
        if isinstance(payload, OpportunityStageChangedPayload):
            if _is_proposal_stage(payload.new_stage):
                types.append(_resolve_proposal_type(context))
            else:
                types.append(DraftType.FOLLOW_UP_EMAIL)
        else:
            types.append(DraftType.FOLLOW_UP_EMAIL)

    if risk_score is not None and risk_score.level == "HIGH":
        types.append(DraftType.RE_ENGAGEMENT_EMAIL)

    return types


def _dedupe_preserve_order(types: list[DraftType]) -> list[DraftType]:
    seen: set[DraftType] = set()
    ordered: list[DraftType] = []
    for draft_type in types:
        if draft_type not in seen:
            seen.add(draft_type)
            ordered.append(draft_type)
    return ordered


def _sort_by_priority(types: list[DraftType]) -> list[DraftType]:
    priority_index = {draft_type: index for index, draft_type in enumerate(_PRIORITY_ORDER)}
    return sorted(types, key=lambda draft_type: priority_index.get(draft_type, 999))


def resolve_draft_types(
    event: FollowUpEvent,
    context: DealContext,
    risk_score: RiskScore | None = None,
) -> list[DraftType]:
    types = _types_for_event(event, context, risk_score)
    types = _dedupe_preserve_order(types)
    types = _sort_by_priority(types)
    return types[:_MAX_TYPES_PER_RUN]
