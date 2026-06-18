from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from followup.contracts.risk import (
    DatabaseRiskAgent,
    RiskAssessment,
    RiskAssessmentRequest,
    RiskDealContext,
    build_risk_deal_context_from_db,
    evaluate_risk_context,
    resolve_workspace_schema,
)


OPPORTUNITY_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_ID = "22222222-2222-2222-2222-222222222222"


class FakeAcquire:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "FakeConnection":
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class FakePool:
    def __init__(self, conn: "FakeConnection") -> None:
        self._conn = conn

    async def __aenter__(self) -> "FakePool":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self._conn)


class FakeConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        now = datetime.now(timezone.utc)
        self.opportunity = {
            "opportunity_id": OPPORTUNITY_ID,
            "name": "Acme Expansion",
            "stage": "PROPOSAL",
            "closeDate": now - timedelta(days=2),
            "updatedAt": now - timedelta(days=30),
            "createdAt": now - timedelta(days=80),
            "amountAmountMicros": 100_000_000_000,
            "amountCurrencyCode": "USD",
            "companyId": "company-1",
            "pointOfContactId": None,
            "ownerId": "owner-1",
            "deletedAt": None,
        }

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.queries.append(query)
        if 'core."dataSource"' in query:
            return "workspace_c4en9trdpordobem3offy83aa"
        return 1

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.queries.append(query)
        if '"opportunity"' in query:
            return self.opportunity
        if "followup_pending_actions" in query:
            return {
                "id": "pending-1",
                "opportunity_id": OPPORTUNITY_ID,
                "profile_narrative": "Buyer went quiet after procurement concern.",
                "urgency": "high",
                "reasoning": "Previous next-step reasoning.",
                "status": "pending",
                "created_at": datetime.now(timezone.utc),
            }
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        now = datetime.now(timezone.utc)
        if "profile_facts" in query:
            return [
                {
                    "id": "fact-1",
                    "opportunity_id": OPPORTUNITY_ID,
                    "entity_type": "opportunity",
                    "entity_crm_id": "entity-1",
                    "fact_type": "concern",
                    "fact_value": "Procurement is delaying signature.",
                    "confidence": 0.9,
                    "source_type": "email",
                    "source_id": "source-1",
                    "extracted_at": now - timedelta(days=1),
                    "sentiment": "negative",
                    "source_snippet": "procurement needs more time",
                    "valid_from": now - timedelta(days=1),
                    "superseded_by": None,
                }
            ]
        if "profile_relationships" in query:
            return [
                {
                    "id": "rel-1",
                    "opportunity_id": OPPORTUNITY_ID,
                    "relationship_type": "blocks",
                    "description": "Procurement owner blocks signature.",
                    "confidence": 0.85,
                    "first_seen_at": now - timedelta(days=5),
                    "last_seen_at": now - timedelta(days=1),
                }
            ]
        if '"message"' in query:
            return []
        if '"note"' in query:
            return []
        if '"task"' in query:
            return []
        return []


