"""P3 — Risk Assessment contracts and database-backed risk agent.

The Risk agent is intentionally independent from the shared ``DealContext``
read path. Callers pass only identifiers; the agent resolves the workspace
schema, loads risk-specific CRM/profile/activity evidence from Postgres, and
scores the deal from fresh signals. Existing stored risk snapshots or pending
action ``risk_assessment`` payloads are not read as evidence.
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import asyncpg

from followup.agents.risk.llm_reasoning_summary import generate_llm_reasoning_summary

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/default"
DATABASE_URL = os.getenv("DATABASE_URL", os.getenv("PG_DATABASE_URL", DEFAULT_DATABASE_URL))

RISK_FACTOR_TYPES: frozenset[str] = frozenset(
    {
        "engagement_gap",
        "unresolved_objection",
        "deal_velocity_drop",
        "sentiment_decline",
        "stakeholder_change",
        "budget_concern",
        "missing_stakeholder",
        "missing_next_step",
        "close_date_pressure",
        "positive_momentum",
    }
)

RISK_MODES: frozenset[str] = frozenset({"single", "sweep"})

SEVERITY_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})

_TRUSTED_WORKSPACE_SCHEMA = re.compile(r"^workspace_[a-z0-9]+$")

_RISK_INCREASING_FACT_TYPES = {
    "concern",
    "gate",
    "objection",
    "blocker",
    "delay",
    "process",
}

_RISK_REDUCING_PHRASES = {
    "approved",
    "active",
    "resolved",
    "engaged",
    "buying signal",
}

_RISK_INCREASING_PHRASES = {
    "concern",
    "blocked",
    "blocker",
    "delay",
    "legal",
    "procurement",
    "security",
    "quiet",
    "stalled",
    "missed",
    "objection",
    "timeline",
}


@dataclass
class RiskDealContext:
    opportunity: dict[str, Any]
    profile_facts: list[dict[str, Any]]
    profile_relationships: list[dict[str, Any]]
    profile_narrative: str | None
    pending_action: dict[str, Any] | None
    recent_messages: list[dict[str, Any]]
    recent_notes: list[dict[str, Any]]
    recent_tasks: list[dict[str, Any]]


@dataclass
class RiskFactor:
    """A single contributing risk signal identified by the agent."""

    factor_type: str  # ∈ RISK_FACTOR_TYPES
    description: str
    severity: str  # ∈ SEVERITY_LEVELS
    evidence: str | None = None
    source: str | None = None
    confidence: float | None = None

    @property
    def factor(self) -> str:
        return self.factor_type


@dataclass
class RiskAssessment:
    """Output of the Risk Assessment agent.

    ``risk_score`` is in [0, 100] (100 = highest risk). All fields are
    str/float/list/dict/None so asdict(assessment) is JSON-safe for storage
    in PendingAction.risk_assessment.
    """

    opportunity_id: str
    risk_score: float  # 0–100
    risk_level: str  # ∈ RISK_LEVELS
    factors: list[RiskFactor]  # asdict recurses into each RiskFactor
    previous_score: float | None
    assessed_at: str  # ISO-8601
    reasoning_summary: str
    recommended_notification: dict[str, Any]
    metadata: dict = field(default_factory=dict)

    @property
    def risk_factors(self) -> list[RiskFactor]:
        return self.factors


@dataclass
class RiskAssessmentRequest:
    """Minimal input for the Risk agent.

    The caller does not build or pass ``DealContext``. ``workspace_id`` narrows
    schema resolution; when omitted the context builder finds the opportunity
    across trusted Twenty workspace schemas.
    """

    opportunity_id: str
    workspace_id: str | None = None
    trigger_type: str | None = None
    mode: str = "single"  # ∈ RISK_MODES


@runtime_checkable
class RiskAgent(Protocol):
    async def evaluate_deal_risk(
        self,
        opportunity_id: str,
        workspace_id: str | None = None,
        trigger_type: str | None = None,
    ) -> RiskAssessment: ...

    async def run(self, request: RiskAssessmentRequest) -> RiskAssessment: ...


def _quote_schema(schema_name: str) -> str:
    if not _TRUSTED_WORKSPACE_SCHEMA.fullmatch(schema_name):
        raise ValueError(f"Unsafe workspace schema resolved: {schema_name!r}")
    return f'"{schema_name}"'


def _database_url() -> str:
    return os.getenv("DATABASE_URL", os.getenv("PG_DATABASE_URL", DEFAULT_DATABASE_URL))


async def fetch_all_as_dicts(
    conn: asyncpg.Connection, query: str, *args: Any
) -> list[dict[str, Any]]:
    rows = await conn.fetch(query, *args)
    return [dict(row) for row in rows]


async def resolve_workspace_schema(
    conn: asyncpg.Connection,
    *,
    opportunity_id: str,
    workspace_id: str | None = None,
) -> str:
    schema_override = os.getenv("FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE")
    if schema_override:
        if not _TRUSTED_WORKSPACE_SCHEMA.fullmatch(schema_override):
            raise ValueError(f"Unsafe workspace schema override: {schema_override!r}")
        schema = _quote_schema(schema_override)
        opportunity_exists = await conn.fetchval(
            f'''
            SELECT EXISTS (
              SELECT 1
              FROM {schema}."opportunity"
              WHERE id = $1::uuid
                AND "deletedAt" IS NULL
            )
            ''',
            opportunity_id,
        )
        if not opportunity_exists:
            raise ValueError(
                "Opportunity not found in workspace schema override: "
                f"opportunity_id={opportunity_id}, schema={schema_override}"
            )
        return schema_override

    if workspace_id is not None:
        schema = await conn.fetchval(
            '''
            SELECT "schema"
            FROM core."dataSource"
            WHERE "workspaceId" = $1::uuid
              AND "schema" IS NOT NULL
            ORDER BY "createdAt" DESC
            LIMIT 1
            ''',
            workspace_id,
        )
        if schema:
            if not _TRUSTED_WORKSPACE_SCHEMA.fullmatch(schema):
                raise ValueError(f"Unsafe workspace schema resolved: {schema!r}")
            return schema
        # Dev DBs sometimes lack core.dataSource; fall back to schema discovery.
        discovered = await conn.fetchval(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_schema LIKE 'workspace\\_%' AND table_name = 'person' "
            "ORDER BY table_schema LIMIT 1"
        )
        if discovered and _TRUSTED_WORKSPACE_SCHEMA.fullmatch(discovered):
            return discovered
        raise ValueError(f"Workspace schema not found for workspace_id={workspace_id}")

    schemas = await conn.fetch(
        """
        SELECT table_schema
        FROM information_schema.tables
        WHERE table_schema LIKE 'workspace\\_%'
          AND table_name = 'opportunity'
        ORDER BY table_schema
        """
    )
    for row in schemas:
        schema = row["table_schema"]
        if not _TRUSTED_WORKSPACE_SCHEMA.fullmatch(schema):
            continue
        found = await conn.fetchval(
            f'''
            SELECT 1
            FROM {_quote_schema(schema)}."opportunity"
            WHERE id = $1::uuid
              AND "deletedAt" IS NULL
            LIMIT 1
            ''',
            opportunity_id,
        )
        if found:
            return schema
    raise ValueError(f"Workspace schema not found for opportunity_id={opportunity_id}")


async def build_risk_deal_context_from_db(
    opportunity_id: str,
    workspace_id: str | None = None,
) -> RiskDealContext:
    async with asyncpg.create_pool(_database_url()) as pool:
        async with pool.acquire() as conn:
            workspace_schema = await resolve_workspace_schema(
                conn, opportunity_id=opportunity_id, workspace_id=workspace_id
            )
            schema = _quote_schema(workspace_schema)

            opportunity = await conn.fetchrow(
                f'''
                SELECT
                  id AS opportunity_id,
                  name,
                  stage::text AS stage,
                  "closeDate",
                  "updatedAt",
                  "createdAt",
                  "amountAmountMicros",
                  "amountCurrencyCode",
                  "companyId",
                  "pointOfContactId",
                  "ownerId",
                  "deletedAt"
                FROM {schema}."opportunity"
                WHERE id = $1::uuid
                  AND "deletedAt" IS NULL
                ''',
                opportunity_id,
            )
            if opportunity is None:
                raise ValueError(f"Opportunity not found: {opportunity_id}")

            profile_facts = await fetch_all_as_dicts(
                conn,
                """
                SELECT
                  id,
                  opportunity_id,
                  entity_type,
                  entity_crm_id,
                  fact_type,
                  fact_value,
                  confidence,
                  source_type,
                  source_id,
                  extracted_at,
                  sentiment,
                  source_snippet,
                  valid_from,
                  superseded_by
                FROM followup_agent.profile_facts
                WHERE opportunity_id = $1::uuid
                  AND superseded_by IS NULL
                ORDER BY extracted_at DESC
                """,
                opportunity_id,
            )

            pending_action = await conn.fetchrow(
                """
                SELECT
                  id,
                  opportunity_id,
                  profile_narrative,
                  urgency,
                  reasoning,
                  status,
                  created_at
                FROM followup_agent.followup_pending_actions
                WHERE opportunity_id = $1::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                opportunity_id,
            )

            profile_relationships = await fetch_all_as_dicts(
                conn,
                """
                SELECT *
                FROM followup_agent.profile_relationships
                WHERE opportunity_id = $1::uuid
                ORDER BY COALESCE(last_seen_at, first_seen_at) DESC
                """,
                opportunity_id,
            )

            recent_messages = await fetch_all_as_dicts(
                conn,
                f'''
                SELECT
                  id,
                  subject,
                  text,
                  "receivedAt",
                  "createdAt",
                  "updatedAt"
                FROM {schema}."message"
                WHERE "deletedAt" IS NULL
                ORDER BY COALESCE("receivedAt", "updatedAt", "createdAt") DESC
                LIMIT 20
                ''',
            )

            recent_notes = await fetch_all_as_dicts(
                conn,
                f'''
                SELECT
                  n.id,
                  n.title,
                  n."bodyV2Markdown" AS body,
                  n."createdAt",
                  n."updatedAt"
                FROM {schema}."note" n
                LEFT JOIN {schema}."noteTarget" nt
                  ON nt."noteId" = n.id
                 AND nt."deletedAt" IS NULL
                WHERE n."deletedAt" IS NULL
                  AND (
                    nt."targetOpportunityId" = $1::uuid
                    OR (nt."targetOpportunityId" IS NULL AND nt.id IS NULL)
                  )
                ORDER BY n."updatedAt" DESC
                LIMIT 20
                ''',
                opportunity_id,
            )

            recent_tasks = await fetch_all_as_dicts(
                conn,
                f'''
                SELECT
                  t.id,
                  t.title,
                  t."bodyV2Markdown" AS body,
                  t.status::text AS status,
                  t."dueAt",
                  t."createdAt",
                  t."updatedAt"
                FROM {schema}."task" t
                LEFT JOIN {schema}."taskTarget" tt
                  ON tt."taskId" = t.id
                 AND tt."deletedAt" IS NULL
                WHERE t."deletedAt" IS NULL
                  AND (
                    tt."targetOpportunityId" = $1::uuid
                    OR (tt."targetOpportunityId" IS NULL AND tt.id IS NULL)
                  )
                ORDER BY COALESCE(t."dueAt", t."updatedAt", t."createdAt") DESC
                LIMIT 20
                ''',
                opportunity_id,
            )

    pending_action_dict = dict(pending_action) if pending_action else None
    return RiskDealContext(
        opportunity=dict(opportunity),
        profile_facts=profile_facts,
        profile_relationships=profile_relationships,
        profile_narrative=(
            pending_action_dict.get("profile_narrative") if pending_action_dict else None
        ),
        pending_action=pending_action_dict,
        recent_messages=recent_messages,
        recent_notes=recent_notes,
        recent_tasks=recent_tasks,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _days_since(value: Any) -> int | None:
    timestamp = _as_datetime(value)
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max((_now() - timestamp).days, 0)


def _days_until(value: Any) -> int | None:
    timestamp = _as_datetime(value)
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (timestamp - _now()).days


def _text(value: Any) -> str:
    return str(value or "").strip()


def _contains_any(value: str, phrases: set[str]) -> bool:
    lowered = value.lower()
    return any(phrase in lowered for phrase in phrases)


def _latest_activity_days(context: RiskDealContext) -> int | None:
    candidates: list[datetime] = []
    for item in [context.opportunity, *context.recent_messages, *context.recent_notes, *context.recent_tasks]:
        for key in ("receivedAt", "dueAt", "updatedAt", "createdAt", "extracted_at"):
            timestamp = _as_datetime(item.get(key))
            if timestamp is not None:
                candidates.append(timestamp)
                break
    if not candidates:
        return None
    latest = max(
        timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
        for timestamp in candidates
    )
    return max((_now() - latest).days, 0)


def _severity_points(severity: str) -> float:
    return {"high": 0.22, "medium": 0.13, "low": 0.07}.get(severity, 0.0)


def _risk_level(score: float) -> str:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _notification_for(
    *,
    opportunity_name: str,
    risk_level: str,
    risk_score: float,
    factors: list[RiskFactor],
) -> dict[str, Any]:
    should_notify = risk_level in {"medium", "high"}
    top = factors[0] if factors else None
    title_suffix = top.description if top else "fresh risk signals need review"
    return {
        "should_notify": should_notify,
        "urgency": "high" if risk_level == "high" else "medium" if should_notify else "low",
        "title": f"Deal at risk: {title_suffix}",
        "message": (
            f"{opportunity_name or 'This deal'} is currently {risk_level} risk "
            f"(score {risk_score:.0f}/100)."
        ),
        "recommended_action": (
            "Follow up with the champion, confirm the next step, and address the "
            "highest-confidence concern."
            if should_notify
            else "Keep monitoring for new customer activity."
        ),
    }


def evaluate_risk_context(
    context: RiskDealContext,
    *,
    trigger_type: str | None = None,
) -> RiskAssessment:
    opportunity = context.opportunity
    factors: list[RiskFactor] = []
    score = 0.12

    opportunity_name = _text(opportunity.get("name"))
    updated_days = _days_since(opportunity.get("updatedAt"))
    if updated_days is None:
        factors.append(
            RiskFactor(
                factor_type="engagement_gap",
                description="No opportunity update timestamp is available.",
                severity="medium",
                evidence="updatedAt is missing",
                source="opportunity",
                confidence=0.7,
            )
        )
    elif updated_days >= 21:
        severity = "high" if updated_days >= 45 else "medium"
        factors.append(
            RiskFactor(
                factor_type="engagement_gap",
                description=f"Opportunity has not been updated for {updated_days} days.",
                severity=severity,
                evidence=f"updatedAt={opportunity.get('updatedAt')}",
                source="opportunity",
                confidence=0.85,
            )
        )

    close_days = _days_until(opportunity.get("closeDate"))
    if close_days is not None and close_days <= 14:
        severity = "high" if close_days < 0 else "medium"
        label = "overdue" if close_days < 0 else f"due in {close_days} days"
        factors.append(
            RiskFactor(
                factor_type="close_date_pressure",
                description=f"Close date is {label}.",
                severity=severity,
                evidence=f"closeDate={opportunity.get('closeDate')}",
                source="opportunity",
                confidence=0.8,
            )
        )

    stage = _text(opportunity.get("stage")).upper()
    if stage in {"PROPOSAL", "MEETING", "SCREENING"} and updated_days and updated_days >= 14:
        factors.append(
            RiskFactor(
                factor_type="deal_velocity_drop",
                description=f"Deal appears stuck in {stage}.",
                severity="medium",
                evidence=f"stage={stage}, days_since_update={updated_days}",
                source="opportunity",
                confidence=0.75,
            )
        )

    if not opportunity.get("ownerId"):
        factors.append(
            RiskFactor(
                factor_type="missing_stakeholder",
                description="No clear deal owner is assigned.",
                severity="medium",
                evidence="ownerId is missing",
                source="opportunity",
                confidence=0.8,
            )
        )
    if not opportunity.get("pointOfContactId"):
        factors.append(
            RiskFactor(
                factor_type="missing_stakeholder",
                description="No point of contact is linked to the opportunity.",
                severity="medium",
                evidence="pointOfContactId is missing",
                source="opportunity",
                confidence=0.8,
            )
        )

    for fact in context.profile_facts:
        fact_type = _text(fact.get("fact_type")).lower()
        fact_value = _text(fact.get("fact_value"))
        sentiment = _text(fact.get("sentiment")).lower()
        confidence = float(fact.get("confidence") or 0.8)
        factor_type = "unresolved_objection"
        if fact_type in {"gate", "process", "delay"}:
            factor_type = "deal_velocity_drop"
        elif fact_type == "budget":
            factor_type = "budget_concern"
        elif fact_type == "sentiment" or sentiment == "negative":
            factor_type = "sentiment_decline"

        if (
            fact_type in _RISK_INCREASING_FACT_TYPES
            or sentiment == "negative"
            or _contains_any(fact_value, _RISK_INCREASING_PHRASES)
        ):
            factors.append(
                RiskFactor(
                    factor_type=factor_type,
                    description=f"{fact_type}: {fact_value[:120]}" if fact_value else fact_type,
                    severity="high" if sentiment == "negative" or confidence >= 0.85 else "medium",
                    evidence=fact.get("source_snippet") or fact_value,
                    source="profile_facts",
                    confidence=confidence,
                )
            )
        elif fact_type == "buying_signal" or _contains_any(fact_value, _RISK_REDUCING_PHRASES):
            factors.append(
                RiskFactor(
                    factor_type="positive_momentum",
                    description=f"Positive signal: {fact_value[:120]}" if fact_value else "Positive signal",
                    severity="low",
                    evidence=fact.get("source_snippet") or fact_value,
                    source="profile_facts",
                    confidence=confidence,
                )
            )

    relationship_text = " ".join(
        f"{_text(rel.get('relationship_type'))} {_text(rel.get('description'))}"
        for rel in context.profile_relationships
    ).lower()
    if "blocks" in relationship_text or "blocker" in relationship_text:
        factors.append(
            RiskFactor(
                factor_type="stakeholder_change",
                description="Stakeholder graph includes a blocker.",
                severity="high",
                evidence=relationship_text[:200],
                source="profile_relationships",
                confidence=0.8,
            )
        )
    if "champions" not in relationship_text and context.profile_relationships:
        factors.append(
            RiskFactor(
                factor_type="missing_stakeholder",
                description="No active champion relationship is recorded.",
                severity="medium",
                evidence="profile_relationships has no champions relationship",
                source="profile_relationships",
                confidence=0.65,
            )
        )

    latest_days = _latest_activity_days(context)
    if latest_days is None or latest_days >= 14:
        factors.append(
            RiskFactor(
                factor_type="engagement_gap",
                description=(
                    "No recent activity was found."
                    if latest_days is None
                    else f"No meaningful activity in {latest_days} days."
                ),
                severity="high" if latest_days is None or latest_days >= 30 else "medium",
                evidence="messages/notes/tasks are empty or stale",
                source="activity",
                confidence=0.75,
            )
        )

    open_tasks = [
        task
        for task in context.recent_tasks
        if _text(task.get("status")).upper() not in {"DONE", "COMPLETED"}
    ]
    overdue_tasks = [
        task
        for task in open_tasks
        if (days_until := _days_until(task.get("dueAt"))) is not None and days_until < 0
    ]
    if overdue_tasks:
        factors.append(
            RiskFactor(
                factor_type="missing_next_step",
                description=f"{len(overdue_tasks)} open task(s) are overdue.",
                severity="high",
                evidence=", ".join(_text(task.get("title")) for task in overdue_tasks[:3]),
                source="tasks",
                confidence=0.85,
            )
        )
    elif not open_tasks:
        factors.append(
            RiskFactor(
                factor_type="missing_next_step",
                description="No open next-step task was found.",
                severity="medium",
                evidence="recent_tasks has no open tasks",
                source="tasks",
                confidence=0.7,
            )
        )

    risk_factors = [factor for factor in factors if factor.factor_type != "positive_momentum"]
    positive_factors = [factor for factor in factors if factor.factor_type == "positive_momentum"]
    score += sum(_severity_points(factor.severity) for factor in risk_factors)
    score -= min(len(positive_factors) * 0.08, 0.24)
    normalized_score = round(max(0.0, min(score, 1.0)), 4)
    risk_score = round(normalized_score * 100, 2)
    risk_level = _risk_level(risk_score)

    ordered = sorted(
        factors,
        key=lambda factor: (
            factor.factor_type == "positive_momentum",
            {"high": 0, "medium": 1, "low": 2}.get(factor.severity, 3),
        ),
    )
    if not ordered:
        ordered = [
            RiskFactor(
                factor_type="engagement_gap",
                description="No strong risk signals were found in current CRM evidence.",
                severity="low",
                evidence="risk context contained no active negative facts",
                source="risk_agent",
                confidence=0.6,
            )
        ]

    reasoning_summary = (
        f"Calculated from current opportunity fields, active profile facts, "
        f"profile narrative, stakeholder relationships, and recent activity. "
        f"Top signal: {ordered[0].description}"
    )

    return RiskAssessment(
        opportunity_id=str(opportunity.get("opportunity_id")),
        risk_score=risk_score,
        risk_level=risk_level,
        factors=ordered,
        previous_score=None,
        assessed_at=_now().isoformat(),
        reasoning_summary=reasoning_summary,
        recommended_notification=_notification_for(
            opportunity_name=opportunity_name,
            risk_level=risk_level,
            risk_score=risk_score,
            factors=ordered,
        ),
        metadata={
            "trigger_type": trigger_type,
            "facts_considered": len(context.profile_facts),
            "relationships_considered": len(context.profile_relationships),
            "messages_considered": len(context.recent_messages),
            "notes_considered": len(context.recent_notes),
            "tasks_considered": len(context.recent_tasks),
            "used_previous_risk_snapshot": False,
            "used_pending_action_risk_assessment": False,
        },
    )


def _recent_activity_summary(context: RiskDealContext) -> str:
    return (
        f"Recent evidence considered: {len(context.recent_messages)} message(s), "
        f"{len(context.recent_notes)} note(s), {len(context.recent_tasks)} task(s)."
    )


def _with_summary_notification(
    assessment: RiskAssessment,
    reasoning_summary: str,
) -> dict[str, Any]:
    notification = dict(assessment.recommended_notification)
    notification["message"] = reasoning_summary
    return notification


ContextBuilder = Callable[[str, str | None], Awaitable[RiskDealContext]]


class DatabaseRiskAgent:
    """Risk agent that loads its own PostgreSQL-backed ``RiskDealContext``."""

    def __init__(
        self,
        context_builder: ContextBuilder = build_risk_deal_context_from_db,
    ) -> None:
        self._context_builder = context_builder

    async def run(self, request: RiskAssessmentRequest) -> RiskAssessment:
        return await self.evaluate_deal_risk(
            opportunity_id=request.opportunity_id,
            workspace_id=request.workspace_id,
            trigger_type=request.trigger_type,
        )

    async def evaluate_deal_risk(
        self,
        opportunity_id: str,
        workspace_id: str | None = None,
        trigger_type: str | None = None,
    ) -> RiskAssessment:
        context = await self._context_builder(
            opportunity_id, workspace_id
        )
        risk_assessment = evaluate_risk_context(context, trigger_type=trigger_type)
        llm_summary = await generate_llm_reasoning_summary(
            opportunity_name=_text(context.opportunity.get("name")),
            risk_score=risk_assessment.risk_score,
            risk_level=risk_assessment.risk_level,
            risk_factors=risk_assessment.risk_factors,
            deterministic_summary=risk_assessment.reasoning_summary,
            profile_narrative=context.profile_narrative,
            recent_activity_summary=_recent_activity_summary(context),
        )
        risk_assessment.reasoning_summary = llm_summary
        risk_assessment.recommended_notification = _with_summary_notification(
            risk_assessment,
            llm_summary,
        )
        return risk_assessment


class MockRiskAgent:
    """Deterministic no-DB stand-in for tests that construct ``AgentBundle()``."""

    async def run(self, request: RiskAssessmentRequest) -> RiskAssessment:
        return await self.evaluate_deal_risk(
            opportunity_id=request.opportunity_id,
            workspace_id=request.workspace_id,
            trigger_type=request.trigger_type,
        )

    async def evaluate_deal_risk(
        self,
        opportunity_id: str,
        workspace_id: str | None = None,
        trigger_type: str | None = None,
    ) -> RiskAssessment:
        score = 20.0
        factors = [
            RiskFactor(
                factor_type="engagement_gap",
                description="Mock risk agent did not load database evidence.",
                severity="low",
                evidence="mock agent",
                source="mock",
                confidence=0.5,
            )
        ]
        return RiskAssessment(
            opportunity_id=opportunity_id,
            risk_score=score,
            risk_level=_risk_level(score),
            factors=factors,
            previous_score=None,
            assessed_at=_now().isoformat(),
            reasoning_summary="Mock risk assessment generated without database access.",
            recommended_notification=_notification_for(
                opportunity_name=opportunity_id,
                risk_level=_risk_level(score),
                risk_score=score,
                factors=factors,
            ),
            metadata={
                "workspace_id": workspace_id,
                "trigger_type": trigger_type,
                "used_previous_risk_snapshot": False,
                "used_pending_action_risk_assessment": False,
            },
        )


async def run_risk_assessment(
    request: RiskAssessmentRequest, *, agent: RiskAgent | None = None
) -> RiskAssessment:
    return await (agent or DatabaseRiskAgent()).run(request)


async def evaluate_deal_risk(
    opportunity_id: str,
    workspace_id: str | None = None,
    trigger_type: str | None = None,
) -> RiskAssessment:
    request = RiskAssessmentRequest(
        opportunity_id=opportunity_id,
        workspace_id=workspace_id,
        trigger_type=trigger_type,
    )
    return await run_risk_assessment(request)


async def mock_run_risk_assessment(request: RiskAssessmentRequest) -> RiskAssessment:
    return await MockRiskAgent().run(request)


__all__ = [
    "DATABASE_URL",
    "RiskDealContext",
    "RiskFactor",
    "RiskAssessment",
    "RiskAssessmentRequest",
    "RiskAgent",
    "DatabaseRiskAgent",
    "MockRiskAgent",
    "RISK_FACTOR_TYPES",
    "RISK_MODES",
    "RISK_LEVELS",
    "SEVERITY_LEVELS",
    "fetch_all_as_dicts",
    "resolve_workspace_schema",
    "build_risk_deal_context_from_db",
    "evaluate_risk_context",
    "evaluate_deal_risk",
    "run_risk_assessment",
    "mock_run_risk_assessment",
]
