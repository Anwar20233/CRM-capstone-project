# P1 Risk Agent Handoff

## Summary

The Risk Agent evaluates the current risk level of a CRM opportunity. P1 only passes identifiers, and the Risk Agent independently loads the evidence it needs from PostgreSQL, builds a private `RiskDealContext`, optionally asks an LLM to extract structured risk signals from messy CRM text, scores the deal with deterministic rules, and returns a structured `RiskAssessment` plus a notification recommendation.

This agent intentionally does not depend on the shared `DealContext` used by next-step, drafting, and profile flows. It also does not use mock or historical risk fields as scoring evidence.

## What The Agent Does

The Risk Agent answers one question:

```text
Given this opportunity, what is the current risk level and why?
```

It does this by:

1. Resolving the correct Twenty workspace schema.
2. Loading the opportunity and current CRM evidence from PostgreSQL.
3. Loading follow-up profile evidence extracted by the profile agent.
4. Building an internal `RiskDealContext`.
5. Using the LLM as an optional evidence extractor for recent messages, notes, tasks, and profile narrative.
6. Applying deterministic risk scoring rules to database facts plus any LLM-extracted structured signals.
7. Using the LLM as an optional summary writer after scoring.
8. Returning a `RiskAssessment` with factors, reasoning, metadata, and a recommended notification payload.

The agent is designed for P1 risk surfacing and sales-rep notification workflows. P1 remains responsible for delivery, UI state, deduplication, dismissal, and any follow-up workflow triggered by the recommendation.

## Architecture

```text
P1 / Follow-Up Orchestrator
  sends opportunity_id, workspace_id, trigger_type
        |
        v
DatabaseRiskAgent.evaluate_deal_risk(...)
        |
        v
build_risk_deal_context_from_db(...)
  resolves workspace schema from core."dataSource"
  fetches opportunity/profile/activity evidence from PostgreSQL
        |
        v
extract_llm_risk_signals(...)
  converts compact CRM text into structured profile-fact-like signals
  never calculates risk score, risk level, urgency, or notification decision
        |
        v
evaluate_risk_context(...)
  deterministically calculates score, level, factors, reasoning, notification
        |
        v
generate_llm_reasoning_summary(...)
  rewrites only reasoning_summary in sales-friendly language
        |
        v
RiskAssessment
  returned to P1 / orchestrator
```

The production bundle wires `DatabaseRiskAgent` by default:

```python
return AgentBundle(
    next_step=OrchestratorNextStepAgent(model=model),
    risk=DatabaseRiskAgent(),
    drafting=OrchestratorDraftingAgent(model=model),
)
```

Tests and mock-only flows can still use `MockRiskAgent` through `AgentBundle()` or `FOLLOWUP_USE_MOCK_AGENTS=1`.

## P1 Input Contract

P1 should call the Risk Agent with minimal identifiers only:

```python
risk_result = await risk_agent.evaluate_deal_risk(
    opportunity_id=opportunity_id,
    workspace_id=workspace_id,
    trigger_type=trigger_type,
)
```

Supported request fields:

```python
{
    "opportunity_id": "uuid string",
    "workspace_id": "uuid string | None",
    "trigger_type": "manual | scheduled | email_signal | risk_alert | opportunity_updated | other",
}
```

`workspace_id` is strongly preferred because it gives the agent a direct workspace schema lookup. If omitted, the agent can search trusted workspace schemas for the opportunity, but P1 should avoid that path when it already knows the workspace.

## What P1 Should Not Send

P1 should not send or precompute:

```text
DealContext
profile facts
profile narrative
previous risk score
risk snapshots
pending action risk_assessment
mock risk fields
```

Those fields either belong to other agents or represent stale/mock risk output. The Risk Agent owns risk context loading so the score reflects current CRM and follow-up evidence.

## Internal Context

The internal context type is `RiskDealContext`:

```python
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
```

This context is private to the Risk Agent. P1 does not build it and does not pass it across the boundary.

## Database Reads

The Risk Agent reads from these sources:

```text
core."dataSource"
workspace_schema."opportunity"
followup_agent.profile_facts
followup_agent.followup_pending_actions
followup_agent.profile_relationships
workspace_schema."message"
workspace_schema."note"
workspace_schema."noteTarget"
workspace_schema."task"
workspace_schema."taskTarget"
```

