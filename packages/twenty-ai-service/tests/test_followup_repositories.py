"""Integration tests for the Follow-Up Agent persistence layer.

Each test runs inside a transaction that is rolled back, so the shared dev
database stays clean. Skipped automatically when Postgres is unreachable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from followup.store.repositories import (
    ExtractionLogRepository,
    PendingActionRepository,
    ProfileFactRepository,
    ProfileRelationshipRepository,
    RunLogRepository,
    ShadowEntityRepository,
    _dsn,
    _register_codecs,
    apply_migrations,
)

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(scope="session")
async def _migrated() -> None:
    try:
        conn = await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"Postgres unavailable: {exc}")
    try:
        await apply_migrations(conn)
    finally:
        await conn.close()


@pytest.fixture
async def conn(_migrated: None):
    connection = await asyncpg.connect(_dsn())
    await _register_codecs(connection)
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
def opportunity_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


async def test_profile_fact_roundtrip(conn, opportunity_id):
    repo = ProfileFactRepository(conn)
    crm_id = uuid.uuid4()
    created = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "entity_type": "contact",
            "entity_crm_id": crm_id,
            "fact_type": "concern",
            "fact_value": "pricing too high",
            "confidence": 0.9,
            "sentiment": "negative",
            "source_type": "email",
            "source_id": uuid.uuid4(),
            "source_snippet": "the price is steep",
        }
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.fact_value == "pricing too high"
    assert fetched.entity_crm_id == crm_id
    assert fetched.sentiment == "negative"
    assert fetched.confidence == 0.9
    assert fetched.extracted_at is not None  # server default


async def test_get_facts_excludes_superseded(conn, opportunity_id):
    repo = ProfileFactRepository(conn)
    entity = uuid.uuid4()
    old = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "entity_type": "contact",
            "entity_crm_id": entity,
            "fact_type": "budget",
            "fact_value": "$50k approved",
            "source_type": "note",
        }
    )
    new = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "entity_type": "contact",
            "entity_crm_id": entity,
            "fact_type": "budget",
            "fact_value": "budget cut to $30k",
            "source_type": "note",
        }
    )
    await repo.supersede(old.id, new.id)

    active = await repo.get_facts(opportunity_id, exclude_superseded=True)
    assert {f.id for f in active} == {new.id}

    everything = await repo.get_facts(opportunity_id, exclude_superseded=False)
    assert {f.id for f in everything} == {old.id, new.id}


async def test_get_facts_for_entity_filters(conn, opportunity_id):
    repo = ProfileFactRepository(conn)
    shadow = uuid.uuid4()
    await repo.create(
        {
            "opportunity_id": opportunity_id,
            "entity_type": "shadow",
            "shadow_entity_id": shadow,
            "fact_type": "role",
            "fact_value": "IT Manager",
            "source_type": "email",
        }
    )
    await repo.create(
        {
            "opportunity_id": opportunity_id,
            "entity_type": "shadow",
            "shadow_entity_id": shadow,
            "fact_type": "concern",
            "fact_value": "worried about integration",
            "source_type": "email",
        }
    )
    roles = await repo.get_facts_for_entity(shadow_entity_id=shadow, fact_type="role")
    assert len(roles) == 1
    assert roles[0].fact_value == "IT Manager"

    all_for_shadow = await repo.get_facts_for_entity(shadow_entity_id=shadow)
    assert len(all_for_shadow) == 2


async def test_relationship_roundtrip(conn, opportunity_id):
    repo = ProfileRelationshipRepository(conn)
    boss, report = uuid.uuid4(), uuid.uuid4()
    created = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "from_entity_crm_id": report,
            "to_entity_crm_id": boss,
            "relationship_type": "reports_to",
            "description": "org hierarchy",
            "source_type": "note",
        }
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.relationship_type == "reports_to"
    assert fetched.from_entity_crm_id == report

    listed = await repo.get_relationships(opportunity_id)
    assert [r.id for r in listed] == [created.id]


async def test_shadow_entity_roundtrip_and_lookup(conn, opportunity_id, workspace_id):
    repo = ShadowEntityRepository(conn)
    created = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "name": "Joseph Martin",
            "email_address": "joe@acme.com",
            "title_or_role": "IT Manager",
            "aliases": ["joe", "Joe M."],
            "mention_count": 3,
        }
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.aliases == ["joe", "Joe M."]

    by_email = await repo.find_by_email(opportunity_id, "joe@acme.com")
    assert by_email is not None and by_email.id == created.id

    by_alias = await repo.find_by_name_fuzzy(opportunity_id, "joe")
    assert created.id in {s.id for s in by_alias}


async def test_get_shadow_entities_filtering(conn, opportunity_id, workspace_id):
    repo = ShadowEntityRepository(conn)

    # Few mentions, no role -> excluded.
    await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "name": "Briefly Mentioned",
            "mention_count": 1,
        }
    )
    # Few mentions but has a role -> included.
    has_role = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "name": "Has Role",
            "title_or_role": "CFO",
            "mention_count": 1,
        }
    )
    # Mentioned enough -> included.
    mentioned = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "name": "Mentioned A Lot",
            "mention_count": 4,
        }
    )
    # Already promoted -> excluded regardless of mentions.
    await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "name": "Promoted",
            "mention_count": 9,
            "status": "promoted",
        }
    )

    result = await repo.get_shadow_entities(opportunity_id, min_mentions=2)
    assert {s.id for s in result} == {has_role.id, mentioned.id}


async def test_extraction_log_roundtrip(conn, opportunity_id, workspace_id):
    repo = ExtractionLogRepository(conn)
    created = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "trigger_type": "email",
            "trigger_id": "msg-123",
            "input_summary": "inbound reply from buyer",
            "facts_extracted": 4,
            "shadow_entities_created": 1,
            "llm_model": "deepseek-v4-flash",
            "tokens_used": 812,
        }
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.facts_extracted == 4
    assert fetched.trigger_id == "msg-123"


async def test_pending_action_roundtrip_and_list(conn, opportunity_id, workspace_id):
    repo = PendingActionRepository(conn)
    created = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "trigger_type": "email_signal",
            "action_type": "send_email",
            "action_payload": {"subject": "Following up", "body": "Hi there"},
            "reasoning": "buyer asked for next steps",
            "urgency": "high",
        }
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.action_payload == {"subject": "Following up", "body": "Hi there"}
    assert fetched.urgency == "high"

    pending = await repo.list_pending(opportunity_id)
    assert [a.id for a in pending] == [created.id]

    # save() upserts: flip status to accepted and persist.
    fetched.status = "accepted"
    fetched.acted_on_by = uuid.uuid4()
    saved = await repo.save(fetched)
    assert saved.status == "accepted"
    assert await repo.list_pending(opportunity_id) == []


async def test_expire_stale_bulk_update(conn, opportunity_id, workspace_id):
    repo = PendingActionRepository(conn)
    base = {
        "opportunity_id": opportunity_id,
        "workspace_id": workspace_id,
        "trigger_type": "risk_alert",
        "action_type": "create_task",
        "action_payload": {},
    }
    past = _now() - timedelta(days=1)
    future = _now() + timedelta(days=1)
    await repo.create({**base, "expires_at": past})
    await repo.create({**base, "expires_at": past})
    fresh = await repo.create({**base, "expires_at": future})

    before = await repo.list_pending(opportunity_id)
    assert len(before) == 3

    await repo.expire_stale(_now())

    still_pending = await repo.list_pending(opportunity_id)
    assert {a.id for a in still_pending} == {fresh.id}


async def test_run_log_roundtrip(conn, opportunity_id, workspace_id):
    repo = RunLogRepository(conn)
    created = await repo.create(
        {
            "opportunity_id": opportunity_id,
            "workspace_id": workspace_id,
            "entry_point": "email_signal",
            "trigger_payload": {"message_id": "abc"},
            "agents_invoked": ["next_step", "risk", "drafting"],
            "profile_loaded": True,
        }
    )
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.agents_invoked == ["next_step", "risk", "drafting"]
    assert fetched.trigger_payload == {"message_id": "abc"}
    assert fetched.status == "running"

    created.status = "completed"
    created.duration_ms = 1234
    saved = await repo.save(created)
    assert saved.status == "completed"
    assert saved.duration_ms == 1234
