import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from followup.api.routes_risk import router
from followup.context.errors import ContextLoadError
from followup.context.schemas import DealContext
from followup.notifications.in_memory_repository import InMemoryNotificationRepository

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PLATFORM_MIGRATION_NOW = datetime(2026, 6, 16, 18, 50, 19, tzinfo=timezone.utc)


def load_fixture(name: str) -> DealContext:
    raw = json.loads((FIXTURES_DIR / name).read_text())
    return DealContext.model_validate(raw)


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.mark.asyncio
async def test_risk_evaluate_returns_score_and_factors(app: FastAPI) -> None:
    context = load_fixture("risk_context_platform_migration.json")

    with patch(
        "followup.api.routes_risk.load_deal_context",
        new=AsyncMock(return_value=context),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/followup/risk/{context.opportunity.id}/evaluate",
                params={
                    "workspace_id": "workspace-001",
                    "user_id": "user-001",
                    "use_llm_context": False,
                    "use_llm_copy": False,
                    "persist_notifications": False,
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["opportunity_id"] == context.opportunity.id
    assert payload["risk_score"]["score"] == 60
    assert payload["risk_score"]["level"] == "medium"
    assert len(payload["factors"]) == 3
    assert payload["persisted"] is False
    assert any(
        evaluation["rule_id"] == "no_future_meeting"
        and evaluation["status"] == "skipped"
        for evaluation in payload["rule_evaluations"]
    )


@pytest.mark.asyncio
async def test_risk_evaluate_opportunity_not_found_returns_404(app: FastAPI) -> None:
    with patch(
        "followup.api.routes_risk.load_deal_context",
        new=AsyncMock(
            side_effect=ContextLoadError(
                "OPPORTUNITY_NOT_FOUND",
                "Opportunity was not found.",
            ),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/followup/risk/missing-opportunity/evaluate",
                params={
                    "workspace_id": "workspace-001",
                    "user_id": "user-001",
                },
            )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "OPPORTUNITY_NOT_FOUND"


@pytest.mark.asyncio
async def test_risk_evaluate_bridge_unreachable_returns_503(app: FastAPI) -> None:
    with patch(
        "followup.api.routes_risk.load_deal_context",
        new=AsyncMock(
            side_effect=ContextLoadError(
                "BRIDGE_UNREACHABLE",
                "CRM bridge is unavailable.",
            ),
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/followup/risk/opp-001/evaluate",
                params={
                    "workspace_id": "workspace-001",
                    "user_id": "user-001",
                },
            )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "BRIDGE_UNREACHABLE"


@pytest.mark.asyncio
async def test_risk_evaluate_persist_notifications_invokes_repository(
    app: FastAPI,
) -> None:
    context = load_fixture("risk_context_platform_migration.json")
    repository = InMemoryNotificationRepository()

    with (
        patch(
            "followup.api.routes_risk.load_deal_context",
            new=AsyncMock(return_value=context),
        ),
        patch(
            "followup.api.routes_risk._DEV_REPOSITORIES",
            {"workspace-001": repository},
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/followup/risk/{context.opportunity.id}/evaluate",
                params={
                    "workspace_id": "workspace-001",
                    "user_id": "user-001",
                    "use_llm_copy": False,
                    "persist_notifications": True,
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["persisted"] is True
    assert repository.count() == len(payload["notifications"])