The agent fetches:

```text
opportunity:
  name, stage, closeDate, updatedAt, createdAt, amount, companyId,
  pointOfContactId, ownerId

profile_facts:
  active facts only, excluding superseded facts

followup_pending_actions:
  latest profile_narrative and pending-action context

profile_relationships:
  stakeholder graph evidence such as blockers and champions

messages:
  recent workspace messages for activity freshness

notes:
  recent notes linked to the opportunity or general notes when no target exists

tasks:
  recent tasks linked to the opportunity or general tasks when no target exists
```

The agent deliberately does not read `followup_agent.risk_snapshots`, and it does not use `followup_agent.followup_pending_actions.risk_assessment` for scoring.

## Schema Safety

Workspace schemas are resolved from trusted metadata in `core."dataSource"`. Before the schema name is interpolated into SQL, it is validated against a strict trusted-schema regex and then quoted.

This matters because table names cannot be passed as normal SQL parameters. Values such as `opportunity_id` and `workspace_id` remain parameterized.

## Hybrid LLM Use

The Risk Agent now uses the LLM in two bounded places:

```text
1. Before scoring:
   extract_llm_risk_signals(...)
   reads compact profile narrative, messages, notes, and tasks
   returns structured signals such as budget, objection, blocker, delay,
   sentiment, or buying_signal

2. After scoring:
   generate_llm_reasoning_summary(...)
   rewrites only the already-calculated reasoning_summary
```

The LLM is not allowed to calculate or override:

```text
risk_score
risk_level
risk_factors
should_notify
urgency
action_type
threshold crossing
daily sweep deduplication
```

The pre-scoring extractor returns profile-fact-like dictionaries:

```python
{
    "fact_type": "budget",
    "fact_value": "Budget owner wants below $25k while current ask is $32k",
    "sentiment": "negative",
    "confidence": 0.9,
    "source_type": "llm_risk_signal",
    "source_snippet": "budget owner wants below $25k",
}
```

Those signals are appended to `RiskDealContext.profile_facts`, then the normal deterministic `evaluate_risk_context(...)` scoring logic maps them into factors such as `budget_concern`, `unresolved_objection`, `deal_velocity_drop`, `sentiment_decline`, or `positive_momentum`.

If either LLM call fails, times out, returns invalid JSON, returns an empty response, or is not configured, the Risk Agent falls back safely:

```text
signal extraction failure -> continue with original database context
summary generation failure -> keep deterministic reasoning_summary
```

## Scoring Logic

`evaluate_risk_context(...)` starts with a low base score and adds or subtracts points based on evidence. It is the only place where risk score and risk level are calculated.

Evidence can come from stored database facts or from the LLM signal extractor. LLM-extracted signals are treated as additional profile facts, so the same deterministic scoring rules still decide how much each signal changes the score.

Risk-increasing signals include:

```text
engagement_gap:
  missing or stale opportunity updates, no recent activity

close_date_pressure:
  close date due soon or already overdue

deal_velocity_drop:
  deal stuck in proposal/meeting/screening stages or process delay facts

missing_stakeholder:
  no owner, no point of contact, or no champion relationship

unresolved_objection:
  active profile facts indicating concern, objection, blocker, risk, delay, churn,
  legal, security, procurement, pricing, or budget concerns

sentiment_decline:
  negative sentiment facts

budget_concern:
  budget-related risk facts

stakeholder_change:
  profile relationships showing a blocker

missing_next_step:
  no open next-step task or overdue open tasks
```

Risk-reducing signals include:

```text
positive_momentum:
  buying signals or phrases like approved, aligned, champion, next step,
  scheduled, committed, signed, or interested
```

The score is clamped to `0.0..1.0`, and the level is derived from the final score:

```text
low:    below medium threshold
medium: notify-worthy risk
high:   highest urgency risk
```

Each factor carries evidence, source, severity, and confidence so P1 can show the reason behind the notification. When a factor originated from pre-scoring LLM extraction, its source remains `profile_facts`, and the underlying fact has `source_type = llm_risk_signal`.

## Output Contract

The result is a `RiskAssessment` dataclass:

