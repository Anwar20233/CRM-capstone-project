"""Unit tests for the Follow-Up profile read path (Step 3).

In-memory fakes for the CRM reader, the repositories, the risk-snapshot table,
and the chat model exercise ``ProfileService`` — the shared load, narrative
assembly, deal-context assembly, JSON-safety of the structured fields, and the
edge cases (no contacts / facts / relationships / risk) — without Postgres or a
live LLM. Mirrors the fakes pattern in test_followup_extraction.py.
"""

from __future__ import annotations

import uuid
from dataclasses import fields
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from followup.profile.dependencies import PipelineDeps
from followup.profile.service import ProfileNotFound, ProfileService
from followup.store.repositories import (
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
    def __init__(self, facts: Optional[list[ProfileFact]] = None) -> None:
        self.store: dict[uuid.UUID, ProfileFact] = {f.id: f for f in (facts or [])}

    async def get_facts(
        self,
        opportunity_id: uuid.UUID,
        exclude_superseded: bool = True,
        limit: int = 100,
    ) -> list[ProfileFact]:
        rows = [
            f
            for f in self.store.values()
            if f.opportunity_id == opportunity_id
            and (not exclude_superseded or f.superseded_by is None)
        ]
        rows.sort(key=lambda f: f.extracted_at or _now(), reverse=True)
        return rows[:limit]

    async def get_facts_for_entity(
        self,
        entity_crm_id: Optional[Any] = None,
        shadow_entity_id: Optional[uuid.UUID] = None,
        fact_type: Optional[str] = None,
    ) -> list[ProfileFact]:
        return [
            f
            for f in self.store.values()
            if entity_crm_id is None or str(f.entity_crm_id) == str(entity_crm_id)
        ]


class FakeRelRepo:
    def __init__(self, rels: Optional[list[ProfileRelationship]] = None) -> None:
        self.store: dict[uuid.UUID, ProfileRelationship] = {r.id: r for r in (rels or [])}

    async def get_relationships(self, opportunity_id: uuid.UUID) -> list[ProfileRelationship]:
        return [r for r in self.store.values() if r.opportunity_id == opportunity_id]


class FakeShadowRepo:
    def __init__(self, shadows: Optional[list[ShadowEntity]] = None) -> None:
        self.store: dict[uuid.UUID, ShadowEntity] = {s.id: s for s in (shadows or [])}

    async def get_shadow_entities(
        self, opportunity_id: uuid.UUID, min_mentions: int = 2
    ) -> list[ShadowEntity]:
        return [
            s
            for s in self.store.values()
            if s.opportunity_id == opportunity_id
            and (s.mention_count >= min_mentions or s.title_or_role is not None)
        ]


class FakeRiskRepo:
    def __init__(self, score: Optional[float] = None) -> None:
        self._score = score

    async def get_latest_score(self, opportunity_id: uuid.UUID) -> Optional[float]:
        return self._score


class FakeCRMReader:
    def __init__(
        self,
        *,
        opportunity: Optional[dict] = None,
        company: Optional[dict] = None,
        contacts: Optional[list[dict]] = None,
        activities: Optional[list[dict]] = None,
    ) -> None:
        self._opportunity = opportunity
        self._company = company
        self._contacts = contacts or []
        self._activities = activities or []

    async def get_opportunity(self, opportunity_id: str):
        return self._opportunity

    async def get_company(self, company_id: str):
        return self._company

    async def get_contacts_for_company(self, company_id: str):
        return list(self._contacts)

    async def get_activities_for_opportunity(self, opportunity_id: str, limit: int = 10):
        return list(self._activities)[:limit]


class FakeChatModel:
    """Returns one canned narrative (the only LLM call in the read path)."""

    def __init__(self, narrative: str = "Synthesized briefing.") -> None:
        self._narrative = narrative
        self.calls = 0
        self.last_messages: Any = None

    async def ainvoke(self, messages):
        self.calls += 1
        self.last_messages = messages
        return SimpleNamespace(content=self._narrative)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def ids():
    return SimpleNamespace(
        opportunity=str(uuid.uuid4()),
        company=str(uuid.uuid4()),
        alice=str(uuid.uuid4()),
        bob=str(uuid.uuid4()),
    )


def _make_deps(reader, *, facts=None, rels=None, shadows=None, risk=None, chat="Briefing."):
    return PipelineDeps(
        executor=None,
        crm_reader=reader,
        crm_orchestrator=SimpleNamespace(),
        notifier=SimpleNamespace(),
        chat_llm=FakeChatModel(chat),
        facts=FakeFactRepo(facts),
        relationships=FakeRelRepo(rels),
        shadows=FakeShadowRepo(shadows),
        risk_snapshots=FakeRiskRepo(risk),
    )


def _full_reader(ids):
    return FakeCRMReader(
        opportunity={
            "id": ids.opportunity,
            "name": "Acme Expansion",
            "stage": "PROPOSAL",
            "value": 50000.0,
            "company_id": ids.company,
        },
        company={"id": ids.company, "name": "Acme"},
        contacts=[
            {"id": ids.alice, "name": "Alice Anders", "role": "Engineer", "email": "alice@acme.com"},
            {"id": ids.bob, "name": "Bob Brown", "role": "VP Sales", "email": "bob@acme.com"},
        ],
        activities=[{"type": "note", "date": "2026-06-01", "summary": "Sent proposal"}],
    )


# ===========================================================================
# build_profile_narrative
# ===========================================================================


async def test_profile_narrative_populates_all_fields(ids):
    facts = [
        _build(
            ProfileFact,
            {
                "opportunity_id": uuid.UUID(ids.opportunity),
                "entity_type": "contact",
                "entity_crm_id": uuid.UUID(ids.alice),
                "fact_type": "concern",
                "fact_value": "worried about timeline",
                "source_type": "email",
                "sentiment": "negative",
            },
            extracted_at=_now(),
        ),
    ]
    rels = [
        _build(
            ProfileRelationship,
            {
                "opportunity_id": uuid.UUID(ids.opportunity),
                "relationship_type": "reports_to",
                "source_type": "email",
            },
            first_seen_at=_now(),
            last_seen_at=_now(),
        ),
    ]
    shadows = [
        _build(
            ShadowEntity,
            {
                "opportunity_id": uuid.UUID(ids.opportunity),
                "workspace_id": uuid.uuid4(),
                "name": "Dana Director",
                "title_or_role": "VP of Eng",
                "mention_count": 3,
            },
        ),
    ]
    deps = _make_deps(
        _full_reader(ids), facts=facts, rels=rels, shadows=shadows, risk=42.0, chat="Deal looks healthy."
    )

    narrative = await ProfileService(deps).build_profile_narrative(ids.opportunity)

    assert narrative.opportunity_id == ids.opportunity
    assert narrative.narrative == "Deal looks healthy."
    assert narrative.risk_score == 42.0
    assert len(narrative.contacts) == 2
    assert narrative.contacts[0].name == "Alice Anders"
    # Alice's concern fact is attached to her ContactSummary and JSON-safe.
    assert narrative.contacts[0].facts[0]["fact_value"] == "worried about timeline"
    assert isinstance(narrative.contacts[0].facts[0]["id"], str)
    assert len(narrative.key_facts) == 1
    assert isinstance(narrative.key_facts[0]["opportunity_id"], str)
    assert len(narrative.relationships) == 1
    assert narrative.generated_at.tzinfo is not None
    assert deps.chat_llm.calls == 1


async def test_profile_narrative_key_facts_capped_at_20_newest_first(ids):
    facts = []
    for index in range(25):
        facts.append(
            _build(
                ProfileFact,
                {
                    "opportunity_id": uuid.UUID(ids.opportunity),
                    "entity_type": "opportunity",
                    "fact_type": "budget",
                    "fact_value": f"fact {index}",
                    "source_type": "email",
                },
                extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc).replace(minute=index),
            )
        )
    deps = _make_deps(_full_reader(ids), facts=facts)

    narrative = await ProfileService(deps).build_profile_narrative(ids.opportunity)

    assert len(narrative.key_facts) == 20
    # Newest first: the highest-minute fact leads.
    assert narrative.key_facts[0]["fact_value"] == "fact 24"


