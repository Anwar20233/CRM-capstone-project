from __future__ import annotations

import json
from typing import Any, Literal

ParseStatus = Literal["ok", "empty", "no_data", "unrecognized", "bridge_error", "tool_error"]

_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
)


def build_find_opportunities_args(limit: int = 10) -> dict[str, Any]:
    return build_find_collection_args(limit=limit)


def build_find_collection_args(
    *,
    limit: int = 20,
    offset: int = 0,
    order_by_field: str = "updatedAt",
    order_by_direction: str = "DescNullsLast",
    **filters: Any,
) -> dict[str, Any]:
    return {
        "limit": limit,
        "offset": offset,
        "orderBy": [{order_by_field: order_by_direction}],
        **filters,
    }


def format_opportunity_stage(stage: Any) -> str:
    from followup.context.stage_normalization import normalize_stage

    return normalize_stage(stage)


def _normalize_edge_items(items: list[Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        node = item.get("node", item)
        if isinstance(node, dict):
            nodes.append(node)
    return nodes


def _is_record_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get("id"))


def extract_result_payload(data: Any) -> Any | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        return data
    if data.get("success") is False:
        return None
    result = data.get("result")
    if result is not None:
        return result
    return data


def extract_records(data: Any) -> list[dict[str, Any]] | None:
    if data is None:
        return None

    if isinstance(data, dict) and ("success" in data or "result" in data):
        payload = extract_result_payload(data)
        if payload is not data:
            nested = extract_records(payload)
            if nested is not None:
                return nested

    if isinstance(data, list):
        records = [item for item in data if _is_record_dict(item)]
        return records

    if not isinstance(data, dict):
        return None

    if _is_record_dict(data):
        return [data]

    if isinstance(data.get("record"), dict) and _is_record_dict(data["record"]):
        return [data["record"]]

    if isinstance(data.get("records"), list):
        return [item for item in data["records"] if _is_record_dict(item)]

    if isinstance(data.get("edges"), list):
        return _normalize_edge_items(data["edges"])

    opportunities = data.get("opportunities")
    if isinstance(opportunities, list):
        return [item for item in opportunities if _is_record_dict(item)]
    if isinstance(opportunities, dict):
        nested = extract_records(opportunities)
        if nested is not None:
            return nested

    if isinstance(data.get("nodes"), list):
        return _normalize_edge_items(data["nodes"])

    fields = data.get("fields")
    if isinstance(fields, list):
        return [item for item in fields if isinstance(item, dict)]

    return None


def extract_single_record(data: Any) -> dict[str, Any] | None:
    records = extract_records(data)
    if not records:
        return None
    return records[0]


def extract_records_from_bridge_result(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], ParseStatus]:
    if not result.get("ok"):
        return [], "bridge_error"

    data = result.get("data")
    if data is None:
        return [], "no_data"

    if isinstance(data, dict) and data.get("success") is False:
        return [], "tool_error"

    records = extract_records(data)
    if records is None:
        return [], "unrecognized"
    if not records:
        return [], "empty"
    return records, "ok"


def extract_single_record_from_bridge_result(
    result: dict[str, Any],
) -> tuple[dict[str, Any] | None, ParseStatus]:
    records, status = extract_records_from_bridge_result(result)
    if status != "ok":
        return None, status
    record = records[0]
    if not _is_record_dict(record):
        return None, "unrecognized"
    return record, "ok"


def extract_pipeline_stage_options(data: Any) -> list[dict[str, Any]]:
    payload = extract_result_payload(data) if isinstance(data, dict) else data

    field_record: dict[str, Any] | None = None
    if isinstance(payload, dict):
        if payload.get("name") == "stage" and isinstance(payload, dict):
            field_record = payload
        elif isinstance(payload.get("record"), dict):
            record = payload["record"]
            if record.get("name") == "stage":
                field_record = record

    if field_record is None:
        metadata_records: list[dict[str, Any]] = []
        if isinstance(payload, dict) and isinstance(payload.get("records"), list):
            metadata_records = [
                item for item in payload["records"] if isinstance(item, dict)
            ]
        if not metadata_records:
            metadata_records = extract_records(data) or []

        for field_metadata in metadata_records:
            if field_metadata.get("name") == "stage":
                field_record = field_metadata
                break

    if field_record is not None:
        settings = field_record.get("settings")
        if isinstance(settings, dict) and isinstance(settings.get("options"), list):
            return [
                option
                for option in settings["options"]
                if isinstance(option, dict)
            ]
        options = field_record.get("options")
        if isinstance(options, list):
            return [option for option in options if isinstance(option, dict)]

    if isinstance(payload, dict):
        options = payload.get("options")
        if isinstance(options, list):
            return [option for option in options if isinstance(option, dict)]

    if isinstance(data, dict):
        options = data.get("options")
        if isinstance(options, list):
            return [option for option in options if isinstance(option, dict)]

    return []


def extract_stage_options_from_bridge_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    if not result.get("ok"):
        return []
    data = result.get("data")
    if not isinstance(data, dict) or data.get("success") is False:
        return []
    return extract_pipeline_stage_options(data)


def parse_opportunity_nodes_from_bridge_result(
    result: dict[str, Any],
) -> tuple[list[dict[str, Any]], ParseStatus]:
    return extract_records_from_bridge_result(result)


def _sanitize_value(key: str, value: Any) -> Any:
    normalized_key = key.lower().replace("-", "_")
    if any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS):
        return "[redacted]"
    if isinstance(value, dict):
        return sanitize_bridge_result_for_debug(value)
    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value[:20]]
    return value


def sanitize_bridge_result_for_debug(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in result.items():
        sanitized[key] = _sanitize_value(key, value)
    return sanitized


def format_bridge_result_for_debug(result: dict[str, Any]) -> str:
    return json.dumps(
        sanitize_bridge_result_for_debug(result),
        indent=2,
        default=str,
    )