```python
{
    "opportunity_id": "...",
    "risk_score": 0.0,
    "risk_level": "low | medium | high",
    "factors": [
        {
            "factor_type": "engagement_gap",
            "description": "Opportunity has not been updated for 30 days.",
            "severity": "low | medium | high",
            "evidence": "updatedAt=...",
            "source": "opportunity | profile_facts | profile_relationships | activity | tasks | risk_agent | mock",
            "confidence": 0.85,
        }
    ],
    "previous_score": None,
    "assessed_at": "ISO timestamp",
    "reasoning_summary": "...",
    "recommended_notification": {
        "should_notify": True,
        "urgency": "low | medium | high",
        "title": "Deal at risk: ...",
        "message": "... is currently high risk (score 0.82).",
        "recommended_action": "Follow up with the champion, confirm the next step, and address the highest-confidence concern.",
    },
    "metadata": {
        "trigger_type": "...",
        "facts_considered": 0,
        "relationships_considered": 0,
        "messages_considered": 0,
        "notes_considered": 0,
        "tasks_considered": 0,
        "used_previous_risk_snapshot": False,
        "used_pending_action_risk_assessment": False,
    },
}
```

Implementation note: the dataclass stores factors in `factors`. `risk_factors` is a Python property alias for P1-facing consumers, and `RiskFactor.factor` is a property alias for `factor_type`. These aliases are available in Python code but are not emitted by `dataclasses.asdict(...)`.

`reasoning_summary` may be LLM-generated, but it is produced after scoring. P1 can display it as the richer sales-facing explanation while still relying on the deterministic `risk_score`, `risk_level`, and `factors` for product logic.

## Notification Contract

The agent returns `recommended_notification` but does not send it.

P1 should treat it as a recommendation:

```python
if risk_result.recommended_notification["should_notify"]:
    await notify_sales_rep(
        opportunity_id=risk_result.opportunity_id,
        payload=risk_result.recommended_notification,
        factors=risk_result.risk_factors,
    )
```

Notification policy:

```text
low:
  should_notify = False
  urgency = low
  recommended action = keep monitoring

medium:
  should_notify = True
  urgency = medium
  recommended action = follow up and address top concern

high:
  should_notify = True
  urgency = high
  recommended action = follow up and address top concern
```

P1 owns:

```text
UI rendering
notification delivery
deduplication
dismissal and snooze state
linking to opportunity detail pages
follow-up workflow after rep action
analytics / audit logging
```

## Orchestrator Flow

The follow-up orchestrator calls the Risk Agent with the same minimal contract:

```python
risk = await deps.agents.risk.evaluate_deal_risk(
    opportunity_id=opportunity_id,
    workspace_id=workspace_id,
    trigger_type=trigger_type,
)
```

The orchestrator stores the returned `RiskAssessment` in state and uses it when building reasoning and risk-sweep plans. The orchestrator may update the in-memory deal context risk score when a deal context exists, but that is not an input to the Risk Agent.

## Daily Risk Sweep

Status: implemented in `followup.risk.daily_sweep`.

The daily sweep is a standalone backend job that runs without P1 needing to trigger risk scoring manually. It wraps the existing `DatabaseRiskAgent` and uses persisted daily score history to decide when an opportunity newly needs sales-rep attention.

### Goal

The daily sweep should run early in the morning before the sales team's day starts and identify all active opportunities that need rep attention.

Implemented behavior:

```text
daily scheduler starts
  -> discover registered workspace schemas
  -> discover active opportunities in each workspace
  -> run DatabaseRiskAgent.evaluate_deal_risk(...) for each opportunity
  -> compare the new score/level with the last stored daily score
  -> detect threshold crossings into medium/high risk
  -> create a risk_alert pending action when needed
  -> persist the latest risk score for the next sweep
```

This job should not depend on P1, inbound email events, or manual orchestrator calls.

### Proposed Schedule

Recommended local/business schedule:

```text
06:00 Asia/Riyadh every day
```

Example cron expression:

```text
0 6 * * *
```

The exact scheduler can be cron, Docker cron, a deployment-provider scheduled job, or a backend worker scheduler. The important part is that the job calls the daily sweep runner directly, not the P1 UI flow.

### Command

Run the standalone sweep from `packages/twenty-ai-service`:

