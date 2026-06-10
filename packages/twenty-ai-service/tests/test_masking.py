"""Tests for the masking layer: EntityHandleMap, CRMResolver, BaseWorker wiring.

Verifies:
- Handle registration: resolved handles keyed by record id (reused across turns,
  fresh for a different record); privacy handles idempotent by value.
- mask_text / mask_value replace entities with handles; unmask_text /
  unmask_value translate handles — including dotted fields — back to values.
- The unresolved-reference guard flags handles/fields that don't exist.
- The CRM resolver's single/multiple/none outcomes, fuzzy ilike filters, and the
  joint person+company search with name-only fallback.
- BaseWorker enables masking by default and honours mask_pii=False.
"""

import json

import pytest

from agent.masking import CRMResolver, EntityHandleMap
from agent.tool_scope import READER_SCOPE
from agent.workers.base_worker import BaseWorker


# A deterministic extractor stub so tests never load the Presidio model. Returns
# the same {label, text, start, end} shape as pipelines.extract.
_KNOWN_ENTITIES = [
    ("person", "John Doe"),
    ("company", "Acme Corp"),
    ("company", "Acme"),
    ("email address", "jane@example.com"),
]


def fake_extractor(text: str) -> list[dict]:
    found = []
    for label, value in _KNOWN_ENTITIES:
        index = text.find(value)
        if index >= 0:
            found.append({"label": label, "text": value, "start": index, "end": index + len(value)})
    return found


@pytest.fixture
def handle_map() -> EntityHandleMap:
    return EntityHandleMap(extractor=fake_extractor)


PERSON_RECORD = {
    "id": "person-uuid-1",
    "name": {"firstName": "John", "lastName": "Doe"},
    "emails": {"primaryEmail": "john@acme.com"},
}
COMPANY_RECORD = {"id": "company-uuid-1", "name": "Acme Corp", "domainName": {"primaryLinkUrl": "acme.com"}}


# ---------------------------------------------------------------------------
# Handle registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_resolved_handle_exposes_record_fields(self, handle_map: EntityHandleMap) -> None:
        handle = handle_map.register_resolved("person", PERSON_RECORD)
        assert handle.name == "person001"
        assert handle.record_id == "person-uuid-1"
        assert handle.fields["id"] == "person-uuid-1"
        assert handle.fields["name"] == "John Doe"
        assert handle.fields["email"] == "john@acme.com"

    def test_same_record_reuses_one_handle(self, handle_map: EntityHandleMap) -> None:
        first = handle_map.register_resolved("person", PERSON_RECORD)
        second = handle_map.register_resolved("person", PERSON_RECORD)
        assert first.name == second.name == "person001"
        assert len(handle_map) == 1

    def test_different_record_gets_a_fresh_handle(self, handle_map: EntityHandleMap) -> None:
        handle_map.register_resolved("person", PERSON_RECORD)
        other = handle_map.register_resolved(
            "person", {"id": "person-uuid-2", "name": {"firstName": "John", "lastName": "Smith"}}
        )
        assert other.name == "person002"
        assert len(handle_map) == 2

    def test_privacy_handle_is_idempotent_by_value(self, handle_map: EntityHandleMap) -> None:
        first = handle_map.register_privacy("email", "jane@example.com")
        second = handle_map.register_privacy("email", "jane@example.com")
        assert first is second
        assert first.name == "email001"

    def test_counters_are_per_entity_type(self, handle_map: EntityHandleMap) -> None:
        handle_map.register_resolved("person", PERSON_RECORD)
        handle_map.register_resolved("company", COMPANY_RECORD)
        assert {h.name for h in handle_map.handles} == {"person001", "company001"}


# ---------------------------------------------------------------------------
# Masking / unmasking
# ---------------------------------------------------------------------------

