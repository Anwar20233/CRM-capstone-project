"""Async persistence layer for the Follow-Up Intelligence Agent.

All tables live in the `followup_agent` schema of Twenty's `default` database
(see migrations/001_initial.sql). Records are returned as plain dataclasses whose
field names match the table columns 1:1, so `Model.from_row(record)` is a direct
mapping. jsonb columns are decoded to Python `dict`/`list` by a connection codec
registered in `Database.connect`.

Repositories accept either an `asyncpg.Pool` or a single `asyncpg.Connection`.
Passing a connection lets a test run every call inside one transaction and roll
it back, which keeps the shared dev database clean.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional, TypeVar

import asyncpg

DEFAULT_DSN = "postgres://postgres:postgres@localhost:5432/default"
SCHEMA = "followup_agent"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _dsn(dsn: Optional[str] = None) -> str:
    return dsn or os.environ.get("PG_DATABASE_URL", DEFAULT_DSN)


async def _register_codecs(conn: asyncpg.Connection) -> None:
    # Decode jsonb to Python objects (and encode Python objects back) so callers
    # work with dict/list, never raw JSON strings.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class Database:
    """Thin owner of an asyncpg pool with the jsonb codec pre-registered."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: Optional[str] = None) -> "Database":
        pool = await asyncpg.create_pool(_dsn(dsn), init=_register_codecs)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def apply_migrations(self) -> None:
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)


async def apply_migrations(conn: asyncpg.Connection) -> None:
    """Run every .sql file in migrations/ in lexical order. Idempotent."""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        await conn.execute(path.read_text())


# An executor is anything we can run queries on: a pooled Database, a raw pool,
# or a single connection.
Executor = "Database | asyncpg.Pool | asyncpg.Connection"


@asynccontextmanager
async def _acquire(executor: Any) -> AsyncIterator[asyncpg.Connection]:
    if isinstance(executor, Database):
        executor = executor.pool
    if isinstance(executor, asyncpg.Connection):
        yield executor
    else:  # asyncpg.Pool
        async with executor.acquire() as conn:
            yield conn


# ===========================================================================
# Record models — one dataclass per table, fields match columns exactly.
# ===========================================================================


@dataclass
class ProfileFact:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    entity_type: str
    fact_type: str
    fact_value: str
    source_type: str
    entity_crm_id: Optional[uuid.UUID] = None
    shadow_entity_id: Optional[uuid.UUID] = None
    confidence: float = 0.8
    sentiment: Optional[str] = None
    source_id: Optional[uuid.UUID] = None
    source_snippet: Optional[str] = None
    extracted_at: Optional[datetime] = None
    valid_from: Optional[datetime] = None
    superseded_by: Optional[uuid.UUID] = None


@dataclass
class ProfileRelationship:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    relationship_type: str
    source_type: str
    from_entity_crm_id: Optional[uuid.UUID] = None
    from_shadow_id: Optional[uuid.UUID] = None
    to_entity_crm_id: Optional[uuid.UUID] = None
    to_shadow_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    confidence: float = 0.8
    source_id: Optional[uuid.UUID] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None


@dataclass
class ShadowEntity:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    email_address: Optional[str] = None
    title_or_role: Optional[str] = None
    company_crm_id: Optional[uuid.UUID] = None
    aliases: list[str] = None  # type: ignore[assignment]
    mention_count: int = 1
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    status: str = "shadow"
    promoted_to_crm_id: Optional[uuid.UUID] = None
    promoted_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None
    dismiss_reason: Optional[str] = None


@dataclass
class ProfileExtraction:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    workspace_id: uuid.UUID
    trigger_type: str
    trigger_id: Optional[str] = None
    input_summary: Optional[str] = None
    entities_found: int = 0
    facts_extracted: int = 0
    relationships_extracted: int = 0
    shadow_entities_created: int = 0
    unresolved_mentions: int = 0
    llm_model: Optional[str] = None
    tokens_used: Optional[int] = None
    created_at: Optional[datetime] = None


