"""Unit tests for the Follow-Up extraction pipeline (Step 2).

In-memory fakes for the repositories, the CRM Reader/Orchestrator, the notifier,
and the chat model exercise the whole write path — sender resolution → deal
selection → validation → conflict resolution → persistence → resolution → shadow
lifecycle — without Postgres or a live LLM.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import fields
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from followup.profile.dependencies import PipelineDeps
from followup.profile.extraction import extract_from_email, extract_from_source
from followup.profile.references import crm_label, shadow_label
from followup.profile.resolution import resolve_unknown_persons
from followup.profile.shadow import check_and_auto_promote, merge_shadows
from followup.store.repositories import (
    ProfileExtraction,
    ProfileFact,
    ProfileRelationship,
    ShadowEntity,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build(model: type, data: dict[str, Any], **defaults: Any) -> Any:
    row = {"id": uuid.uuid4(), **defaults, **data}
    names = {f.name for f in fields(model)}
    return model(**{k: v for k, v in row.items() if k in names})


# ===========================================================================
# In-memory fakes
# ===========================================================================


class FakeFactRepo:
    def __init__(self) -> None:
        self.store: dict[uuid.UUID, ProfileFact] = {}

    async def create(self, data: dict[str, Any]) -> ProfileFact:
        fact = _build(ProfileFact, data, extracted_at=_now())
        self.store[fact.id] = fact
        return fact

    async def get_facts_for_entity(
        self,
        entity_crm_id: Optional[uuid.UUID] = None,
        shadow_entity_id: Optional[uuid.UUID] = None,
        fact_type: Optional[str] = None,
    ) -> list[ProfileFact]:
        result = []
        for fact in self.store.values():
            if entity_crm_id is not None and fact.entity_crm_id != entity_crm_id:
                continue
            if shadow_entity_id is not None and fact.shadow_entity_id != shadow_entity_id:
                continue
            if fact_type is not None and fact.fact_type != fact_type:
                continue
            result.append(fact)
        return result

    async def supersede(self, old_id: uuid.UUID, new_id: uuid.UUID) -> None:
        self.store[old_id].superseded_by = new_id

    async def save(self, fact: ProfileFact) -> ProfileFact:
        self.store[fact.id] = fact
        return fact

    async def reassign_shadow(self, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
        count = 0
        for fact in self.store.values():
            if fact.shadow_entity_id == from_id:
                fact.shadow_entity_id = to_id
                count += 1
        return count

    async def attach_crm_id(self, shadow_id: uuid.UUID, crm_id: uuid.UUID) -> int:
        count = 0
        for fact in self.store.values():
            if fact.shadow_entity_id == shadow_id:
                fact.entity_crm_id = crm_id
                count += 1
        return count


class FakeRelRepo:
    def __init__(self) -> None:
        self.store: dict[uuid.UUID, ProfileRelationship] = {}

    async def create(self, data: dict[str, Any]) -> ProfileRelationship:
        rel = _build(ProfileRelationship, data, first_seen_at=_now(), last_seen_at=_now())
        self.store[rel.id] = rel
        return rel

    async def get_relationships(self, opportunity_id: uuid.UUID) -> list[ProfileRelationship]:
        return [r for r in self.store.values() if r.opportunity_id == opportunity_id]

    async def save(self, rel: ProfileRelationship) -> ProfileRelationship:
        self.store[rel.id] = rel
        return rel

    async def reassign_shadow(self, from_id: uuid.UUID, to_id: uuid.UUID) -> int:
        count = 0
        for rel in self.store.values():
            if rel.from_shadow_id == from_id:
                rel.from_shadow_id = to_id
                count += 1
            if rel.to_shadow_id == from_id:
                rel.to_shadow_id = to_id
                count += 1
        return count

    async def attach_crm_id(self, shadow_id: uuid.UUID, crm_id: uuid.UUID) -> int:
        count = 0
        for rel in self.store.values():
            if rel.from_shadow_id == shadow_id:
                rel.from_entity_crm_id = crm_id
                count += 1
            if rel.to_shadow_id == shadow_id:
                rel.to_entity_crm_id = crm_id
                count += 1
        return count


class FakeShadowRepo:
    def __init__(self) -> None:
        self.store: dict[uuid.UUID, ShadowEntity] = {}

    async def create(self, data: dict[str, Any]) -> ShadowEntity:
        shadow = _build(
            ShadowEntity,
            data,
            aliases=[],
            mention_count=1,
            status="shadow",
            first_seen_at=_now(),
            last_seen_at=_now(),
        )
        self.store[shadow.id] = shadow
        return shadow

    async def get(self, shadow_id: uuid.UUID) -> Optional[ShadowEntity]:
        return self.store.get(shadow_id)

    async def find_by_email(self, opportunity_id: uuid.UUID, email: str) -> Optional[ShadowEntity]:
        for shadow in self.store.values():
            if shadow.opportunity_id == opportunity_id and shadow.email_address == email:
                return shadow
        return None

    async def find_by_name_fuzzy(self, opportunity_id: uuid.UUID, name: str) -> list[ShadowEntity]:
        needle = name.casefold()
        matches = []
        for shadow in self.store.values():
            if shadow.opportunity_id != opportunity_id:
                continue
            haystack = [shadow.name or ""] + list(shadow.aliases or [])
            if any(needle in value.casefold() or value.casefold() in needle for value in haystack):
                matches.append(shadow)
        return matches

    async def list_active(
        self, opportunity_id: uuid.UUID, exclude_statuses: tuple[str, ...] = ("dismissed", "merged")
    ) -> list[ShadowEntity]:
        return [
            s
            for s in self.store.values()
            if s.opportunity_id == opportunity_id and s.status not in exclude_statuses
        ]

    async def save(self, shadow: ShadowEntity) -> ShadowEntity:
        self.store[shadow.id] = shadow
        return shadow


class FakeExtractionLogRepo:
    def __init__(self) -> None:
        self.store: dict[uuid.UUID, ProfileExtraction] = {}

    async def create(self, data: dict[str, Any]) -> ProfileExtraction:
        log = _build(ProfileExtraction, data, created_at=_now())
        self.store[log.id] = log
        return log


class FakeCRMReader:
    """Sender-anchored CRM reader fake. All lookups are pre-seeded dicts."""

    def __init__(
        self,
        *,
        people_by_email: Optional[dict[str, dict]] = None,
        company: Optional[dict] = None,
        opportunity: Optional[dict] = None,
        opportunities: Optional[list[dict]] = None,
        contacts: Optional[list[dict]] = None,
    ) -> None:
        self._people_by_email = people_by_email or {}
        self._company = company
        self._opportunity = opportunity
        self._opportunities = opportunities or []
        self._contacts = contacts or []

    async def get_person_by_email(self, email: str):
        return self._people_by_email.get(email)

    async def get_company(self, company_id: str):
        return self._company

    async def get_opportunity(self, opportunity_id: str):
        return self._opportunity

    async def get_open_opportunities_for_company(self, company_id: str):
        return list(self._opportunities)

    async def get_contacts_for_company(self, company_id: str):
        return list(self._contacts)


class FakeCRMOrchestrator:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_contact(self, name, email, role, company_id, workspace_id, initiated_by="followup_agent"):
        record = {"id": str(uuid.uuid4()), "name": name, "email": email, "role": role}
        self.created.append(record)
        return record


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def notify_rep(self, workspace_id, opportunity_id, event_type, payload):
        self.events.append(
            {
                "workspace_id": workspace_id,
                "opportunity_id": opportunity_id,
                "event_type": event_type,
                "payload": payload,
            }
        )


class FakeChatModel:
    """Returns one scripted extraction JSON (the only LLM call in the pipeline)."""

    def __init__(self, extract: dict[str, Any]) -> None:
        self._extract = extract
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return SimpleNamespace(content=json.dumps(self._extract))


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def ids():
    return SimpleNamespace(
        opportunity=str(uuid.uuid4()),
        opportunity_b=str(uuid.uuid4()),
        workspace=str(uuid.uuid4()),
        company=str(uuid.uuid4()),
        alice=str(uuid.uuid4()),
        bob=str(uuid.uuid4()),
    )


@pytest.fixture
def contacts(ids):
    return [
        {"id": ids.alice, "name": "Alice Anders", "role": "Engineer", "company": "Acme", "email": "alice@acme.com"},
        {"id": ids.bob, "name": "Bob Brown", "role": "VP Sales", "company": "Acme", "email": "bob@acme.com"},
    ]


@pytest.fixture
def company(ids):
    return {"id": ids.company, "name": "Acme"}


@pytest.fixture
def direct_reader(ids, contacts, company):
    """Reader for the known-opportunity (direct) path."""
    return FakeCRMReader(
        company=company,
        contacts=contacts,
        opportunity={"id": ids.opportunity, "name": "Acme Expansion", "stage": "PROPOSAL", "company_id": ids.company},
    )


def _make_deps(crm, extract, orchestrator=None, notifier=None, shadows=None):
    return PipelineDeps(
        executor=None,
        crm_reader=crm,
        crm_orchestrator=orchestrator or FakeCRMOrchestrator(),
        notifier=notifier or FakeNotifier(),
        chat_llm=FakeChatModel(extract),
        facts=FakeFactRepo(),
        relationships=FakeRelRepo(),
        shadows=shadows or FakeShadowRepo(),
        extractions=FakeExtractionLogRepo(),
    )


# ===========================================================================
# Direct path (opportunity already known)
# ===========================================================================


async def test_direct_persists_facts_relationships_and_creates_shadow(ids, direct_reader):
    extract = {
        "opportunity_id": crm_label(ids.opportunity),
        "facts": [
            {
                "entity_id": crm_label(ids.alice),
                "fact_type": "concern",
                "fact_value": "worried about integration timeline",
                "confidence": 0.9,
                "sentiment": "negative",
            },
            {"entity_id": crm_label(ids.bob), "fact_type": "vibe", "fact_value": "good"},  # bad type → dropped
            {"entity_id": "crm_does-not-exist", "fact_type": "budget", "fact_value": "$1m"},  # unknown → dropped
        ],
        "relationships": [
            {"from_id": crm_label(ids.alice), "to_id": crm_label(ids.bob), "type": "reports_to"},
        ],
        "unknown_persons": [
            {"name": "Charlie Chase", "apparent_role": "Procurement Lead", "company_context": "Acme"}
        ],
    }
    deps = _make_deps(direct_reader, extract)

    result = await extract_from_source(
        ids.opportunity, ids.workspace, "email", "email_123", "body text", deps=deps
    )

    assert result.facts_created == 1
    assert result.relationships_created == 1
    assert result.shadows_created == 1
    fact = next(iter(deps.facts.store.values()))
    assert fact.entity_crm_id == uuid.UUID(ids.alice)
    assert fact.fact_type == "concern"
    assert fact.sentiment == "negative"
    rel = next(iter(deps.relationships.store.values()))
    assert rel.from_entity_crm_id == uuid.UUID(ids.alice)
    assert rel.to_entity_crm_id == uuid.UUID(ids.bob)
    log = next(iter(deps.extractions.store.values()))
    assert log.facts_extracted == 1
    assert log.shadow_entities_created == 1


async def test_same_source_conflict_supersedes_old_fact(ids, direct_reader):
    deps = _make_deps(direct_reader, {
        "opportunity_id": crm_label(ids.opportunity),
        "facts": [{"entity_id": crm_label(ids.alice), "fact_type": "role", "fact_value": "Staff Engineer"}],
        "relationships": [],
        "unknown_persons": [],
    })
    old = await deps.facts.create(
        {
            "opportunity_id": uuid.UUID(ids.opportunity),
            "entity_type": "contact",
            "entity_crm_id": uuid.UUID(ids.alice),
            "fact_type": "role",
            "fact_value": "Junior Engineer",
            "source_type": "email",
        }
    )

    result = await extract_from_source(ids.opportunity, ids.workspace, "email", "email_999", "body", deps=deps)

    assert result.facts_created == 1
    assert result.facts_superseded == 1
    assert deps.facts.store[old.id].superseded_by is not None


async def test_cross_source_lower_priority_keeps_both_and_discounts(ids, direct_reader):
    deps = _make_deps(direct_reader, {
        "opportunity_id": crm_label(ids.opportunity),
        "facts": [{"entity_id": crm_label(ids.alice), "fact_type": "role", "fact_value": "Manager"}],
        "relationships": [],
        "unknown_persons": [],
    })
    old = await deps.facts.create(
        {
            "opportunity_id": uuid.UUID(ids.opportunity),
            "entity_type": "contact",
            "entity_crm_id": uuid.UUID(ids.alice),
            "fact_type": "role",
            "fact_value": "Director",
            "source_type": "crm_record",
            "confidence": 0.9,
        }
    )

    result = await extract_from_source(ids.opportunity, ids.workspace, "email", "email_555", "body", deps=deps)

    assert result.facts_created == 1
    assert result.facts_superseded == 0
    assert deps.facts.store[old.id].superseded_by is None
    new_fact = next(f for f in deps.facts.store.values() if f.id != old.id)
    assert new_fact.fact_value == "Manager"
    assert new_fact.confidence == pytest.approx(0.7)


async def test_relationship_dedup_updates_last_seen(ids, direct_reader):
    deps = _make_deps(direct_reader, {
        "opportunity_id": crm_label(ids.opportunity),
        "facts": [],
        "relationships": [{"from_id": crm_label(ids.alice), "to_id": crm_label(ids.bob), "type": "reports_to"}],
        "unknown_persons": [],
    })
    await deps.relationships.create(
        {
            "opportunity_id": uuid.UUID(ids.opportunity),
            "from_entity_crm_id": uuid.UUID(ids.alice),
            "to_entity_crm_id": uuid.UUID(ids.bob),
            "relationship_type": "reports_to",
            "source_type": "email",
        }
    )

    result = await extract_from_source(ids.opportunity, ids.workspace, "email", "email_333", "body", deps=deps)

    assert result.relationships_created == 0
    assert result.relationships_updated == 1
    assert len(deps.relationships.store) == 1


# ===========================================================================
# Email path (sender resolution + deal selection)
# ===========================================================================


def _email_reader(ids, contacts, company, opportunities):
    sender = {
        "id": ids.alice,
        "name": "Alice Anders",
        "role": "Engineer",
        "company_id": ids.company,
        "company_name": "Acme",
    }
    return FakeCRMReader(
        people_by_email={"alice@acme.com": sender},
        company=company,
        contacts=contacts,
        opportunities=opportunities,
    )


async def test_email_single_opportunity_extracts(ids, contacts, company):
    reader = _email_reader(ids, contacts, company, [{"id": ids.opportunity, "name": "Acme Expansion", "stage": "PROPOSAL"}])
    deps = _make_deps(reader, {
        "opportunity_id": None,  # one candidate → pipeline uses it anyway
        "facts": [{"entity_id": crm_label(ids.alice), "fact_type": "buying_signal", "fact_value": "ready to sign"}],
        "relationships": [],
        "unknown_persons": [],
    })

    outcome = await extract_from_email(
        ids.workspace, "email", "email_1", "we are ready", "alice@acme.com", deps=deps
    )

    assert outcome.status == "extracted"
    assert outcome.opportunity_id == ids.opportunity
    assert outcome.sender_crm_id == ids.alice
    assert outcome.extraction.facts_created == 1


async def test_email_multiple_opportunities_picked_by_extractor(ids, contacts, company):
    reader = _email_reader(
        ids, contacts, company,
        [
            {"id": ids.opportunity, "name": "Acme Expansion", "stage": "PROPOSAL"},
            {"id": ids.opportunity_b, "name": "Acme Renewal", "stage": "NEGOTIATION"},
        ],
    )
    deps = _make_deps(reader, {
        "opportunity_id": crm_label(ids.opportunity_b),  # extractor disambiguates
        "facts": [{"entity_id": crm_label(ids.alice), "fact_type": "commitment", "fact_value": "will renew"}],
        "relationships": [],
        "unknown_persons": [],
    })

    outcome = await extract_from_email(
        ids.workspace, "email", "email_2", "about the renewal", "alice@acme.com", deps=deps
    )

    assert outcome.status == "extracted"
    assert outcome.opportunity_id == ids.opportunity_b
    fact = next(iter(deps.facts.store.values()))
    assert fact.opportunity_id == uuid.UUID(ids.opportunity_b)


async def test_email_ambiguous_opportunity_halts(ids, contacts, company):
    reader = _email_reader(
        ids, contacts, company,
        [
            {"id": ids.opportunity, "name": "Acme Expansion", "stage": "PROPOSAL"},
            {"id": ids.opportunity_b, "name": "Acme Renewal", "stage": "NEGOTIATION"},
        ],
    )
    deps = _make_deps(reader, {"opportunity_id": None, "facts": [], "relationships": [], "unknown_persons": []})

    outcome = await extract_from_email(
        ids.workspace, "email", "email_3", "vague note", "alice@acme.com", deps=deps
    )

    assert outcome.status == "ambiguous_opportunity"
    assert outcome.extraction is None
    assert set(outcome.candidate_opportunity_ids) == {ids.opportunity, ids.opportunity_b}
    assert deps.facts.store == {}  # nothing persisted


async def test_email_no_open_opportunity_halts(ids, contacts, company):
    reader = _email_reader(ids, contacts, company, [])
    deps = _make_deps(reader, {"opportunity_id": None, "facts": [], "relationships": [], "unknown_persons": []})

    outcome = await extract_from_email(
        ids.workspace, "email", "email_4", "hello", "alice@acme.com", deps=deps
    )

    assert outcome.status == "no_opportunity"
    assert outcome.sender_crm_id == ids.alice
    assert deps.facts.store == {}


async def test_email_unknown_sender_halts(ids, contacts, company):
    reader = FakeCRMReader(people_by_email={}, company=company, contacts=contacts)
    deps = _make_deps(reader, {"opportunity_id": None, "facts": [], "relationships": [], "unknown_persons": []})

    outcome = await extract_from_email(
        ids.workspace, "email", "email_5", "hello", "stranger@nowhere.com", deps=deps
    )

    assert outcome.status == "unknown_sender"
    assert outcome.sender_crm_id is None
    assert deps.facts.store == {}


# ===========================================================================
# Resolution + shadow lifecycle
# ===========================================================================


async def test_shadow_auto_promoted_on_authority_title(ids, direct_reader):
    orchestrator = FakeCRMOrchestrator()
    notifier = FakeNotifier()
    deps = _make_deps(
        direct_reader,
        {
            "opportunity_id": crm_label(ids.opportunity),
            "facts": [],
            "relationships": [],
            "unknown_persons": [
                {"name": "Dana Director", "apparent_role": "VP of Engineering", "company_context": "Acme"}
            ],
        },
        orchestrator=orchestrator,
        notifier=notifier,
    )

    result = await extract_from_source(ids.opportunity, ids.workspace, "email", "email_777", "body", deps=deps)

    assert result.shadows_created == 1
    assert len(orchestrator.created) == 1
    assert notifier.events and notifier.events[0]["event_type"] == "contact_auto_added"
    shadow = next(iter(deps.shadows.store.values()))
    assert shadow.status == "promoted"
    assert shadow.promoted_to_crm_id is not None


async def test_bare_first_name_is_unresolved_not_persisted(ids, direct_reader):
    deps = _make_deps(direct_reader, {
        "opportunity_id": crm_label(ids.opportunity),
        "facts": [],
        "relationships": [],
        "unknown_persons": [{"name": "Sam"}],
    })

    result = await extract_from_source(ids.opportunity, ids.workspace, "email", "email_111", "body", deps=deps)

    assert result.unresolved_mentions == 1
    assert result.shadows_created == 0
    assert deps.shadows.store == {}


async def test_unknown_person_matching_crm_contact_is_not_a_new_shadow(ids, direct_reader):
    deps = _make_deps(direct_reader, {
        "opportunity_id": crm_label(ids.opportunity),
        "facts": [],
        "relationships": [],
        "unknown_persons": [{"name": "Alice", "email": "alice@acme.com"}],
    })

    result = await extract_from_source(ids.opportunity, ids.workspace, "email", "email_222", "body", deps=deps)

    assert result.shadows_created == 0
    assert deps.shadows.store == {}


async def test_merge_shadows_repoints_facts_and_unions_identity(ids, direct_reader):
    deps = _make_deps(direct_reader, {"facts": [], "relationships": [], "unknown_persons": []})

    keep = await deps.shadows.create(
        {"opportunity_id": uuid.UUID(ids.opportunity), "workspace_id": uuid.UUID(ids.workspace), "name": "John", "mention_count": 1}
    )
    merged = await deps.shadows.create(
        {
            "opportunity_id": uuid.UUID(ids.opportunity),
            "workspace_id": uuid.UUID(ids.workspace),
            "name": "John Smith",
            "title_or_role": "CTO",
            "email_address": "john@acme.com",
            "mention_count": 2,
        }
    )
    fact = await deps.facts.create(
        {
            "opportunity_id": uuid.UUID(ids.opportunity),
            "entity_type": "shadow",
            "shadow_entity_id": merged.id,
            "fact_type": "role",
            "fact_value": "CTO",
            "source_type": "email",
        }
    )

    result = await merge_shadows(deps, str(keep.id), str(merged.id))

    assert deps.facts.store[fact.id].shadow_entity_id == keep.id
    assert result.mention_count == 3
    assert "John Smith" in (result.aliases or [])
    assert result.email_address == "john@acme.com"
    assert result.title_or_role == "CTO"
    assert deps.shadows.store[merged.id].status == "merged"


async def test_check_and_auto_promote_on_mention_threshold(ids, direct_reader):
    orchestrator = FakeCRMOrchestrator()
    deps = _make_deps(
        direct_reader, {"facts": [], "relationships": [], "unknown_persons": []}, orchestrator=orchestrator
    )
    shadow = await deps.shadows.create(
        {"opportunity_id": uuid.UUID(ids.opportunity), "workspace_id": uuid.UUID(ids.workspace), "name": "Mentioned A Lot", "mention_count": 3}
    )

    promoted = await check_and_auto_promote(deps, str(shadow.id))

    assert promoted is True
    assert deps.shadows.store[shadow.id].status == "promoted"
    assert len(orchestrator.created) == 1


async def test_resolve_unknown_persons_matches_existing_shadow(ids, direct_reader):
    deps = _make_deps(direct_reader, {"facts": [], "relationships": [], "unknown_persons": []})
    existing = await deps.shadows.create(
        {"opportunity_id": uuid.UUID(ids.opportunity), "workspace_id": uuid.UUID(ids.workspace), "name": "Priya Patel", "mention_count": 1}
    )

    result = await resolve_unknown_persons(
        deps,
        opportunity_id=ids.opportunity,
        workspace_id=ids.workspace,
        unknown_persons=[{"name": "Priya", "likely_matches_shadow": shadow_label(existing.id)}],
        source_type="email",
        source_id="email_444",
        contacts=[],
        company=None,
    )

    assert result.shadow_matches == 1
    assert result.shadows_created == 0
    assert deps.shadows.store[existing.id].mention_count == 2


async def test_resolve_rejects_shadow_hint_when_names_differ(ids, direct_reader):
    # A same-title but differently-named person must NOT be merged into an
    # existing shadow, even when the LLM hints it (Rachel Torres vs shadow David,
    # both "VP of Engineering"). The hint is rejected and a new shadow is made.
    deps = _make_deps(direct_reader, {"facts": [], "relationships": [], "unknown_persons": []})
    david = await deps.shadows.create(
        {
            "opportunity_id": uuid.UUID(ids.opportunity),
            "workspace_id": uuid.UUID(ids.workspace),
            "name": "David",
            "title_or_role": "VP of Engineering",
            "aliases": ["VP of Engineering"],
            "mention_count": 4,
        }
    )

    result = await resolve_unknown_persons(
        deps,
        opportunity_id=ids.opportunity,
        workspace_id=ids.workspace,
        unknown_persons=[
            {
                "name": "Rachel Torres",
                "apparent_role": "VP of Engineering",
                "company_context": "Acme",
                "likely_matches_shadow": shadow_label(david.id),
            }
        ],
        source_type="email",
        source_id="email_555",
        contacts=[],
        company={"id": ids.company, "name": "Acme"},
    )

    assert result.shadow_matches == 0
    assert result.shadows_created == 1
    assert deps.shadows.store[david.id].mention_count == 4  # untouched
    created = next(s for s in deps.shadows.store.values() if s.name == "Rachel Torres")
    assert created.id != david.id
