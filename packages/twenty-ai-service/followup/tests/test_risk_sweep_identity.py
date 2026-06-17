from pathlib import Path

import pytest

from followup.context.schemas import DealContext
from followup.store.risk_snapshot_store import InMemoryRiskSnapshotStore
from followup.workflows.risk_sweep.env import require_sweep_env
from followup.workflows.risk_sweep.sweep import (
    build_sweep_opportunity_record,
    resolve_sweep_authenticated_user_id,
    run_daily_risk_sweep,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> DealContext:
    import json

    raw = json.loads((FIXTURES_DIR / name).read_text())
    return DealContext.model_validate(raw)


@pytest.mark.asyncio
async def test_sweep_prefers_user_id_over_owner_id_for_context_loading():
    healthy = load_fixture("risk_context_healthy.json")
    captured_user_ids: list[str] = []

    async def list_active_opportunities(workspace_id: str) -> list[dict[str, str]]:
        return [
            {
                "id": healthy.opportunity.id,
                "owner_id": "workspace-member-owner-id",
                "user_id": "authenticated-user-id",
                "stage": "PROPOSAL",
            },
        ]

    async def load_deal_context(
        opportunity_id: str,
        workspace_id: str,
        user_id: str,
    ) -> DealContext:
        captured_user_ids.append(user_id)
        return healthy

    store = InMemoryRiskSnapshotStore()
    result = await run_daily_risk_sweep(
        "workspace-001",
        list_active_opportunities=list_active_opportunities,
        load_deal_context_fn=load_deal_context,
        snapshot_store=store,
    )

    assert captured_user_ids == ["authenticated-user-id"]
    assert result.succeeded_count == 1
    assert result.failed_count == 0


@pytest.mark.asyncio
async def test_sweep_falls_back_to_owner_id_when_user_id_absent():
    stale = load_fixture("risk_context_stale.json")
    captured_user_ids: list[str] = []

    async def list_active_opportunities(workspace_id: str) -> list[dict[str, str]]:
        return [
            {
                "id": stale.opportunity.id,
                "owner_id": "legacy-owner-id",
                "stage": "PROPOSAL",
            },
        ]

    async def load_deal_context(
        opportunity_id: str,
        workspace_id: str,
        user_id: str,
    ) -> DealContext:
        captured_user_ids.append(user_id)
        return stale

    store = InMemoryRiskSnapshotStore()
    result = await run_daily_risk_sweep(
        "workspace-001",
        list_active_opportunities=list_active_opportunities,
        load_deal_context_fn=load_deal_context,
        snapshot_store=store,
    )

    assert captured_user_ids == ["legacy-owner-id"]
    assert result.succeeded_count == 1


def test_resolve_sweep_authenticated_user_id_prefers_user_id():
    opportunity = {
        "id": "opp-1",
        "owner_id": "workspace-member-owner-id",
        "user_id": "authenticated-user-id",
        "stage": "PROPOSAL",
    }

    assert resolve_sweep_authenticated_user_id(opportunity) == "authenticated-user-id"


def test_resolve_sweep_authenticated_user_id_falls_back_to_owner_id():
    opportunity = {
        "id": "opp-1",
        "owner_id": "legacy-owner-id",
        "stage": "PROPOSAL",
    }

    assert resolve_sweep_authenticated_user_id(opportunity) == "legacy-owner-id"


def test_sweep_script_builds_separate_owner_and_authenticated_user_ids():
    record = build_sweep_opportunity_record(
        opportunity_id="opp-1",
        owner_id="workspace-member-owner-id",
        authenticated_user_id="authenticated-user-id",
        stage="PROPOSAL",
        name="Platform Migration",
    )

    assert record["owner_id"] == "workspace-member-owner-id"
    assert record["user_id"] == "authenticated-user-id"
    assert record["owner_id"] != record["user_id"]


def test_require_sweep_env_missing_user_id_fails_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "workspace-001")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "role-001")
    monkeypatch.delenv("TWENTY_USER_ID", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        require_sweep_env()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert (
        "TWENTY_USER_ID is required for authenticated sweep context loading"
        in captured.err
    )