@dataclass
class PendingAction:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    workspace_id: uuid.UUID
    trigger_type: str
    action_type: str
    action_payload: dict[str, Any]
    trigger_id: Optional[str] = None
    reasoning: Optional[str] = None
    urgency: str = "medium"
    next_step_result: Optional[dict[str, Any]] = None
    risk_assessment: Optional[dict[str, Any]] = None
    draft_result: Optional[dict[str, Any]] = None
    profile_narrative: Optional[str] = None
    status: str = "pending"
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    acted_on_at: Optional[datetime] = None
    acted_on_by: Optional[uuid.UUID] = None
    execution_status: Optional[str] = None
    execution_error: Optional[str] = None
    executed_at: Optional[datetime] = None


@dataclass
class FollowupRun:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    workspace_id: uuid.UUID
    entry_point: str
    trigger_payload: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    profile_loaded: bool = False
    agents_invoked: list[str] = None  # type: ignore[assignment]
    pending_action_id: Optional[uuid.UUID] = None
    error: Optional[str] = None
    status: str = "running"


@dataclass
class RiskDailyScore:
    id: uuid.UUID
    opportunity_id: uuid.UUID
    workspace_id: uuid.UUID
    risk_score: float
    risk_level: str
    top_factors: list[dict[str, Any]]
    assessment: dict[str, Any]
    trigger_type: str = "daily_sweep"
    assessed_at: Optional[datetime] = None
    created_pending_action_id: Optional[uuid.UUID] = None


TModel = TypeVar("TModel")


def _from_row(model: type[TModel], row: Optional[asyncpg.Record]) -> Optional[TModel]:
    if row is None:
        return None
    field_names = {f.name for f in fields(model)}  # type: ignore[arg-type]
    return model(**{k: v for k, v in dict(row).items() if k in field_names})


# ===========================================================================
# Generic write helpers — build INSERT / upsert from a column dict.
# ===========================================================================


async def _insert(conn: asyncpg.Connection, table: str, data: dict[str, Any]) -> asyncpg.Record:
    cols = list(data.keys())
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = (
        f'INSERT INTO {SCHEMA}.{table} ({col_list}) VALUES ({placeholders}) RETURNING *'
    )
    return await conn.fetchrow(sql, *data.values())


async def _upsert(conn: asyncpg.Connection, table: str, data: dict[str, Any]) -> asyncpg.Record:
    cols = list(data.keys())
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    col_list = ", ".join(f'"{c}"' for c in cols)
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c != "id")
    sql = (
        f'INSERT INTO {SCHEMA}.{table} ({col_list}) VALUES ({placeholders}) '
        f"ON CONFLICT (id) DO UPDATE SET {updates} RETURNING *"
    )
    return await conn.fetchrow(sql, *data.values())


def _to_dict(record: Any) -> dict[str, Any]:
    """Accept a dataclass instance or a plain dict of column values."""
    if isinstance(record, dict):
        return {k: v for k, v in record.items() if v is not None or k == "id"}
    return {f.name: getattr(record, f.name) for f in fields(record)}


# ===========================================================================
# Repositories
# ===========================================================================