```bash
PYTHONPATH=. .venv/bin/python -m followup.risk.daily_sweep
```

Recommended local env:

```bash
export PG_DATABASE_URL="postgres://postgres:postgres@localhost:5432/default"
export HF_HOME="$PWD/.cache/huggingface"
mkdir -p "$HF_HOME"
```

For local/dev databases where the workspace schema exists but is not registered in `core."dataSource"`, use:

```bash
export FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE="workspace_c4en9trdpordobem3offy83aa"
export FOLLOWUP_RISK_WORKSPACE_ID_OVERRIDE="00000000-0000-0000-0000-000000000000"
```

`FOLLOWUP_RISK_WORKSPACE_ID_OVERRIDE` is optional locally. When omitted, the sweep uses the nil UUID as a dev-only placeholder because `followup_pending_actions.workspace_id` is required. Production should prefer `core."dataSource"` workspace discovery instead of either override.

### Active Deal Discovery

The daily sweep considers active opportunities only.

Recommended filters:

```text
opportunity."deletedAt" IS NULL
stage is not a closed/won/customer terminal stage
workspace schema comes from core."dataSource" or the local schema override
```

Current terminal stages skipped by the sweep:

```text
CUSTOMER
CLOSED
CLOSED_WON
CLOSED_LOST
WON
LOST
```

### Threshold Crossing Logic

The sweep persists score history in `followup_agent.risk_daily_scores`. This table is used only for threshold-crossing detection. It is not used as evidence when calculating the current risk score.

Implemented threshold policy:

```text
low -> medium:
  create medium urgency risk_alert

low -> high:
  create high urgency risk_alert

medium -> high:
  create high urgency risk_alert

medium -> medium:
  do not create duplicate alert

high -> high:
  do not create duplicate alert

high/medium -> low:
  persist the lower score, no alert

existing pending risk_alert:
  do not create duplicate alert
```

The score-level boundaries come from `evaluate_risk_context(...)`, so P1 and the sweep use the same `low`, `medium`, and `high` semantics.

### Persistence

The daily sweep persists two things:

```text
risk_daily_scores row:
  opportunity_id
  workspace_id
  risk_score
  risk_level
  top_factors
  assessment
  assessed_at
  trigger_type = daily_sweep
  created_pending_action_id

risk alert / pending action when needed:
  opportunity_id
  workspace_id
  trigger_type = risk_alert
  action_type = follow_up_call | escalate
  action_payload = recommended_notification + risk factors
  risk_assessment = full RiskAssessment
  reasoning = risk_assessment.reasoning_summary
  urgency = recommended_notification.urgency
  expires_at = based on urgency
```

Important: persisted previous scores are for threshold comparison only. They should not be used as evidence when calculating the current risk score.

### Notification Creation

For each opportunity, the sweep creates a pending action only when this condition is true:

```text
risk_result.recommended_notification.should_notify is true
AND no pending risk_alert already exists
AND (
  there is no previous score and the current level is medium/high
  OR the opportunity crossed upward into medium/high risk
)
```

The pending action payload should include:

```text
risk_score
risk_level
top risk factors
reasoning_summary
recommended_notification
evidence counts from metadata
```

P1 can then render or deliver the alert without recomputing risk.

### Test Coverage

Covered by `tests/test_daily_risk_sweep.py` and `tests/test_followup_repositories.py`:

```text
discovers active opportunities
skips deleted or terminal opportunities
calls DatabaseRiskAgent with minimal identifiers
persists latest daily score
creates alert on low -> medium
creates alert on medium -> high
does not duplicate alert on unchanged medium/high
does not duplicate when a pending risk_alert already exists
works without a P1 trigger
supports local schema override for dev testing
LLM signal extraction can enrich profile facts before scoring
LLM signal extraction failure does not crash scoring
LLM reasoning summary can replace only reasoning_summary
LLM reasoning summary failure falls back to deterministic reasoning
```

## Error Behavior

Expected hard failures:

```text
workspace schema not found
unsafe workspace schema name
opportunity not found
database connection failure
```

The orchestrator node wrapper catches node-level failures and marks the run failed rather than crashing the whole process. P1 should still handle failed risk calls gracefully and avoid showing stale risk as fresh output.

