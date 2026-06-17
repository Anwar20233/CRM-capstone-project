import pytest

from followup.context.bridge_parse import (
    build_find_collection_args,
    extract_pipeline_stage_options,
    extract_records,
    extract_records_from_bridge_result,
    extract_result_payload,
    extract_single_record,
    extract_single_record_from_bridge_result,
    parse_opportunity_nodes_from_bridge_result,
)


def _tool_output_records(records: list[dict]) -> dict:
    return {
        "ok": True,
        "data": {
            "success": True,
            "message": f"Found {len(records)} records",
            "result": {"records": records, "count": str(len(records))},
        },
    }


def test_extract_result_payload_from_tool_output():
    payload = extract_result_payload(
        {
            "success": True,
            "result": {"records": [{"id": "1"}]},
        },
    )
    assert payload == {"records": [{"id": "1"}]}


def test_extract_single_record_from_result_record():
    record = extract_single_record(
        {
            "success": True,
            "result": {
                "record": {"id": "opp-1", "name": "Deal"},
            },
        },
    )
    assert record == {"id": "opp-1", "name": "Deal"}


def test_extract_single_record_from_result_records():
    record = extract_single_record(
        {
            "success": True,
            "result": {
                "records": [{"id": "opp-1", "name": "Platform Migration"}],
            },
        },
    )
    assert record["name"] == "Platform Migration"


def test_extract_records_supports_edges_and_direct_list():
    assert extract_records({"edges": [{"node": {"id": "1"}}]}) == [{"id": "1"}]
    assert extract_records([{"id": "2", "name": "Deal"}]) == [{"id": "2", "name": "Deal"}]


def test_extract_records_from_bridge_result_empty():
    records, status = extract_records_from_bridge_result(_tool_output_records([]))
    assert records == []
    assert status == "empty"


def test_extract_single_record_from_bridge_result_missing():
    record, status = extract_single_record_from_bridge_result(_tool_output_records([]))
    assert record is None
    assert status == "empty"


def test_extract_single_record_from_bridge_result_unrecognized():
    record, status = extract_single_record_from_bridge_result(
        {"ok": True, "data": {"unexpected": True}},
    )
    assert record is None
    assert status == "unrecognized"


def test_extract_pipeline_stage_options_from_field_metadata_records():
    options = extract_pipeline_stage_options(
        {
            "success": True,
            "result": {
                "records": [
                    {"name": "title", "options": []},
                    {
                        "name": "stage",
                        "options": [
                            {"value": "PROPOSAL", "label": "Proposal", "position": 1},
                        ],
                    },
                ],
            },
        },
    )
    assert len(options) == 1
    assert options[0]["label"] == "Proposal"


def test_extract_pipeline_stage_options_from_single_field_record():
    options = extract_pipeline_stage_options(
        {
            "success": True,
            "result": {
                "record": {
                    "name": "stage",
                    "options": [
                        {"value": "NEW", "label": "New", "position": 0},
                        {"value": "PROPOSAL", "label": "Proposal", "position": 3},
                    ],
                },
            },
        },
    )
    assert [option["value"] for option in options] == ["NEW", "PROPOSAL"]


def test_extract_pipeline_stage_options_from_settings_options():
    options = extract_pipeline_stage_options(
        {
            "success": True,
            "result": {
                "name": "stage",
                "settings": {
                    "options": [
                        {"value": "NEW", "label": "New", "position": 0},
                        {"value": "CUSTOMER", "label": "Customer", "position": 4},
                    ],
                },
            },
        },
    )
    assert [option["value"] for option in options] == ["NEW", "CUSTOMER"]


def test_build_find_collection_args_matches_schema():
    args = build_find_collection_args(limit=20)
    assert args == {
        "limit": 20,
        "offset": 0,
        "orderBy": [{"updatedAt": "DescNullsLast"}],
    }


def test_parse_opportunity_nodes_bridge_error():
    nodes, status = parse_opportunity_nodes_from_bridge_result(
        {"ok": False, "error": {"message": "denied"}},
    )
    assert nodes == []
    assert status == "bridge_error"