class ProfileFactRepository:
    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, fact_data: dict[str, Any]) -> ProfileFact:
        async with _acquire(self._executor) as conn:
            return _from_row(ProfileFact, await _insert(conn, "profile_facts", fact_data))  # type: ignore[return-value]

    async def get(self, fact_id: uuid.UUID) -> Optional[ProfileFact]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.profile_facts WHERE id = $1", fact_id
            )
            return _from_row(ProfileFact, row)

    async def get_facts(
        self,
        opportunity_id: uuid.UUID,
        exclude_superseded: bool = True,
        limit: int = 100,
    ) -> list[ProfileFact]:
        clause = "AND superseded_by IS NULL" if exclude_superseded else ""
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.profile_facts "
                f"WHERE opportunity_id = $1 {clause} "
                f"ORDER BY extracted_at DESC LIMIT $2",
                opportunity_id,
                limit,
            )
        return [_from_row(ProfileFact, r) for r in rows]  # type: ignore[misc]

    async def get_facts_for_entity(
        self,
        entity_crm_id: Optional[uuid.UUID] = None,
        shadow_entity_id: Optional[uuid.UUID] = None,
        fact_type: Optional[str] = None,
    ) -> list[ProfileFact]:
        conditions: list[str] = []
        params: list[Any] = []
        if entity_crm_id is not None:
            params.append(entity_crm_id)
            conditions.append(f"entity_crm_id = ${len(params)}")
        if shadow_entity_id is not None:
            params.append(shadow_entity_id)
            conditions.append(f"shadow_entity_id = ${len(params)}")
        if fact_type is not None:
            params.append(fact_type)
            conditions.append(f"fact_type = ${len(params)}")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.profile_facts {where} ORDER BY extracted_at DESC",
                *params,
            )
        return [_from_row(ProfileFact, r) for r in rows]  # type: ignore[misc]

    async def supersede(self, old_fact_id: uuid.UUID, new_fact_id: uuid.UUID) -> None:
        async with _acquire(self._executor) as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.profile_facts SET superseded_by = $2 WHERE id = $1",
                old_fact_id,
                new_fact_id,
            )

    async def reassign_shadow(
        self, from_shadow_id: uuid.UUID, to_shadow_id: uuid.UUID
    ) -> int:
        """Repoint every fact on ``from_shadow_id`` to ``to_shadow_id`` (merge)."""
        async with _acquire(self._executor) as conn:
            result = await conn.execute(
                f"UPDATE {SCHEMA}.profile_facts SET shadow_entity_id = $2 "
                f"WHERE shadow_entity_id = $1",
                from_shadow_id,
                to_shadow_id,
            )
        return int(result.split()[-1])

    async def attach_crm_id(
        self, shadow_entity_id: uuid.UUID, entity_crm_id: uuid.UUID
    ) -> int:
        """Stamp the promoted CRM id onto a shadow's facts (shadow id kept for audit)."""
        async with _acquire(self._executor) as conn:
            result = await conn.execute(
                f"UPDATE {SCHEMA}.profile_facts SET entity_crm_id = $2 "
                f"WHERE shadow_entity_id = $1",
                shadow_entity_id,
                entity_crm_id,
            )
        return int(result.split()[-1])

    async def save(self, fact: Any) -> ProfileFact:
        async with _acquire(self._executor) as conn:
            return _from_row(ProfileFact, await _upsert(conn, "profile_facts", _to_dict(fact)))  # type: ignore[return-value]


class ProfileRelationshipRepository:
    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, relationship_data: dict[str, Any]) -> ProfileRelationship:
        async with _acquire(self._executor) as conn:
            return _from_row(  # type: ignore[return-value]
                ProfileRelationship, await _insert(conn, "profile_relationships", relationship_data)
            )

    async def get(self, relationship_id: uuid.UUID) -> Optional[ProfileRelationship]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.profile_relationships WHERE id = $1", relationship_id
            )
            return _from_row(ProfileRelationship, row)

    async def get_relationships(self, opportunity_id: uuid.UUID) -> list[ProfileRelationship]:
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.profile_relationships "
                f"WHERE opportunity_id = $1 ORDER BY first_seen_at DESC",
                opportunity_id,
            )
        return [_from_row(ProfileRelationship, r) for r in rows]  # type: ignore[misc]

    async def reassign_shadow(
        self, from_shadow_id: uuid.UUID, to_shadow_id: uuid.UUID
    ) -> int:
        """Repoint every relationship endpoint on ``from_shadow_id`` to ``to_shadow_id``."""
        async with _acquire(self._executor) as conn:
            from_result = await conn.execute(
                f"UPDATE {SCHEMA}.profile_relationships SET from_shadow_id = $2 "
                f"WHERE from_shadow_id = $1",
                from_shadow_id,
                to_shadow_id,
            )
            to_result = await conn.execute(
                f"UPDATE {SCHEMA}.profile_relationships SET to_shadow_id = $2 "
                f"WHERE to_shadow_id = $1",
                from_shadow_id,
                to_shadow_id,
            )
        return int(from_result.split()[-1]) + int(to_result.split()[-1])

    async def attach_crm_id(
        self, shadow_entity_id: uuid.UUID, entity_crm_id: uuid.UUID
    ) -> int:
        """Stamp the promoted CRM id onto a shadow's relationship endpoints."""
        async with _acquire(self._executor) as conn:
            from_result = await conn.execute(
                f"UPDATE {SCHEMA}.profile_relationships SET from_entity_crm_id = $2 "
                f"WHERE from_shadow_id = $1",
                shadow_entity_id,
                entity_crm_id,
            )
            to_result = await conn.execute(
                f"UPDATE {SCHEMA}.profile_relationships SET to_entity_crm_id = $2 "
                f"WHERE to_shadow_id = $1",
                shadow_entity_id,
                entity_crm_id,
            )
        return int(from_result.split()[-1]) + int(to_result.split()[-1])

    async def save(self, relationship: Any) -> ProfileRelationship:
        async with _acquire(self._executor) as conn:
            return _from_row(  # type: ignore[return-value]
                ProfileRelationship, await _upsert(conn, "profile_relationships", _to_dict(relationship))
            )


