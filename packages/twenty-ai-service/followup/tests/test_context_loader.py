import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from followup.context.crm_fetch import RawOpportunityBundle
from followup.context.enrich import enrich_context
from followup.context.errors import ContextLoadError, LlmExtractError
from followup.context.loader import load_deal_context
from followup.context.llm_extract import extract_deal_context
from followup.context.map_crm import map_deal_context_fallback
from followup.context.schemas import (
    DealContext,
    MeetingSnapshot,
    OpportunitySnapshot,
    TimelineItem,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_raw_bundle(name: str = "raw_opportunity_bundle.json") -> RawOpportunityBundle:
    raw = json.loads((FIXTURES_DIR / name).read_text())
    return RawOpportunityBundle.model_validate(raw)


def test_map_deal_context_fallback_matches_core_fields():
    bundle = load_raw_bundle()
    context = map_deal_context_fallback(bundle)

    assert context.opportunity.id == "opp-stale-001"
    assert context.opportunity.name == "Globex Renewal"
    assert context.opportunity.stage == "PROPOSAL"
    assert context.opportunity.amount == 60000.0
    assert context.company is not None
    assert context.company.name == "Globex Inc"
    assert len(context.contacts) == 1
    assert context.contacts[0].name == "Bob Smith"
    assert len(context.timeline) == 2
    assert context.timeline[0].title == "Initial outreach"
    assert len(context.tasks) == 1
    assert context.tasks[0].title == "Send pricing"


def test_enrich_context_computes_engagement_metrics():
    bundle = load_raw_bundle()
    base_context = map_deal_context_fallback(bundle)
    now = datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)
    context = enrich_context(base_context, now=now)

    assert context.loaded_at == now
    assert context.engagement.days_since_last_activity == 23
    assert context.engagement.activity_count_14d == 0
    assert context.engagement.activity_count_prior_14d == 2
    assert context.engagement.has_future_meeting is False
    assert context.tasks[0].is_overdue is True


def test_enrich_context_detects_future_meeting():
    now = datetime(2026, 6, 12, 8, 0, tzinfo=timezone.utc)
    context = DealContext(
        opportunity=OpportunitySnapshot(
            id="opp-1",
            name="Deal",
            stage="PROPOSAL",
        ),
        timeline=[
            TimelineItem(
                type="note",
                title="Check-in",
                occurred_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
            ),
        ],
        meetings=[
            MeetingSnapshot(
                id="meeting-1",
                title="Follow-up",
                starts_at=datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc),
                status="SCHEDULED",
            ),
        ],
    )
    enriched = enrich_context(context, now=now)
    assert enriched.engagement.has_future_meeting is True


@pytest.mark.asyncio
async def test_extract_deal_context_validates_llm_json():
    bundle = load_raw_bundle()
    mock_client = MagicMock()
    mock_openai = MagicMock()
    mock_client.get_openai_client.return_value = mock_openai
    mock_openai.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "opportunity": {
                                "id": "opp-stale-001",
                                "name": "Globex Renewal",
                                "stage": "PROPOSAL",
                            },
                            "contacts": [],
                            "timeline": [],
                            "tasks": [],
                            "meetings": [],
                        },
                    ),
                ),
            ),
        ],
    )

    context = await extract_deal_context(bundle, llm_client=mock_client)
    assert context.opportunity.id == "opp-stale-001"
    assert context.context_provenance == "hybrid"


@pytest.mark.asyncio
async def test_extract_deal_context_raises_on_invalid_json():
    bundle = load_raw_bundle()
    mock_client = MagicMock()
    mock_openai = MagicMock()
    mock_client.get_openai_client.return_value = mock_openai
    mock_openai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="{not-json"))],
    )

    with pytest.raises(LlmExtractError):
        await extract_deal_context(bundle, llm_client=mock_client)


@pytest.mark.asyncio
async def test_load_deal_context_uses_llm_then_enriches(monkeypatch):
    bundle = load_raw_bundle()
    mapped = map_deal_context_fallback(bundle)

    monkeypatch.setattr(
        "followup.context.loader.fetch_opportunity_bundle",
        AsyncMock(return_value=bundle),
    )
    monkeypatch.setattr(
        "followup.context.loader.extract_deal_context",
        AsyncMock(return_value=mapped.model_copy(update={"context_provenance": "hybrid"})),
    )

    context = await load_deal_context(
        "opp-stale-001",
        "workspace-1",
        "user-002",
        role_id="reader-role",
        use_llm=True,
    )
    assert context.opportunity.name == "Globex Renewal"
    assert context.context_provenance == "hybrid"
    assert context.loaded_at is not None


@pytest.mark.asyncio
async def test_load_deal_context_falls_back_when_llm_fails(monkeypatch):
    bundle = load_raw_bundle()

    monkeypatch.setattr(
        "followup.context.loader.fetch_opportunity_bundle",
        AsyncMock(return_value=bundle),
    )
    monkeypatch.setattr(
        "followup.context.loader.extract_deal_context",
        AsyncMock(side_effect=LlmExtractError("bad json")),
    )

    context = await load_deal_context(
        "opp-stale-001",
        "workspace-1",
        "user-002",
        role_id="reader-role",
        use_llm=True,
    )
    assert context.opportunity.name == "Globex Renewal"
    assert context.context_provenance == "crm_fallback"


@pytest.mark.asyncio
async def test_load_deal_context_skips_llm_when_disabled(monkeypatch):
    bundle = load_raw_bundle()
    extract_mock = AsyncMock(side_effect=AssertionError("LLM should not run"))

    monkeypatch.setattr(
        "followup.context.loader.fetch_opportunity_bundle",
        AsyncMock(return_value=bundle),
    )
    monkeypatch.setattr(
        "followup.context.loader.extract_deal_context",
        extract_mock,
    )

    context = await load_deal_context(
        "opp-stale-001",
        "workspace-1",
        "user-002",
        role_id="reader-role",
        use_llm=False,
    )
    assert context.opportunity.id == "opp-stale-001"
    extract_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_opportunity_bundle_raises_when_opportunity_missing(monkeypatch):
    from followup.context.crm_fetch import fetch_opportunity_bundle
    from followup.context.crm_identity import CrmIdentity

    async def fake_forward(path, payload):
        tool = payload.get("tool")
        if tool == "find_one_opportunity":
            return {"ok": False, "error": {"message": "not found"}}
        return {"ok": True, "data": {"success": True, "result": {"records": []}}}

    monkeypatch.setattr("followup.context.crm_fetch.forward", fake_forward)

    with pytest.raises(ContextLoadError) as error_info:
        await fetch_opportunity_bundle(
            "missing-opp",
            CrmIdentity(
                workspace_id="workspace-1",
                user_id="user-1",
                role_id="reader-role",
            ),
        )
    assert error_info.value.code == "BRIDGE_ERROR"
