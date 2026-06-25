"""Standalone daily risk sweep.

The sweep is a backend job: it discovers active opportunities, scores each one
with the database-backed Risk Agent, stores the daily score, and creates a
``risk_alert`` pending action when a deal newly crosses into attention-worthy
risk.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from followup.contracts.risk import (
    DatabaseRiskAgent,
    RiskAgent,
    RiskAssessment,
)
from followup.store.repositories import (
    Database,
    PendingActionRepository,
    RiskDailyScore,
    RiskDailyScoreRepository,
    apply_migrations,
)


DEFAULT_DATABASE_URL = "postgres://postgres:postgres@localhost:5432/default"
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")
DEFAULT_SWEEP_LIMIT = 500
RISK_ALERT_TRIGGER_TYPE = "risk_alert"
DAILY_SWEEP_TRIGGER_TYPE = "daily_sweep"
_TRUSTED_WORKSPACE_SCHEMA = re.compile(r"workspace_[a-zA-Z0-9_]+")
_TERMINAL_STAGES = frozenset({"CUSTOMER", "CLOSED", "CLOSED_WON", "CLOSED_LOST", "WON", "LOST"})
_RISK_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class OpportunityCandidate:
    opportunity_id: str
    workspace_id: uuid.UUID
    workspace_schema: str
    name: str | None = None
    stage: str | None = None


@dataclass(frozen=True)
class RiskSweepResult:
    opportunity: OpportunityCandidate
    assessment: RiskAssessment | None
    previous_score: RiskDailyScore | None
    alert_created: bool = False
    pending_action_id: uuid.UUID | None = None
    skipped_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SweepSummary:
    scanned: int
    scored: int
    alerts_created: int
    skipped: int
    failed: int
    results: list[RiskSweepResult]


def _database_url() -> str:
    return os.getenv("DATABASE_URL", os.getenv("PG_DATABASE_URL", DEFAULT_DATABASE_URL))


def _quote_schema(schema_name: str) -> str:
    if not _TRUSTED_WORKSPACE_SCHEMA.fullmatch(schema_name):
        raise ValueError(f"Unsafe workspace schema: {schema_name!r}")
    return f'"{schema_name}"'


def _as_uuid(value: Any, *, fallback: uuid.UUID | None = None) -> uuid.UUID:
    if value is None:
        if fallback is None:
            raise ValueError("UUID value is required")
        return fallback
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


@asynccontextmanager
async def _acquire(executor: Any) -> AsyncIterator[Any]:
    if isinstance(executor, Database):
        executor = executor.pool
    if hasattr(executor, "fetch") and hasattr(executor, "fetchrow"):
        yield executor
        return
    async with executor.acquire() as conn:
        yield conn


def should_create_risk_alert(
    *,
    previous_score: RiskDailyScore | None,
    assessment: RiskAssessment,
    has_pending_risk_alert: bool = False,
) -> bool:
    if has_pending_risk_alert:
        return False
    if not assessment.recommended_notification.get("should_notify"):
        return False

    current_rank = _RISK_LEVEL_RANK.get(assessment.risk_level, 0)
    if previous_score is None:
        return current_rank >= _RISK_LEVEL_RANK["medium"]

    previous_rank = _RISK_LEVEL_RANK.get(previous_score.risk_level, 0)
    return current_rank > previous_rank and current_rank >= _RISK_LEVEL_RANK["medium"]


def _expires_at(urgency: str) -> datetime:
    hours = {"high": 24, "medium": 72, "low": 168}.get(urgency, 72)
    return _now() + timedelta(hours=hours)


def _action_type_for(assessment: RiskAssessment) -> str:
    return "escalate" if assessment.risk_level == "high" else "follow_up_call"


def _top_factors(assessment: RiskAssessment, limit: int = 3) -> list[dict[str, Any]]:
    return [asdict(factor) for factor in assessment.factors[:limit]]


def _risk_alert_payload(assessment: RiskAssessment) -> dict[str, Any]:
    return {
        "risk_score": assessment.risk_score,
        "risk_level": assessment.risk_level,
        "top_factors": _top_factors(assessment),
        "reasoning_summary": assessment.reasoning_summary,
        "recommended_notification": assessment.recommended_notification,
        "metadata": assessment.metadata,
    }


class DailyRiskSweep:
    def __init__(
        self,
        executor: Any,
        *,
        risk_agent: RiskAgent | None = None,
        score_repository: RiskDailyScoreRepository | None = None,
        pending_action_repository: PendingActionRepository | None = None,
        limit: int = DEFAULT_SWEEP_LIMIT,
    ) -> None:
        self._executor = executor
        self._risk_agent = risk_agent or DatabaseRiskAgent()
        self._scores = score_repository or RiskDailyScoreRepository(executor)
        self._pending_actions = pending_action_repository or PendingActionRepository(executor)
        self._limit = limit

    async def discover_active_opportunities(
        self, *, unprocessed_only: bool = False
    ) -> list[OpportunityCandidate]:
        schema_override = os.getenv("FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE")
        if schema_override:
            workspace_id = _as_uuid(
                os.getenv("FOLLOWUP_RISK_WORKSPACE_ID_OVERRIDE"),
                fallback=DEFAULT_WORKSPACE_ID,
            )
            return await self._discover_schema_opportunities(
                workspace_schema=schema_override,
                workspace_id=workspace_id,
                limit=self._limit,
                unprocessed_only=unprocessed_only,
            )

        async with _acquire(self._executor) as conn:
            data_sources = await conn.fetch(
                '''
                SELECT "workspaceId", "schema"
                FROM core."dataSource"
                WHERE "schema" IS NOT NULL
                ORDER BY "createdAt" DESC
                '''
            )

        # core."dataSource" is the production source of (workspaceId → schema),
        # but a dev/seeded database can have it empty while the workspace schemas
        # exist. Fall back to information_schema so the sweep still finds deals.
        schema_pairs: list[tuple[str, uuid.UUID]] = [
            (ds["schema"], _as_uuid(ds["workspaceId"])) for ds in data_sources
        ]
        if not schema_pairs:
            schema_pairs = await self._discover_schema_pairs_fallback()

        candidates: list[OpportunityCandidate] = []
        for schema, workspace_id in schema_pairs:
            remaining = max(self._limit - len(candidates), 0)
            if remaining == 0:
                break
            if not _TRUSTED_WORKSPACE_SCHEMA.fullmatch(schema):
                continue
            candidates.extend(
                await self._discover_schema_opportunities(
                    workspace_schema=schema,
                    workspace_id=workspace_id,
                    limit=remaining,
                    unprocessed_only=unprocessed_only,
                )
            )
        return candidates

    async def _discover_schema_pairs_fallback(self) -> list[tuple[str, uuid.UUID]]:
        """Resolve (schema → workspace_id) without core."dataSource".

        Scans information_schema for ``workspace_*`` schemas that own an
        ``opportunity`` table. When exactly one workspace exists (the common
        dev/seed case) every schema is attributed to it so stored scores and
        alerts carry the real workspace_id; otherwise we cannot map a schema to
        a workspace and fall back to the zero UUID.
        """
        async with _acquire(self._executor) as conn:
            schema_rows = await conn.fetch(
                """
                SELECT table_schema
                FROM information_schema.tables
                WHERE table_schema LIKE 'workspace\\_%'
                  AND table_name = 'opportunity'
                GROUP BY table_schema
                ORDER BY table_schema
                """
            )
            workspaces = await conn.fetch("SELECT id FROM core.workspace")

        sole_workspace_id = (
            _as_uuid(workspaces[0]["id"]) if len(workspaces) == 1 else DEFAULT_WORKSPACE_ID
        )
        return [
            (row["table_schema"], sole_workspace_id)
            for row in schema_rows
            if _TRUSTED_WORKSPACE_SCHEMA.fullmatch(row["table_schema"])
        ]

    async def _discover_schema_opportunities(
        self,
        *,
        workspace_schema: str,
        workspace_id: uuid.UUID,
        limit: int,
        unprocessed_only: bool = False,
    ) -> list[OpportunityCandidate]:
        schema = _quote_schema(workspace_schema)
        if unprocessed_only:
            # Only fetch opportunities that have never been scored OR whose
            # CRM record was updated after their last risk score — stale scores
            # are a miss, so we re-evaluate rather than silently skipping them.
            query = f'''
                SELECT o.id, o.name, o.stage::text AS stage
                FROM {schema}."opportunity" o
                LEFT JOIN (
                    SELECT opportunity_id, MAX(assessed_at) AS last_assessed_at
                    FROM followup_agent.risk_daily_scores
                    GROUP BY opportunity_id
                ) rds ON rds.opportunity_id = o.id
                WHERE o."deletedAt" IS NULL
                  AND (
                    o.stage IS NULL
                    OR upper(o.stage::text) <> ALL($1::text[])
                  )
                  AND (
                    rds.last_assessed_at IS NULL
                    OR o."updatedAt" > rds.last_assessed_at
                  )
                ORDER BY o."updatedAt" ASC NULLS FIRST
                LIMIT $2
            '''
        else:
            query = f'''
                SELECT id, name, stage::text AS stage
                FROM {schema}."opportunity"
                WHERE "deletedAt" IS NULL
                  AND (
                    stage IS NULL
                    OR upper(stage::text) <> ALL($1::text[])
                  )
                ORDER BY "updatedAt" ASC NULLS FIRST
                LIMIT $2
            '''
        async with _acquire(self._executor) as conn:
            rows = await conn.fetch(query, list(_TERMINAL_STAGES), limit)

        candidates: list[OpportunityCandidate] = []
        for row in rows:
            stage = row.get("stage") if isinstance(row, dict) else row["stage"]
            if str(stage or "").upper() in _TERMINAL_STAGES:
                continue
            candidates.append(
                OpportunityCandidate(
                    opportunity_id=str(row["id"]),
                    workspace_id=workspace_id,
                    workspace_schema=workspace_schema,
                    name=row.get("name") if isinstance(row, dict) else row["name"],
                    stage=stage,
                )
            )
        return candidates

    async def run(
        self,
        opportunities: list[OpportunityCandidate] | None = None,
        *,
        unprocessed_only: bool = False,
    ) -> SweepSummary:
        candidates = (
            opportunities
            if opportunities is not None
            else await self.discover_active_opportunities(unprocessed_only=unprocessed_only)
        )
        results = [await self._score_one(candidate) for candidate in candidates]
        return SweepSummary(
            scanned=len(candidates),
            scored=sum(1 for result in results if result.assessment is not None),
            alerts_created=sum(1 for result in results if result.alert_created),
            skipped=sum(1 for result in results if result.skipped_reason is not None),
            failed=sum(1 for result in results if result.error is not None),
            results=results,
        )

    async def _score_one(self, opportunity: OpportunityCandidate) -> RiskSweepResult:
        previous_score: RiskDailyScore | None = None
        pending_action_id: uuid.UUID | None = None
        try:
            previous_score = await self._scores.get_latest(
                _as_uuid(opportunity.opportunity_id)
            )
            assessment = await self._risk_agent.evaluate_deal_risk(
                opportunity_id=opportunity.opportunity_id,
                workspace_id=str(opportunity.workspace_id)
                if opportunity.workspace_id != DEFAULT_WORKSPACE_ID
                else None,
                trigger_type=DAILY_SWEEP_TRIGGER_TYPE,
            )
            has_pending_alert = await self._has_pending_risk_alert(
                _as_uuid(opportunity.opportunity_id)
            )
            create_alert = should_create_risk_alert(
                previous_score=previous_score,
                assessment=assessment,
                has_pending_risk_alert=has_pending_alert,
            )

            if create_alert:
                pending_action = await self._pending_actions.create(
                    {
                        "opportunity_id": _as_uuid(opportunity.opportunity_id),
                        "workspace_id": opportunity.workspace_id,
                        "trigger_type": RISK_ALERT_TRIGGER_TYPE,
                        "action_type": _action_type_for(assessment),
                        "action_payload": _risk_alert_payload(assessment),
                        "risk_assessment": asdict(assessment),
                        "reasoning": assessment.reasoning_summary,
                        "urgency": assessment.recommended_notification.get("urgency", "medium"),
                        "expires_at": _expires_at(
                            assessment.recommended_notification.get("urgency", "medium")
                        ),
                    }
                )
                pending_action_id = pending_action.id

            await self._scores.create(
                {
                    "opportunity_id": _as_uuid(opportunity.opportunity_id),
                    "workspace_id": opportunity.workspace_id,
                    "risk_score": assessment.risk_score,
                    "risk_level": assessment.risk_level,
                    "top_factors": _top_factors(assessment),
                    "assessment": asdict(assessment),
                    "trigger_type": DAILY_SWEEP_TRIGGER_TYPE,
                    "created_pending_action_id": pending_action_id,
                }
            )

            skipped_reason = None
            if not create_alert:
                skipped_reason = (
                    "pending_risk_alert_exists"
                    if has_pending_alert
                    else "no_threshold_crossing"
                )
            return RiskSweepResult(
                opportunity=opportunity,
                assessment=assessment,
                previous_score=previous_score,
                alert_created=create_alert,
                pending_action_id=pending_action_id,
                skipped_reason=skipped_reason,
            )
        except Exception as error:  # noqa: BLE001
            return RiskSweepResult(
                opportunity=opportunity,
                assessment=None,
                previous_score=previous_score,
                error=str(error),
            )

    async def _has_pending_risk_alert(self, opportunity_id: uuid.UUID) -> bool:
        pending = await self._pending_actions.list_pending(opportunity_id)
        return any(action.trigger_type == RISK_ALERT_TRIGGER_TYPE for action in pending)


def _summary_to_json(summary: SweepSummary) -> str:
    return json.dumps(asdict(summary), indent=2, default=str)


async def run_daily_sweep() -> SweepSummary:
    database = await Database.connect(_database_url())
    try:
        async with database.pool.acquire() as conn:
            await apply_migrations(conn)
        sweep = DailyRiskSweep(database)
        return await sweep.run()
    finally:
        await database.close()


async def run_smart_sweep() -> SweepSummary:
    """Score only opportunities that are new or updated since their last score.

    Use this instead of ``run_daily_sweep`` for incremental runs — it skips
    deals whose CRM record hasn't changed since they were last evaluated, so
    the sweep is fast even with many opportunities in the database.
    """
    database = await Database.connect(_database_url())
    try:
        async with database.pool.acquire() as conn:
            await apply_migrations(conn)
        sweep = DailyRiskSweep(database)
        return await sweep.run(unprocessed_only=True)
    finally:
        await database.close()


async def _main() -> None:
    summary = await run_daily_sweep()
    print(_summary_to_json(summary))


if __name__ == "__main__":
    asyncio.run(_main())