class ShadowEntityRepository:
    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, shadow_data: dict[str, Any]) -> ShadowEntity:
        async with _acquire(self._executor) as conn:
            return _from_row(ShadowEntity, await _insert(conn, "shadow_entities", shadow_data))  # type: ignore[return-value]

    async def get(self, shadow_id: uuid.UUID) -> Optional[ShadowEntity]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.shadow_entities WHERE id = $1", shadow_id
            )
            return _from_row(ShadowEntity, row)

    async def get_shadow_entities(
        self, opportunity_id: uuid.UUID, min_mentions: int = 2
    ) -> list[ShadowEntity]:
        # Surface shadows worth a rep's attention: mentioned often enough OR with a
        # known role. Already-resolved shadows (promoted/dismissed/merged) are
        # excluded; the project also uses 'detected'/'pending_promotion' for
        # still-active shadows, so we filter by what is NOT resolved rather than
        # hardcoding a single 'shadow' status.
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.shadow_entities "
                f"WHERE opportunity_id = $1 "
                f"AND status NOT IN ('promoted', 'dismissed', 'merged') "
                f"AND (mention_count >= $2 OR title_or_role IS NOT NULL) "
                f"ORDER BY mention_count DESC",
                opportunity_id,
                min_mentions,
            )
        return [_from_row(ShadowEntity, r) for r in rows]  # type: ignore[misc]

    async def list_active(
        self,
        opportunity_id: uuid.UUID,
        exclude_statuses: tuple[str, ...] = ("dismissed", "merged"),
    ) -> list[ShadowEntity]:
        # Every still-relevant shadow for the deal, for use as extraction context
        # (the read path uses get_shadow_entities, which is narrower). Resolved
        # shadows — dismissed/merged by default — are filtered out.
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.shadow_entities "
                f"WHERE opportunity_id = $1 AND status <> ALL($2::text[]) "
                f"ORDER BY mention_count DESC",
                opportunity_id,
                list(exclude_statuses),
            )
        return [_from_row(ShadowEntity, r) for r in rows]  # type: ignore[misc]

    async def find_by_email(self, opportunity_id: uuid.UUID, email: str) -> Optional[ShadowEntity]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.shadow_entities "
                f"WHERE opportunity_id = $1 AND email_address = $2",
                opportunity_id,
                email,
            )
            return _from_row(ShadowEntity, row)

    async def find_by_name_fuzzy(self, opportunity_id: uuid.UUID, name: str) -> list[ShadowEntity]:
        # Substring match against the canonical name and any recorded alias.
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.shadow_entities "
                f"WHERE opportunity_id = $1 AND ("
                f"  name ILIKE $2 OR EXISTS ("
                f"    SELECT 1 FROM jsonb_array_elements_text(aliases) alias WHERE alias ILIKE $2"
                f"  )"
                f")",
                opportunity_id,
                f"%{name}%",
            )
        return [_from_row(ShadowEntity, r) for r in rows]  # type: ignore[misc]

    async def save(self, shadow: Any) -> ShadowEntity:
        async with _acquire(self._executor) as conn:
            return _from_row(ShadowEntity, await _upsert(conn, "shadow_entities", _to_dict(shadow)))  # type: ignore[return-value]