@pytest.mark.asyncio
async def test_build_risk_context_fetches_database_evidence(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setattr(
        "followup.contracts.risk.asyncpg.create_pool",
        lambda *args, **kwargs: FakePool(conn),
    )

    context = await build_risk_deal_context_from_db(OPPORTUNITY_ID, WORKSPACE_ID)

    assert context.opportunity["opportunity_id"] == OPPORTUNITY_ID
    assert context.profile_facts[0]["fact_type"] == "concern"
    assert context.profile_narrative == "Buyer went quiet after procurement concern."
    assert context.profile_relationships[0]["relationship_type"] == "blocks"
    assert context.recent_messages == []
    assert context.recent_notes == []
    assert context.recent_tasks == []


@pytest.mark.asyncio
async def test_build_risk_context_ignores_mock_risk_fields(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setattr(
        "followup.contracts.risk.asyncpg.create_pool",
        lambda *args, **kwargs: FakePool(conn),
    )

    await build_risk_deal_context_from_db(OPPORTUNITY_ID, WORKSPACE_ID)

    all_queries = "\n".join(conn.queries)
    assert "risk_snapshots" not in all_queries
    assert "risk_assessment" not in all_queries


@pytest.mark.asyncio
async def test_resolve_workspace_schema_allows_valid_dev_override(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setenv(
        "FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE",
        "workspace_c4en9trdpordobem3offy83aa",
    )

    schema = await resolve_workspace_schema(conn, opportunity_id=OPPORTUNITY_ID)

    assert schema == "workspace_c4en9trdpordobem3offy83aa"
    all_queries = "\n".join(conn.queries)
    assert 'core."dataSource"' not in all_queries
    assert '"workspace_c4en9trdpordobem3offy83aa"."opportunity"' in all_queries


@pytest.mark.asyncio
async def test_database_risk_agent_uses_only_minimal_identifiers() -> None:
    calls: list[tuple[str, str | None]] = []

    async def context_builder(
        opportunity_id: str, workspace_id: str | None
    ) -> RiskDealContext:
        calls.append((opportunity_id, workspace_id))
        return _risk_context()

    agent = DatabaseRiskAgent(context_builder=context_builder)
    result = await agent.run(
        RiskAssessmentRequest(
            opportunity_id=OPPORTUNITY_ID,
            workspace_id=WORKSPACE_ID,
            trigger_type="risk_sweep",
        )
    )

    assert calls == [(OPPORTUNITY_ID, WORKSPACE_ID)]
    assert isinstance(result, RiskAssessment)
    assert result.recommended_notification["should_notify"] is True
    assert result.metadata["used_previous_risk_snapshot"] is False


@pytest.mark.asyncio
async def test_database_risk_agent_exposes_p1_evaluate_entrypoint() -> None:
    async def context_builder(
        opportunity_id: str, workspace_id: str | None
    ) -> RiskDealContext:
        assert opportunity_id == OPPORTUNITY_ID
        assert workspace_id == WORKSPACE_ID
        return _risk_context()

    agent = DatabaseRiskAgent(context_builder=context_builder)

    result = await agent.evaluate_deal_risk(
        opportunity_id=OPPORTUNITY_ID,
        workspace_id=WORKSPACE_ID,
        trigger_type="daily_sweep",
    )

    assert result.opportunity_id == OPPORTUNITY_ID
    assert result.risk_factors == result.factors
    assert result.risk_factors[0].factor == result.risk_factors[0].factor_type
    assert result.metadata["trigger_type"] == "daily_sweep"


def test_evaluate_risk_context_handles_missing_optional_profile_data() -> None:
    context = _risk_context(profile_facts=[], profile_narrative=None)

    result = evaluate_risk_context(context)

    assert result.opportunity_id == OPPORTUNITY_ID
    assert result.factors
    assert result.recommended_notification["recommended_action"]


def _risk_context(
    *,
    profile_facts: list[dict[str, Any]] | None = None,
    profile_narrative: str | None = "Buyer went quiet after procurement concern.",
) -> RiskDealContext:
    now = datetime.now(timezone.utc)
    return RiskDealContext(
        opportunity={
            "opportunity_id": OPPORTUNITY_ID,
            "name": "Acme Expansion",
            "stage": "PROPOSAL",
            "closeDate": now - timedelta(days=1),
            "updatedAt": now - timedelta(days=35),
            "createdAt": now - timedelta(days=80),
            "amountAmountMicros": 100_000_000_000,
            "amountCurrencyCode": "USD",
            "companyId": "company-1",
            "pointOfContactId": None,
            "ownerId": "owner-1",
        },
        profile_facts=profile_facts
        if profile_facts is not None
        else [
            {
                "fact_type": "concern",
                "fact_value": "Procurement is delaying signature.",
                "confidence": 0.9,
                "sentiment": "negative",
                "source_snippet": "procurement needs more time",
            }
        ],
        profile_relationships=[
            {
                "relationship_type": "blocks",
                "description": "Procurement owner blocks signature.",
                "confidence": 0.85,
            }
        ],
        profile_narrative=profile_narrative,
        pending_action={
            "id": "pending-1",
            "profile_narrative": profile_narrative,
            "urgency": "high",
            "reasoning": "Previous next-step reasoning.",
            "status": "pending",
        },
        recent_messages=[],
        recent_notes=[],
        recent_tasks=[],
    )
