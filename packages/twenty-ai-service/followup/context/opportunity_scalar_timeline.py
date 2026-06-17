from __future__ import annotations

from typing import Any

# UI label -> internal Opportunity field name (Twenty camelCase API names).
# Verified against live workspace opportunity records via agent-bridge.
OPPORTUNITY_SCALAR_FIELD_BY_LABEL: dict[str, str] = {
    "Notes": "notes",
    "Email Text": "emailText",
}

OPPORTUNITY_SCALAR_TIMELINE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("notes", "note", "Notes"),
    ("emailText", "email", "Email"),
)

TIMESTAMP_SOURCE_UNAVAILABLE = "unavailable"


def _non_empty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _title_for_scalar_field(field_name: str, text: str) -> str:
    if field_name == "emailText":
        first_line = text.splitlines()[0].strip() if text else ""
        if first_line.lower().startswith("subject:"):
            subject = first_line.split(":", 1)[1].strip()
            return subject or "Email"
        return "Email"
    if field_name == "notes":
        first_line = text.splitlines()[0].strip() if text else ""
        return first_line[:80] if first_line else "Notes"
    return field_name


def build_opportunity_scalar_timeline_events(
    opportunity: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    for field_name, timeline_type, _label in OPPORTUNITY_SCALAR_TIMELINE_FIELDS:
        text = _non_empty_text(opportunity.get(field_name))
        if text is None:
            continue
        events.append(
            {
                "type": timeline_type,
                "item": {
                    "title": _title_for_scalar_field(field_name, text),
                    "summary": text[:240],
                    "body": text,
                    "source": f"opportunity.{field_name}",
                    "timestamp_source": TIMESTAMP_SOURCE_UNAVAILABLE,
                },
            },
        )

    return events