class ExtractionLogRepository:
    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, log_data: dict[str, Any]) -> ProfileExtraction:
        async with _acquire(self._executor) as conn:
            return _from_row(  # type: ignore[return-value]
                ProfileExtraction, await _insert(conn, "profile_extractions", log_data)
            )

    async def get(self, log_id: uuid.UUID) -> Optional[ProfileExtraction]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.profile_extractions WHERE id = $1", log_id
            )
            return _from_row(ProfileExtraction, row)


class PendingActionRepository:
    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, action_data: dict[str, Any]) -> PendingAction:
        async with _acquire(self._executor) as conn:
            return _from_row(  # type: ignore[return-value]
                PendingAction, await _insert(conn, "followup_pending_actions", action_data)
            )

    async def get(self, action_id: uuid.UUID) -> Optional[PendingAction]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.followup_pending_actions WHERE id = $1", action_id
            )
            return _from_row(PendingAction, row)

    async def list_pending(
        self, opportunity_id: uuid.UUID, status: str = "pending"
    ) -> list[PendingAction]:
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.followup_pending_actions "
                f"WHERE opportunity_id = $1 AND status = $2 ORDER BY created_at DESC",
                opportunity_id,
                status,
            )
        return [_from_row(PendingAction, r) for r in rows]  # type: ignore[misc]

    async def expire_stale(self, before_timestamp: datetime) -> int:
        async with _acquire(self._executor) as conn:
            result = await conn.execute(
                f"UPDATE {SCHEMA}.followup_pending_actions SET status = 'expired' "
                f"WHERE status = 'pending' AND expires_at < $1",
                before_timestamp,
            )
        # asyncpg returns the command tag, e.g. "UPDATE 3".
        return int(result.split()[-1])

    async def save(self, action: Any) -> PendingAction:
        async with _acquire(self._executor) as conn:
            return _from_row(  # type: ignore[return-value]
                PendingAction, await _upsert(conn, "followup_pending_actions", _to_dict(action))
            )


class RiskSnapshotRepository:
    """Reads the ``risk_snapshots`` table the P3 risk agent writes.

    The read path only needs the latest score per opportunity; nothing here
    writes (that table is owned by P3). Returns ``None`` until a snapshot exists.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def get_latest_score(self, opportunity_id: uuid.UUID) -> Optional[float]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT score FROM {SCHEMA}.risk_snapshots "
                f"WHERE opportunity_id = $1 ORDER BY computed_at DESC LIMIT 1",
                opportunity_id,
            )
        return float(row["score"]) if row is not None else None


class RiskDailyScoreRepository:
    """Persists daily sweep score history for threshold-crossing detection."""

    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, score_data: dict[str, Any]) -> RiskDailyScore:
        async with _acquire(self._executor) as conn:
            return _from_row(  # type: ignore[return-value]
                RiskDailyScore, await _insert(conn, "risk_daily_scores", score_data)
            )

    async def get_latest(self, opportunity_id: uuid.UUID) -> Optional[RiskDailyScore]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.risk_daily_scores "
                f"WHERE opportunity_id = $1 ORDER BY assessed_at DESC, id DESC LIMIT 1",
                opportunity_id,
            )
        return _from_row(RiskDailyScore, row)


class RunLogRepository:
    def __init__(self, executor: Any) -> None:
        self._executor = executor

    async def create(self, run_data: dict[str, Any]) -> FollowupRun:
        async with _acquire(self._executor) as conn:
            return _from_row(FollowupRun, await _insert(conn, "followup_runs", run_data))  # type: ignore[return-value]

    async def get(self, run_id: uuid.UUID) -> Optional[FollowupRun]:
        async with _acquire(self._executor) as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.followup_runs WHERE id = $1", run_id
            )
            return _from_row(FollowupRun, row)

    async def save(self, run: Any) -> FollowupRun:
        async with _acquire(self._executor) as conn:
            return _from_row(FollowupRun, await _upsert(conn, "followup_runs", _to_dict(run)))  # type: ignore[return-value]