# ===========================================================================
# build_deal_context
# ===========================================================================


async def test_deal_context_populates_all_fields(ids):
    facts = [
        _build(
            ProfileFact,
            {
                "opportunity_id": uuid.UUID(ids.opportunity),
                "entity_type": "contact",
                "entity_crm_id": uuid.UUID(ids.alice),
                "fact_type": "concern",
                "fact_value": "pricing too high",
                "source_type": "email",
            },
            extracted_at=_now(),
        ),
        _build(
            ProfileFact,
            {
                "opportunity_id": uuid.UUID(ids.opportunity),
                "entity_type": "opportunity",
                "fact_type": "competitor",
                "fact_value": "evaluating Globex",
                "source_type": "email",
            },
            extracted_at=_now(),
        ),
    ]
    deps = _make_deps(_full_reader(ids), facts=facts, risk=70.0)

    context = await ProfileService(deps).build_deal_context(ids.opportunity)

    assert context.opportunity_id == ids.opportunity
    assert context.opportunity_name == "Acme Expansion"
    assert context.deal_stage == "PROPOSAL"
    assert context.deal_value == 50000.0
    assert context.company_name == "Acme"
    assert context.risk_score == 70.0
    assert len(context.contacts) == 2
    assert context.recent_activities == [
        {"type": "note", "date": "2026-06-01", "summary": "Sent proposal"}
    ]
    # Only the concern fact lands in open_concerns, and it is JSON-safe.
    assert len(context.open_concerns) == 1
    assert context.open_concerns[0]["fact_value"] == "pricing too high"
    assert isinstance(context.open_concerns[0]["opportunity_id"], str)