@pytest.mark.asyncio
async def test_fetch_opportunity_bundle_parses_tool_output_envelope(monkeypatch):
    from followup.context.crm_fetch import fetch_opportunity_bundle
    from followup.context.crm_identity import CrmIdentity

    identity = CrmIdentity(
        workspace_id="workspace-1",
        user_id="user-1",
        role_id="reader-role",
    )
    opportunity_id = "822639e5-9bf7-40f1-8882-a11140362339"

    async def fake_forward(path, payload):
        tool = payload.get("tool")
        if tool == "find_one_opportunity":
            return _tool_output_records(
                [
                    {
                        "id": opportunity_id,
                        "name": "Platform Migration",
                        "stage": "PROPOSAL",
                        "amount": {"amountMicros": 60000000000},
                        "companyId": "company-1",
                        "pointOfContactId": "person-1",
                        "updatedAt": "2026-06-16T18:50:19.473Z",
                    },
                ],
            )
        if tool == "find_one_company":
            return _tool_output_records(
                [{"id": "company-1", "name": "Stripe"}],
            )
        if tool == "find_one_person":
            return _tool_output_records(
                [
                    {
                        "id": "person-1",
                        "name": {"firstName": "Patrick", "lastName": "Collison"},
                    },
                ],
            )
        if tool in {"find_notes", "find_tasks"}:
            return _tool_output_records([])
        if tool == "get_field_metadata":
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "result": {
                        "name": "stage",
                        "options": [
                            {"value": "NEW", "label": "New", "position": 0},
                            {"value": "SCREENING", "label": "Screening", "position": 1},
                            {"value": "MEETING", "label": "Meeting", "position": 2},
                            {"value": "PROPOSAL", "label": "Proposal", "position": 3},
                            {"value": "CUSTOMER", "label": "Customer", "position": 4},
                        ],
                    },
                },
            }
        raise AssertionError(f"Unexpected tool: {tool}")

    monkeypatch.setattr("followup.context.crm_fetch.forward", fake_forward)

    bundle = await fetch_opportunity_bundle(opportunity_id, identity)
    assert bundle.opportunity is not None
    assert bundle.opportunity["name"] == "Platform Migration"
    assert bundle.opportunity["stage"] == "PROPOSAL"
    assert bundle.company is not None
    assert bundle.company["name"] == "Stripe"
    assert bundle.point_of_contact is not None
    assert [stage["value"] for stage in bundle.pipeline_stages] == [
        "NEW",
        "SCREENING",
        "MEETING",
        "PROPOSAL",
        "CUSTOMER",
    ]


@pytest.mark.asyncio
async def test_fetch_opportunity_bundle_raises_when_wrapper_has_no_record(monkeypatch):
    from followup.context.crm_fetch import fetch_opportunity_bundle
    from followup.context.crm_identity import CrmIdentity
    from followup.context.errors import ContextLoadError

    identity = CrmIdentity(
        workspace_id="workspace-1",
        user_id="user-1",
        role_id="reader-role",
    )

    async def fake_forward(path, payload):
        if payload.get("tool") == "find_one_opportunity":
            return {
                "ok": True,
                "data": {
                    "success": True,
                    "message": "Found 0 opportunity records",
                    "result": {"records": [], "count": "0"},
                },
            }
        return _tool_output_records([])

    monkeypatch.setattr("followup.context.crm_fetch.forward", fake_forward)

    with pytest.raises(ContextLoadError) as error_info:
        await fetch_opportunity_bundle("missing-opp", identity)
    assert error_info.value.code == "OPPORTUNITY_NOT_FOUND"


@pytest.mark.asyncio
async def test_fetch_opportunity_bundle_uses_limit_order_by_for_collections(monkeypatch):
    from followup.context.crm_fetch import fetch_opportunity_bundle
    from followup.context.crm_identity import CrmIdentity

    identity = CrmIdentity(
        workspace_id="workspace-1",
        user_id="user-1",
        role_id="reader-role",
    )
    captured_args: dict[str, dict] = {}

    async def fake_forward(path, payload):
        tool = payload.get("tool")
        captured_args[tool] = payload.get("args", {})
        if tool == "find_one_opportunity":
            return _tool_output_records(
                [{"id": "opp-1", "name": "Deal", "stage": "NEW"}],
            )
        return _tool_output_records([])

    monkeypatch.setattr("followup.context.crm_fetch.forward", fake_forward)

    await fetch_opportunity_bundle("opp-1", identity)

    expected = build_find_collection_args(limit=20)
    assert captured_args["find_notes"] == expected
    assert captured_args["find_tasks"] == expected