class TestMaskUnmask:
    def test_mask_text_discovers_and_replaces(self, handle_map: EntityHandleMap) -> None:
        masked = handle_map.mask_text("Email John Doe about Acme Corp")
        assert "John Doe" not in masked
        assert "Acme Corp" not in masked
        assert "person001" in masked and "company001" in masked

    def test_resolved_handle_dotted_unmask(self, handle_map: EntityHandleMap) -> None:
        handle_map.register_resolved("person", PERSON_RECORD)
        assert handle_map.unmask_text("use person001.id") == "use person-uuid-1"
        assert handle_map.unmask_text("person001.email") == "john@acme.com"
        # Bare handle renders as the display name.
        assert handle_map.unmask_text("Hi person001") == "Hi John Doe"

    def test_unknown_handle_or_field_left_intact(self, handle_map: EntityHandleMap) -> None:
        handle_map.register_resolved("person", PERSON_RECORD)
        assert handle_map.unmask_text("person009") == "person009"
        assert handle_map.unmask_text("person001.unknown") == "person001.unknown"

    def test_longest_first_replacement(self, handle_map: EntityHandleMap) -> None:
        # "Acme" must not corrupt "Acme Corp": the longer surface wins.
        handle_map.register_resolved("company", COMPANY_RECORD)
        masked = handle_map.mask_text("Acme Corp is great", discover=False)
        assert masked == "company001 is great"

    def test_unmask_value_substitutes_in_tool_args(self, handle_map: EntityHandleMap) -> None:
        handle_map.register_resolved("person", PERSON_RECORD)
        args = {"personId": "person001.id", "note": "ping person001"}
        assert handle_map.unmask_value(args) == {
            "personId": "person-uuid-1",
            "note": "ping John Doe",
        }

    def test_find_unresolved_references(self, handle_map: EntityHandleMap) -> None:
        handle_map.register_resolved("person", PERSON_RECORD)
        leftovers = handle_map.find_unresolved_references(
            {"a": "person001.id", "b": "person009", "c": "person001.bogus"}
        )
        assert set(leftovers) == {"person009", "person001.bogus"}
        # A clean reference is not flagged.
        assert handle_map.find_unresolved_references({"a": "person001.id"}) == []


# ---------------------------------------------------------------------------
# Tool-result record registration
# ---------------------------------------------------------------------------

class TestRecordRegistration:
    def test_mask_value_registers_returned_records(self, handle_map: EntityHandleMap) -> None:
        result = {"ok": True, "data": {"records": [PERSON_RECORD, COMPANY_RECORD]}}
        masked = handle_map.mask_value(result)
        # The returned names are masked to handles; ids stay (not PII).
        assert "John" not in str(masked) and "Acme Corp" not in str(masked)
        assert handle_map._by_record_id["person-uuid-1"].name == "person001"
        assert handle_map._by_record_id["company-uuid-1"].name == "company001"

    def test_captures_record_from_json_string_result(self) -> None:
        # A sub-agent (reader) returns its record as a JSON STRING in `response`.
        # The record — and its id — must still be captured as a resolved handle,
        # not NER-masked into a fieldless privacy token. Extractor off on purpose:
        # structured results are masked by record registration, never by NER.
        handle_map = EntityHandleMap(extractor=lambda text: [])
        reader_response = json.dumps(
            {"resolution": "single", "entity_type": "company", "record": COMPANY_RECORD}
        )
        result = {"ok": True, "data": {"response": reader_response}}
        masked = handle_map.mask_value(result)

        assert "Acme Corp" not in json.dumps(masked)
        handle = handle_map.handle_for_surface("Acme Corp")
        assert handle is not None and handle.fields["id"] == "company-uuid-1"
        assert handle_map.unmask_text("company001.id") == "company-uuid-1"

    def test_resolved_handle_reclaims_a_privacy_squatted_surface(self) -> None:
        # A privacy placeholder registered first must not block the later resolved
        # record from owning the surface (the cross-turn Notion bug).
        handle_map = EntityHandleMap(extractor=lambda text: [])
        handle_map.register_privacy("company", "Acme Corp")
        handle_map.register_resolved("company", COMPANY_RECORD)

        handle = handle_map.handle_for_surface("Acme Corp")
        assert handle is not None and handle.is_resolved
        assert handle.fields["id"] == "company-uuid-1"

    def test_email_field_not_masked_as_person_surface(self) -> None:
        # The email address stored on a person record is a field value, not a
        # masking surface. Adding it to surfaces caused the LLM to see "person001"
        # where an email should appear and unmask it to the person's display name
        # (the Patrick Collison / Dario Amodei bug).
        handle_map = EntityHandleMap(extractor=lambda text: [])
        person = {
            "id": "p1",
            "name": {"firstName": "Dario", "lastName": "Amodei"},
            "emails": {"primaryEmail": "dario@anthropic.com"},
        }
        handle_map.register_resolved("person", person)

        result = {"email": "dario@anthropic.com", "name": "Dario Amodei"}
        masked = handle_map.mask_value(result)
        assert masked["email"] == "dario@anthropic.com", (
            f"email was masked away: {masked['email']!r}"
        )
        assert "Dario" not in masked["name"]

    def test_company_name_not_replaced_inside_url(self) -> None:
        # A company name that appears as a substring inside its own domain URL must
        # not be substituted (the "https://company010.com" Anthropic bug). The name
        # is still replaced in plain prose.
        handle_map = EntityHandleMap(extractor=lambda text: [])
        company = {
            "id": "c1",
            "name": "Anthropic",
            "domainName": {"primaryLinkUrl": "https://anthropic.com"},
        }
        handle_map.register_resolved("company", company)

        result = {"website": "https://anthropic.com", "note": "Contact Anthropic"}
        masked = handle_map.mask_value(result)
        assert masked["website"] == "https://anthropic.com", (
            f"URL was corrupted: {masked['website']!r}"
        )
        assert "Anthropic" not in masked["note"]