The standalone daily sweep catches per-opportunity failures and includes the error in its JSON summary, so one bad opportunity does not stop the whole sweep. Startup-level failures such as database connection errors still fail the job.

## Mock Agent

`MockRiskAgent` exists for tests and mock bundles. It does not access the database and returns a deterministic low-risk assessment with metadata showing it was generated by the mock path.

Use mock mode only for tests or local flows that explicitly do not want database-backed evidence:

```bash
FOLLOWUP_USE_MOCK_AGENTS=1
```

## P1 Integration Checklist

Use this checklist when wiring the P1 UI/backend to the Risk Agent:

```text
[ ] Call evaluate_deal_risk with opportunity_id, workspace_id, trigger_type only.
[ ] Do not pass DealContext or preloaded profile data.
[ ] Do not use risk_snapshots or pending_action.risk_assessment as fresh evidence.
[ ] Configure LLM_PROVIDER, LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL if using LLM signal extraction or LLM summaries.
[ ] Treat missing or failing LLM config as degraded explanation/enrichment, not as risk-score failure.
[ ] Show risk_level, risk_score, reasoning_summary, and top risk factors.
[ ] Use recommended_notification.should_notify to decide whether to notify.
[ ] Keep delivery, dedupe, dismissal, and analytics in P1.
[ ] Preserve factors/evidence in UI or audit logs so reps can understand why the deal was flagged.
[ ] Treat errors as unavailable fresh risk, not as low risk.
[ ] Configure a production scheduler for `python -m followup.risk.daily_sweep`.
[ ] Monitor `followup_agent.risk_daily_scores` for sweep history.
[ ] Confirm P1 renders `risk_alert` pending actions created by the sweep.
```

## Backend Testing Guide

Use this guide to test the database-backed Risk Agent locally from the backend.

### 1. Start From The AI Service Directory

If you are already in `packages/twenty-ai-service`, do not run `cd packages/twenty-ai-service` again.

```bash
cd /Users/ranaalshehri/temp-repos/CRM-capstone-project/packages/twenty-ai-service
```

### 2. Set Local Environment Variables

The local `.env` may not define `PG_DATABASE_URL`, so set it explicitly.

```bash
unset TRANSFORMERS_CACHE
export PG_DATABASE_URL="postgres://postgres:postgres@localhost:5432/default"
export FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE="workspace_c4en9trdpordobem3offy83aa"
export HF_HOME="$PWD/.cache/huggingface"
mkdir -p "$HF_HOME"
```

`FOLLOWUP_RISK_WORKSPACE_SCHEMA_OVERRIDE` is only for local/dev testing when the workspace schema exists but is not registered in `core."dataSource"`. The Risk Agent still validates the schema name and checks that the opportunity exists in that schema before using it.

### 3. List Available Opportunities

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
import asyncio
import asyncpg
import os

SCHEMA = "workspace_c4en9trdpordobem3offy83aa"

async def main():
    conn = await asyncpg.connect(os.environ["PG_DATABASE_URL"])
    rows = await conn.fetch(
        f'''
        SELECT id, name, stage::text AS stage, "closeDate", "updatedAt"
        FROM "{SCHEMA}"."opportunity"
        WHERE "deletedAt" IS NULL
        ORDER BY "updatedAt" DESC
        LIMIT 20
        '''
    )
    await conn.close()

    for row in rows:
        print(
            row["id"],
            "|",
            row["name"],
            "|",
            row["stage"],
            "| close:",
            row["closeDate"],
            "| updated:",
            row["updatedAt"],
        )

asyncio.run(main())
PY
```

### 4. Run One Risk Assessment

Replace `OPPORTUNITY_ID` with any ID from the list above.

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
import asyncio
import json
from dataclasses import asdict

from followup.contracts.risk import evaluate_deal_risk

OPPORTUNITY_ID = "9543adcf-ec03-44e2-9233-3c2d3ebae98a"

async def main():
    result = await evaluate_deal_risk(
        opportunity_id=OPPORTUNITY_ID,
        trigger_type="manual",
    )
    print(json.dumps(asdict(result), indent=2, default=str))

asyncio.run(main())
PY
```

Expected fields in the output:

```text
risk_score
risk_level
factors
reasoning_summary
recommended_notification
metadata.facts_considered
metadata.relationships_considered
metadata.messages_considered
metadata.notes_considered
metadata.tasks_considered
```

