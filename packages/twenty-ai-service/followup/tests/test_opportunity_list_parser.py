import asyncio
import importlib.util
from pathlib import Path

import pytest

from followup.context.bridge_parse import (
    build_find_opportunities_args,
    format_opportunity_stage,
    parse_opportunity_nodes_from_bridge_result,
    sanitize_bridge_result_for_debug,
)


def test_build_find_opportunities_args_matches_tool_schema():
    args = build_find_opportunities_args(25)
    assert args == {
        "limit": 25,
        "offset": 0,
        "orderBy": [{"updatedAt": "DescNullsLast"}],
    }


@pytest.mark.parametrize(
    ("result", "expected_ids"),
    [
        (
            {
                "ok": True,
                "data": {
                    "success": True,
                    "result": {
                        "records": [
                            {"id": "a", "name": "Deal A", "stage": "NEW"},
                        ],
                        "count": 1,
                    },
                },
            },
            ["a"],
        ),
        (
            {
                "ok": True,
                "data": {
                    "edges": [{"node": {"id": "b", "name": "Deal B"}}],
                },
            },
            ["b"],
        ),
        (
            {
                "ok": True,
                "data": {
                    "opportunities": {
                        "edges": [{"node": {"id": "c", "name": "Deal C"}}],
                    },
                },
            },
            ["c"],
        ),
        (
            {
                "ok": True,
                "data": {
                    "nodes": [{"id": "d", "name": "Deal D"}],
                },
            },
            ["d"],
        ),
        (
            {
                "ok": True,
                "data": [
                    {"id": "e", "name": "Deal E"},
                ],
            },
            ["e"],
        ),
    ],
)
def test_parse_opportunity_nodes_supports_multiple_shapes(result, expected_ids):
    nodes, status = parse_opportunity_nodes_from_bridge_result(result)
    assert status == "ok"
    assert [node["id"] for node in nodes] == expected_ids


def test_parse_opportunity_nodes_valid_empty_response():
    result = {
        "ok": True,
        "data": {
            "success": True,
            "result": {"records": [], "count": 0},
        },
    }
    nodes, status = parse_opportunity_nodes_from_bridge_result(result)
    assert nodes == []
    assert status == "empty"


def test_parse_opportunity_nodes_no_data():
    nodes, status = parse_opportunity_nodes_from_bridge_result({"ok": True})
    assert nodes == []
    assert status == "no_data"


def test_parse_opportunity_nodes_unrecognized_shape():
    nodes, status = parse_opportunity_nodes_from_bridge_result(
        {"ok": True, "data": {"unexpected": {"items": []}}},
    )
    assert nodes == []
    assert status == "unrecognized"


def test_parse_opportunity_nodes_bridge_error():
    nodes, status = parse_opportunity_nodes_from_bridge_result(
        {"ok": False, "error": {"message": "denied"}},
    )
    assert nodes == []
    assert status == "bridge_error"


def test_format_opportunity_stage_string_and_dict():
    assert format_opportunity_stage("PROPOSAL") == "PROPOSAL"
    assert format_opportunity_stage({"label": "Proposal", "value": "PROPOSAL"}) == "PROPOSAL"


def test_sanitize_bridge_result_for_debug_redacts_secrets():
    payload = {
        "ok": True,
        "data": {
            "api_key": "secret-value",
            "result": {"records": [{"id": "1", "name": "Deal"}]},
        },
    }
    sanitized = sanitize_bridge_result_for_debug(payload)
    assert sanitized["data"]["api_key"] == "[redacted]"
    assert sanitized["data"]["result"]["records"][0]["name"] == "Deal"


def _load_try_script_module():
    script_path = (
        Path(__file__).resolve().parents[2] / "scripts" / "try_load_deal_context.py"
    )
    spec = importlib.util.spec_from_file_location("try_load_deal_context", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_list_opportunities_prints_rows_for_result_records(capsys):
    script = _load_try_script_module()

    async def fake_forward(path, payload):
        assert payload["args"] == build_find_opportunities_args(10)
        return {
            "ok": True,
            "data": {
                "success": True,
                "message": "Found 2 opportunity records",
                "result": {
                    "records": [
                        {
                            "id": "fc747edc-cb00-4078-8d6b-1fab2611dae4",
                            "name": "AI Model Training",
                            "stage": "PROPOSAL",
                        },
                        {
                            "id": "2beb07b0-340c-41d7-be33-5aa91757f329",
                            "name": "API Integration Deal",
                            "stage": {"label": "Meeting", "value": "MEETING"},
                        },
                    ],
                    "count": 2,
                },
            },
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "workspace-1")
    monkeypatch.setenv("TWENTY_USER_ID", "user-1")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role")
    monkeypatch.setattr(script, "forward", fake_forward)

    try:
        asyncio.run(script.list_opportunities())
        output = capsys.readouterr().out
        assert "AI Model Training" in output
        assert "API Integration Deal" in output
        assert "fc747edc-cb00-4078-8d6b-1fab2611dae4" in output
        assert "MEETING" in output
        assert "No opportunities found" not in output
    finally:
        monkeypatch.undo()


def test_list_opportunities_unrecognized_shape_message(capsys):
    script = _load_try_script_module()

    async def fake_forward(path, payload):
        return {"ok": True, "data": {"foo": "bar"}}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("TWENTY_WORKSPACE_ID", "workspace-1")
    monkeypatch.setenv("TWENTY_USER_ID", "user-1")
    monkeypatch.setenv("TWENTY_READER_ROLE_ID", "reader-role")
    monkeypatch.setattr(script, "forward", fake_forward)

    try:
        asyncio.run(script.list_opportunities())
        output = capsys.readouterr().out
        assert "response shape was not recognized" in output
        assert "Bridge response:" in output
        assert "No opportunities found in this workspace." not in output
    finally:
        monkeypatch.undo()