# ---------------------------------------------------------------------------
# CRM resolver
# ---------------------------------------------------------------------------

def _envelope(records: list[dict]) -> dict:
    return {"ok": True, "data": {"records": records}}


class TestResolver:
    @pytest.mark.asyncio
    async def test_company_single_match(self) -> None:
        async def search(tool, args):
            return _envelope([COMPANY_RECORD])

        resolution = await CRMResolver(search).resolve_company("acme")
        assert resolution.status == "single"
        assert resolution.record["id"] == "company-uuid-1"

    @pytest.mark.asyncio
    async def test_person_multiple_matches(self) -> None:
        async def search(tool, args):
            return _envelope(
                [PERSON_RECORD, {"id": "p2", "name": {"firstName": "John", "lastName": "Roe"}}]
            )

        resolution = await CRMResolver(search).resolve_person("John")
        assert resolution.status == "multiple"
        assert len(resolution.records) == 2

    @pytest.mark.asyncio
    async def test_none_when_no_match(self) -> None:
        async def search(tool, args):
            return _envelope([])

        resolution = await CRMResolver(search).resolve_company("ghost")
        assert resolution.status == "none"
        assert resolution.record is None

    @pytest.mark.asyncio
    async def test_single_token_uses_or_filter(self) -> None:
        captured = {}

        async def search(tool, args):
            captured["args"] = args
            return _envelope([PERSON_RECORD])

        await CRMResolver(search).resolve_person("John")
        assert "or" in captured["args"]  # match first OR last name
        assert captured["args"]["or"][0]["name"]["firstName"]["ilike"] == "%John%"

    @pytest.mark.asyncio
    async def test_joint_search_narrows_by_company_then_falls_back(self) -> None:
        calls = []

        async def search(tool, args):
            calls.append((tool, args))
            if tool == "find_companies":
                return _envelope([COMPANY_RECORD])
            # First (narrowed) people search finds nothing → triggers fallback.
            if "and" in args:
                return _envelope([])
            return _envelope([PERSON_RECORD])

        resolution = await CRMResolver(search).resolve_person("John", company_name="Acme")
        assert resolution.status == "single"
        assert [tool for tool, _ in calls] == ["find_companies", "find_people", "find_people"]
        # The narrowed search filtered by the resolved company id.
        assert calls[1][1]["and"][0]["company"]["eq"] == "company-uuid-1"


# ---------------------------------------------------------------------------
# BaseWorker wiring
# ---------------------------------------------------------------------------

class TestBaseWorkerWiring:
    def test_masking_on_by_default(self) -> None:
        worker = BaseWorker(scope=READER_SCOPE, system_prompt="x")
        assert isinstance(worker.pii_map, EntityHandleMap)

    def test_mask_pii_false_disables(self) -> None:
        worker = BaseWorker(scope=READER_SCOPE, system_prompt="x", mask_pii=False)
        assert worker.pii_map is None

    def test_shared_map_is_reused(self) -> None:
        shared = EntityHandleMap(extractor=fake_extractor)
        worker = BaseWorker(scope=READER_SCOPE, system_prompt="x", pii_map=shared)
        assert worker.pii_map is shared