### 5. Run All Listed Opportunities As Test Cases

```bash
PYTHONPATH=. .venv/bin/python - <<'PY'
import asyncio
import asyncpg
import os

from followup.contracts.risk import evaluate_deal_risk

SCHEMA = "workspace_c4en9trdpordobem3offy83aa"

async def load_opportunities():
    conn = await asyncpg.connect(os.environ["PG_DATABASE_URL"])
    try:
        return await conn.fetch(
            f'''
            SELECT id, name, stage::text AS stage, "closeDate", "updatedAt"
            FROM "{SCHEMA}"."opportunity"
            WHERE "deletedAt" IS NULL
            ORDER BY "updatedAt" DESC
            LIMIT 20
            '''
        )
    finally:
        await conn.close()

async def main():
    opportunities = await load_opportunities()
    print(f"Testing {len(opportunities)} opportunities from {SCHEMA}\n")

    for index, opportunity in enumerate(opportunities, start=1):
        result = await evaluate_deal_risk(
            opportunity_id=str(opportunity["id"]),
            trigger_type="manual_test_case",
        )
        notification = result.recommended_notification

        print(f"{index}. {opportunity['name']}")
        print(f"   opportunity_id: {opportunity['id']}")
        print(
            f"   stage: {opportunity['stage']} | "
            f"close: {opportunity['closeDate']} | "
            f"updated: {opportunity['updatedAt']}"
        )
        print(
            f"   risk: {result.risk_level} ({result.risk_score:.2f}) | "
            f"notify: {notification['should_notify']} | "
            f"urgency: {notification['urgency']}"
        )
        for factor in result.factors[:3]:
            print(
                f"   - {factor.severity}: "
                f"{factor.factor_type} — {factor.description}"
            )
        print(
            "   evidence counts: "
            f"facts={result.metadata['facts_considered']}, "
            f"relationships={result.metadata['relationships_considered']}, "
            f"messages={result.metadata['messages_considered']}, "
            f"notes={result.metadata['notes_considered']}, "
            f"tasks={result.metadata['tasks_considered']}"
        )
        print()

asyncio.run(main())
PY
```

## Local Test Case Results

These results were produced against:

```text
schema: workspace_c4en9trdpordobem3offy83aa
database: postgres://postgres:postgres@localhost:5432/default
trigger_type: manual_test_case
```

### Low Risk Cases

```text
Enterprise Plan Upgrade
  score: 0.34
  notify: false
  top signal: close_date_pressure — Close date is overdue.
  evidence: facts=0, relationships=0, messages=20, notes=5, tasks=5

New Expansion Deal
  score: 0.38
  notify: false
  top signals:
    missing_stakeholder — No clear deal owner is assigned.
    missing_stakeholder — No point of contact is linked to the opportunity.

Test Corp — New Client
  score: 0.38
  notify: false
  top signals:
    missing_stakeholder — No clear deal owner is assigned.
    missing_stakeholder — No point of contact is linked to the opportunity.
```

Several newer or test-created opportunities stayed in the `low` range because they had only missing-stakeholder signals or a single overdue close-date signal, with no active profile facts or stakeholder relationship risks.

### Medium Risk Cases

```text
Platform Migration
  opportunity_id: 822639e5-9bf7-40f1-8882-a11140362339
  score: 0.47
  notify: true
  top signals:
    close_date_pressure — Close date is overdue.
    deal_velocity_drop — Deal appears stuck in PROPOSAL.

Workspace Expansion
  opportunity_id: 75de302f-1044-4957-8da4-1f67ebefd52b
  score: 0.47
  notify: true
  top signals:
    close_date_pressure — Close date is overdue.
    deal_velocity_drop — Deal appears stuck in MEETING.

API Integration Deal
  opportunity_id: 2beb07b0-340c-41d7-be33-5aa91757f329
  score: 0.47
  notify: true
  top signals:
    close_date_pressure — Close date is overdue.
    deal_velocity_drop — Deal appears stuck in SCREENING.

Figma — Design Collaboration Platform
  opportunity_id: 0cacee5d-89d4-577e-8298-2d2bfec138ea
  score: 0.69
  notify: true
  top signals:
    engagement_gap — Opportunity has not been updated for 47 days.
    missing_next_step — 1 open task(s) are overdue.
    deal_velocity_drop — Deal appears stuck in PROPOSAL.
```

