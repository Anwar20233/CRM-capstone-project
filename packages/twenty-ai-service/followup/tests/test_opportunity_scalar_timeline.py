from datetime import datetime, timezone

from followup.agents.risk.rules import has_proposal_evidence
from followup.context.crm_fetch import RawOpportunityBundle, _merge_timeline
from followup.context.enrich import compute_engagement_metrics, enrich_context
from followup.context.map_crm import map_deal_context_fallback
from followup.context.opportunity_scalar_timeline import (
    OPPORTUNITY_SCALAR_FIELD_BY_LABEL,
    TIMESTAMP_SOURCE_UNAVAILABLE,
    build_opportunity_scalar_timeline_events,
)
from followup.context.schemas import DealContext, OpportunitySnapshot, TimelineItem


def test_scalar_field_mapping_documents_ui_labels():
    assert OPPORTUNITY_SCALAR_FIELD_BY_LABEL["Email Text"] == "emailText"
    assert OPPORTUNITY_SCALAR_FIELD_BY_LABEL["Notes"] == "notes"


def test_build_opportunity_scalar_timeline_events_from_email_text():
    opportunity = {
        "updatedAt": "2026-06-16T18:50:19.473Z",
        "emailText": (
            "Subject: Follow-up on Platform Migration Proposal\n\n"
            "Hi team,\n\nThank you for the demo."
        ),
    }
    events = build_opportunity_scalar_timeline_events(opportunity)
    assert len(events) == 1
    assert events[0]["type"] == "email"
    assert events[0]["item"]["title"] == "Follow-up on Platform Migration Proposal"
    assert events[0]["item"]["source"] == "opportunity.emailText"
    assert events[0]["item"]["timestamp_source"] == TIMESTAMP_SOURCE_UNAVAILABLE
    assert "updatedAt" not in events[0]
    assert "updatedAt" not in events[0]["item"]
    assert "Thank you for the demo" in events[0]["item"]["summary"]


def test_build_opportunity_scalar_timeline_events_from_notes():
    opportunity = {
        "updatedAt": "2026-06-16T18:50:19.473Z",
        "notes": "Customer asked for security review before signing.",
    }
    events = build_opportunity_scalar_timeline_events(opportunity)
    assert len(events) == 1
    assert events[0]["type"] == "note"
    assert events[0]["item"]["source"] == "opportunity.notes"
    assert events[0]["item"]["timestamp_source"] == TIMESTAMP_SOURCE_UNAVAILABLE
    assert "security review" in events[0]["item"]["summary"]


def test_scalar_events_do_not_use_opportunity_updated_at():
    opportunity = {
        "updatedAt": "2026-06-16T18:50:19.473Z",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "emailText": "Subject: API Integration Questions\n\nPlease confirm webhooks.",
    }
    events = build_opportunity_scalar_timeline_events(opportunity)
    assert events[0].get("updatedAt") is None
    assert events[0]["item"].get("updatedAt") is None


def test_merge_timeline_includes_scalar_opportunity_fields():
    opportunity = {
        "updatedAt": "2026-06-16T18:50:19.473Z",
        "emailText": "Subject: API Integration Questions\n\nPlease confirm webhooks.",
    }
    timeline = _merge_timeline([], [], opportunity=opportunity)
    assert len(timeline) == 1
    assert timeline[0]["type"] == "email"
    assert timeline[0]["item"]["timestamp_source"] == TIMESTAMP_SOURCE_UNAVAILABLE


def test_map_deal_context_fallback_maps_undated_scalar_email_timeline():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Platform Migration",
            "stage": "PROPOSAL",
            "updatedAt": "2026-06-16T18:50:19.473Z",
            "emailText": "Subject: Follow-up on Platform Migration Proposal\n\nBody text",
        },
        timeline=_merge_timeline(
            [],
            [],
            opportunity={
                "id": "opp-1",
                "name": "Platform Migration",
                "stage": "PROPOSAL",
                "updatedAt": "2026-06-16T18:50:19.473Z",
                "emailText": (
                    "Subject: Follow-up on Platform Migration Proposal\n\nBody text"
                ),
            },
        ),
    )
    context = map_deal_context_fallback(bundle)
    assert len(context.timeline) == 1
    timeline_item = context.timeline[0]
    assert timeline_item.type == "email"
    assert timeline_item.title == "Follow-up on Platform Migration Proposal"
    assert timeline_item.occurred_at is None
    assert timeline_item.source == "opportunity.emailText"
    assert timeline_item.timestamp_source == TIMESTAMP_SOURCE_UNAVAILABLE


def test_undated_scalar_timeline_does_not_affect_engagement_metrics():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Platform Migration",
            "stage": "PROPOSAL",
            "updatedAt": "2026-06-16T18:50:19.473Z",
            "emailText": "Subject: Follow-up on Platform Migration Proposal\n\nBody text",
        },
        timeline=_merge_timeline(
            [],
            [],
            opportunity={
                "id": "opp-1",
                "name": "Platform Migration",
                "stage": "PROPOSAL",
                "updatedAt": "2026-06-16T18:50:19.473Z",
                "emailText": (
                    "Subject: Follow-up on Platform Migration Proposal\n\nBody text"
                ),
            },
        ),
    )
    now = datetime(2026, 6, 16, 18, 50, 19, tzinfo=timezone.utc)
    context = enrich_context(map_deal_context_fallback(bundle), now=now)

    assert context.engagement.days_since_last_activity is None
    assert context.engagement.activity_count_14d == 0
    assert context.engagement.activity_count_prior_14d == 0


def test_dated_timeline_still_affects_engagement_metrics():
    now = datetime(2026, 6, 16, 18, 0, tzinfo=timezone.utc)
    context = DealContext(
        opportunity=OpportunitySnapshot(
            id="opp-1",
            name="Deal",
            stage="PROPOSAL",
        ),
        timeline=[
            TimelineItem(
                type="note",
                title="Recent check-in",
                occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
            ),
            TimelineItem(
                type="email",
                title="Follow-up on Platform Migration Proposal",
                summary="Proposal details",
                occurred_at=None,
                source="opportunity.emailText",
                timestamp_source=TIMESTAMP_SOURCE_UNAVAILABLE,
            ),
        ],
    )

    engagement = compute_engagement_metrics(context, now)

    assert engagement.days_since_last_activity == 6
    assert engagement.activity_count_14d == 1
    assert engagement.activity_count_prior_14d == 0


def test_undated_scalar_content_remains_available_for_proposal_detection():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Platform Migration",
            "stage": "PROPOSAL",
            "emailText": "Subject: Follow-up on Platform Migration Proposal\n\nBody",
        },
        timeline=_merge_timeline(
            [],
            [],
            opportunity={
                "id": "opp-1",
                "name": "Platform Migration",
                "stage": "PROPOSAL",
                "emailText": (
                    "Subject: Follow-up on Platform Migration Proposal\n\nBody"
                ),
            },
        ),
    )
    context = map_deal_context_fallback(bundle)

    assert has_proposal_evidence(context)