# ===========================================================================
# Edge cases
# ===========================================================================


async def test_missing_opportunity_raises(ids):
    deps = _make_deps(FakeCRMReader(opportunity=None))

    with pytest.raises(ProfileNotFound):
        await ProfileService(deps).build_profile_narrative(ids.opportunity)


async def test_empty_deal_yields_valid_objects(ids):
    reader = FakeCRMReader(
        opportunity={
            "id": ids.opportunity,
            "name": "Bare Deal",
            "stage": "NEW",
            "company_id": None,
        },
    )
    deps = _make_deps(reader, chat="Early-stage deal, little known.")

    narrative = await ProfileService(deps).build_profile_narrative(ids.opportunity)
    context = await ProfileService(deps).build_deal_context(ids.opportunity)

    assert narrative.contacts == []
    assert narrative.key_facts == []
    assert narrative.relationships == []
    assert narrative.risk_score is None
    assert narrative.narrative == "Early-stage deal, little known."

    assert context.contacts == []
    assert context.open_concerns == []
    assert context.key_relationships == []
    assert context.recent_activities == []
    assert context.deal_value == 0.0
    assert context.company_name == ""
    assert context.risk_score is None


async def test_no_company_skips_contacts_and_company_lookup(ids):
    # company_id absent → no contacts roster, empty company name, no crash.
    reader = FakeCRMReader(
        opportunity={
            "id": ids.opportunity,
            "name": "Solo Deal",
            "stage": "QUALIFIED",
            "value": 1000.0,
            "company_id": None,
        },
        contacts=[{"id": ids.alice, "name": "Ignored", "role": None, "email": None}],
    )
    deps = _make_deps(reader)

    context = await ProfileService(deps).build_deal_context(ids.opportunity)

    assert context.contacts == []
    assert context.company_name == ""