### High Risk Cases

```text
Airbnb — Platform Integration
  opportunity_id: a45a119e-376d-5eb2-8b96-33c2631660ea
  score: 1.00
  notify: true
  top signals:
    engagement_gap — Opportunity has not been updated for 47 days.
    sentiment_decline — concern: Q3 integration timeline is tight due to engineering freeze in August
    deal_velocity_drop — Deal appears stuck in PROPOSAL.
  evidence: facts=14, relationships=16, messages=20, notes=7, tasks=5

Stripe — Analytics Suite
  opportunity_id: b278fc35-b5e0-5846-a9a4-7b4618952be7
  score: 0.79
  notify: true
  top signals:
    engagement_gap — Opportunity has not been updated for 47 days.
    deal_velocity_drop — gate: Owns the security review — the last gate before commit; open item is encryption-at-rest
    deal_velocity_drop — Deal appears stuck in SCREENING.
  evidence: facts=5, relationships=2, messages=20, notes=6, tasks=5

Notion — Workflow Automation
  opportunity_id: 3a24c9cd-dcbc-5b9f-8456-d1de97de951d
  score: 0.87
  notify: true
  top signals:
    engagement_gap — Opportunity has not been updated for 47 days.
    unresolved_objection — Price objection: budget holder wants below $25k; current ask is $32k
    deal_velocity_drop — Deal appears stuck in MEETING.
  evidence: facts=5, relationships=1, messages=20, notes=6, tasks=5
```

These are the best local cases for validating P1 notification behavior because they produce `recommended_notification.should_notify = true` and include strong factor evidence from profile facts, relationship data, stale opportunity updates, or overdue tasks.

## Real Database Verification

These checks were run against the local Postgres database:

```text
database: postgres://postgres:postgres@localhost:5432/default
schema override: workspace_c4en9trdpordobem3offy83aa
workspace override: 00000000-0000-0000-0000-000000000000
```

### Single Risk Agent Run

Opportunity tested:

```text
name: Airbnb — Platform Integration
opportunity_id: a45a119e-376d-5eb2-8b96-33c2631660ea
```

Result:

```text
risk_score: 1.0
risk_level: high
should_notify: true
urgency: high
facts_considered: 14
relationships_considered: 16
messages_considered: 20
notes_considered: 7
tasks_considered: 5
```

Top signals:

```text
engagement_gap:
  Opportunity has not been updated for 47 days.

sentiment_decline:
  Q3 integration timeline is tight due to engineering freeze in August.

deal_velocity_drop:
  Deal appears stuck in PROPOSAL.
```

### Daily Sweep Run

First daily sweep result:

```text
scanned: 19
scored: 19
alerts_created: 8
skipped: 11
failed: 0
```

Database writes after the first sweep:

```text
risk_daily_scores total: 19
risk_alert pending_actions total: 8
```

Second daily sweep result:

```text
scanned: 19
scored: 19
alerts_created: 0
skipped: 19
failed: 0
```

The second run confirms deduping works: existing pending `risk_alert` actions prevented duplicate alerts.

Final database state after two sweeps:

```text
risk_daily_scores total: 38
risk_alert pending_actions total: 8
```

## Verification

Latest verification after Risk Agent and daily sweep implementation:

```text
python compile:
  py_compile passed for 184 Python files under packages/twenty-ai-service

focused previously failing tests:
  44 passed, 43 warnings

risk focused tests:
  36 passed in 1.46s

daily sweep focused tests:
  54 passed in 1.45s

full service test suite:
  346 passed, 93 warnings in 10.54s

command:
  /Users/ranaalshehri/temp-repos/CRM-capstone-project/packages/twenty-ai-service/.venv/bin/python -m pytest tests -v
```

Warnings are dependency/deprecation warnings and are not test failures.

Latest focused verification after hybrid LLM signal extraction and reasoning-summary additions:

```text
risk agent focused tests:
  packages/twenty-ai-service/tests/test_risk_agent.py
  11 passed in 0.38s

python compile:
  py_compile passed for changed Risk Agent files
```
