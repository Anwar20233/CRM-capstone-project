import pytest

from followup.agents.risk.rules import (
    compute_risk_score,
    days_in_stage,
    evaluate_stalled_stage,
    has_proposal_evidence,
)
from followup.context.crm_fetch import RawOpportunityBundle, _merge_timeline
from followup.context.enrich import enrich_context
from followup.context.map_crm import map_deal_context_fallback
from followup.context.stage_normalization import (
    FALLBACK_PIPELINE_STAGES,
    build_stage_sla_days,
    is_closed_stage,
    normalize_stage,
    stage_at_or_after,
    stage_index_in_pipeline,
)


@pytest.mark.parametrize(
    ("raw_stage", "expected"),
    [
        ("Proposal", "PROPOSAL"),
        ("proposal", "PROPOSAL"),
        ("PROPOSAL", "PROPOSAL"),
        ("Closed Won", "CLOSED_WON"),
        ("closed-won", "CLOSED_WON"),
        ("CLOSED_WON", "CLOSED_WON"),
        ("NEW", "NEW"),
        (None, "UNKNOWN"),
        ("", "UNKNOWN"),
        ("Mystery Stage", "MYSTERY_STAGE"),
    ],
)
def test_normalize_stage(raw_stage, expected):
    assert normalize_stage(raw_stage) == expected


def test_is_closed_stage():
    assert is_closed_stage("Closed Won") is True
    assert is_closed_stage("PROPOSAL") is False


def test_stage_index_in_pipeline_uses_normalized_names():
    pipeline = ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"]
    assert stage_index_in_pipeline("proposal", pipeline) == 3
    assert stage_at_or_after("PROPOSAL", "PROPOSAL", pipeline) is True


def test_build_pipeline_meta_from_live_stage_options():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Platform Migration",
            "stage": "PROPOSAL",
            "updatedAt": "2026-06-16T18:50:19.473Z",
        },
        pipeline_stages=[
            {"value": "NEW", "label": "New", "position": 0},
            {"value": "SCREENING", "label": "Screening", "position": 1},
            {"value": "MEETING", "label": "Meeting", "position": 2},
            {"value": "PROPOSAL", "label": "Proposal", "position": 3},
            {"value": "CUSTOMER", "label": "Customer", "position": 4},
        ],
    )
    context = map_deal_context_fallback(bundle)

    assert context.opportunity.stage == "PROPOSAL"
    assert context.pipeline_meta.stages == [
        "NEW",
        "SCREENING",
        "MEETING",
        "PROPOSAL",
        "CUSTOMER",
    ]
    assert context.pipeline_meta.stage_sla_days["PROPOSAL"] == 14
    assert context.pipeline_meta.source == "crm_metadata"
    assert (
        context.pipeline_meta.stage_sla_days.get(context.opportunity.stage) == 14
    )


def test_build_pipeline_meta_uses_fallback_when_metadata_missing():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={"id": "opp-1", "name": "Deal", "stage": "NEW"},
        pipeline_stages=[],
    )
    context = map_deal_context_fallback(bundle)

    assert context.pipeline_meta.stages == FALLBACK_PIPELINE_STAGES
    assert context.pipeline_meta.stage_sla_days == build_stage_sla_days(
        FALLBACK_PIPELINE_STAGES,
    )
    assert context.pipeline_meta.source == "fallback_defaults"


def test_stage_entered_at_is_not_copied_from_updated_at():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Platform Migration",
            "stage": "PROPOSAL",
            "updatedAt": "2026-06-16T18:50:19.473Z",
        },
        timeline=_merge_timeline(
            [],
            [],
            opportunity={
                "id": "opp-1",
                "name": "Platform Migration",
                "stage": "PROPOSAL",
                "updatedAt": "2026-06-16T18:50:19.473Z",
                "emailText": "Subject: Follow-up on Platform Migration Proposal\n\nBody",
            },
        ),
    )
    context = map_deal_context_fallback(bundle)

    assert context.opportunity.updated_at is not None
    assert context.opportunity.stage_entered_at is None


def test_stage_entered_at_preserves_real_stage_history():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Deal",
            "stage": "PROPOSAL",
            "stageEnteredAt": "2026-05-01T12:00:00Z",
            "updatedAt": "2026-06-16T18:50:19.473Z",
        },
    )
    context = map_deal_context_fallback(bundle)

    assert context.opportunity.stage_entered_at is not None
    assert context.opportunity.stage_entered_at.isoformat().startswith("2026-05-01")


def test_stalled_stage_skips_when_stage_entered_at_missing():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Platform Migration",
            "stage": "PROPOSAL",
            "updatedAt": "2026-01-01T00:00:00Z",
        },
        pipeline_stages=[
            {"value": "PROPOSAL", "label": "Proposal", "position": 3},
        ],
    )
    context = enrich_context(map_deal_context_fallback(bundle))

    assert days_in_stage(context) is None
    assert evaluate_stalled_stage(context) is None
    assert not any(
        factor.rule_id == "stalled_stage"
        for factor in compute_risk_score(context).factors
    )


def test_stalled_stage_fires_with_real_stage_entered_at():
    bundle = RawOpportunityBundle(
        opportunity_id="opp-1",
        opportunity={
            "id": "opp-1",
            "name": "Deal",
            "stage": "PROPOSAL",
            "stageEnteredAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-06-16T18:50:19.473Z",
        },
        pipeline_stages=[
            {"value": "PROPOSAL", "label": "Proposal", "position": 3},
        ],
    )
    context = enrich_context(map_deal_context_fallback(bundle))

    assert days_in_stage(context) is not None
    assert evaluate_stalled_stage(context) is not None


def test_undated_scalar_proposal_content_still_detected():
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
