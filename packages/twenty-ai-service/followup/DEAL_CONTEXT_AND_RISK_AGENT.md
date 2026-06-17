# Deal Context & Risk Agent — Team Guide

This document is the **team source of truth** for using `DealContext` and the Risk Scoring & Notification Agent in `twenty-ai-service`. Use it when building follow-up agents, wiring the orchestrator, or debugging risk evaluation locally.

**Deeper internals:**

- Loader pipeline details: [DEAL_CONTEXT_LOADER.md](./DEAL_CONTEXT_LOADER.md)
- Risk agent file reference: [agents/risk/README.md](./agents/risk/README.md)
- Full system spec: [FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md](../FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md)
- P1 orchestrator wiring: [P1_INTEGRATION.md](./P1_INTEGRATION.md)

---

## What changed (v2 summary)

| Area | Before | Now |
|------|--------|-----|
| Context loading | JSON fixtures only in tests | Live CRM via `load_deal_context()` |
| Data confidence | Empty lists ambiguous | `context_completeness` per section |
| Stages | Mixed labels | Canonical `SCREAMING_SNAKE_CASE` (`PROPOSAL`, `CLOSED_WON`, …) |
| Timeline | `updated_at` sometimes used as activity date | Undated scalar fields (`emailText`/`notes`) with `occurred_at=null` |
| Risk rules | 7 rules | **8 rules** (+ `past_expected_close_date`) |
| Rule output | Score only | Structured `triggered` / `not_triggered` / `skipped` explanations |
| Notifications | Returned only | Persistence boundary via `NotificationRepository` |
| HTTP | Context endpoint only | `POST /followup/risk/{id}/evaluate` |
| Daily sweep | Basic iteration | Failure isolation, snapshots, authenticated user identity |

```text
agent-bridge → crm_fetch → llm_extract | map_crm → enrich → DealContext
                                                              │
                                                              ▼
                                                    rules.py (8 rules)
                                                              │
                                                              ▼
                                                    agent.py → notifications.py
                                                              │
                                                              ▼
                                              RiskNotificationAgentResult
                                                              │
                                    ┌─────────────────────────┴─────────────────────────┐
                                    ▼                                                   ▼
                          NotificationRepository                              RiskSnapshotStore
                          (outside pure agent)                                (daily sweep)
```

---

## DealContext schema

`DealContext` is the shared input contract for risk, next-step, and drafting agents. Defined in `followup/context/schemas.py`.

### Top-level fields

| Field | Type | Description |
|-------|------|-------------|
| `opportunity` | `OpportunitySnapshot` | Core deal record |
| `company` | `CompanySnapshot \| null` | Linked company |
| `contacts` | `ContactSnapshot[]` | People on the deal |
| `timeline` | `TimelineItem[]` | Emails, notes, activities |
| `tasks` | `TaskSnapshot[]` | Open/overdue tasks |
| `meetings` | `MeetingSnapshot[]` | Scheduled meetings |
| `pipeline_meta` | `PipelineMeta` | Stage list + SLA days |
| `engagement` | `EngagementMetrics` | Computed recency metrics |
| `context_completeness` | `ContextCompleteness \| null` | Per-section load status (v2) |
| `context_provenance` | `"profile_primary" \| "crm_fallback" \| "hybrid"` | How context was built |
| `loaded_at` | `datetime \| null` | When context was assembled |

### OpportunitySnapshot

| Field | Notes |
|-------|-------|
| `stage` | **Canonical** `SCREAMING_SNAKE_CASE` — e.g. `PROPOSAL`, `CLOSED_WON`. Normalized by `stage_normalization.py`. |
| `stage_entered_at` | Only from CRM `stageEnteredAt`. **Never** copied from `updated_at`. When `null`, stalled-stage rule is skipped. |
| `close_date` | Expected close date; used by `past_expected_close_date` rule |
| `owner_id` | Opportunity owner — **not** the authenticated bridge user |

### TimelineItem

| Field | Notes |
|-------|-------|
| `occurred_at` | `null` when no trustworthy date exists |
| `source` | e.g. `opportunity.emailText`, `opportunity.notes` |
| `timestamp_source` | `"unavailable"` for undated scalar fields |

Undated timeline entries are included for content (proposal evidence, objections) but **excluded** from engagement recency calculations.

### EngagementMetrics

| Field | Notes |
|-------|-------|
| `days_since_last_activity` | `int \| null` — `null` when no dated activity exists |
| `activity_count_14d` | Count of dated timeline items in last 14 days |
| `activity_count_prior_14d` | Count in the 14 days before that (for engagement drop) |
| `has_future_meeting` | `true` when a future meeting is recorded |

### PipelineMeta

| Field | Notes |
|-------|-------|
| `stages` | Ordered pipeline stages from CRM metadata or fallback defaults |
| `stage_sla_days` | Per-stage SLA for stalled-stage rule |
| `source` | `"crm_metadata"` (live) or `"fallback_defaults"` |

### ContextCompleteness (v2)

Each section has a `ContextSectionStatus`:

```json
{
  "status": "loaded | partial | unavailable | not_requested",
  "reason": "Human-readable explanation when not fully loaded",
  "source": "agent_bridge | crm_metadata | opportunity_scalar_fields | ..."
}
```

Sections: `opportunity`, `company`, `contacts`, `timeline`, `tasks`, `meetings`, `pipeline_metadata`.

**Critical semantics:**

| Situation | Meaning |
|-----------|---------|
| `tasks: []` + `tasks.status = "loaded"` | Queried successfully; no tasks exist |
| `tasks: []` + `tasks.status = "unavailable"` | Could not query — **do not infer** absence |
| `timeline.status = "partial"` | Scalar `emailText`/`notes` loaded; linked CRM activity records unavailable |
| `context_completeness = null` | Legacy fixture — rules treat sections as `loaded` |

### Example (Platform Migration)

```json
{
  "opportunity": {
    "id": "822639e5-9bf7-40f1-8882-a11140362339",
    "name": "Platform Migration",
    "stage": "PROPOSAL",
    "amount": 60000.0,
    "close_date": "2026-01-31T16:25:00.000Z",
    "stage_entered_at": null
  },
  "timeline": [
    {
      "type": "email",
      "title": "Follow-up on Platform Migration Proposal",
      "summary": "The customer is concerned about data privacy...",
      "occurred_at": null,
      "source": "opportunity.emailText",
      "timestamp_source": "unavailable"
    }
  ],
  "tasks": [],
  "meetings": [],
  "engagement": {
    "days_since_last_activity": null,
    "activity_count_14d": 0,
    "activity_count_prior_14d": 0,
    "has_future_meeting": false
  },
  "context_completeness": {
    "opportunity": { "status": "loaded", "source": "agent_bridge" },
    "company": { "status": "loaded", "source": "agent_bridge" },
    "contacts": { "status": "loaded", "source": "agent_bridge" },
    "timeline": {
      "status": "partial",
      "reason": "Opportunity scalar emailText/notes loaded, but linked CRM activity records were unavailable.",
      "source": "opportunity_scalar_fields"
    },
    "tasks": {
      "status": "unavailable",
      "reason": "Reader tool does not expose the required opportunity relationship filter.",
      "source": "agent_bridge"
    },
    "meetings": {
      "status": "unavailable",
      "reason": "No supported meeting query is currently configured.",
      "source": "agent_bridge"
    },
    "pipeline_metadata": { "status": "loaded", "source": "crm_metadata" }
  }
}
```

Full fixture: `followup/tests/fixtures/risk_context_platform_migration.json`

---

## Loading DealContext

### Python (primary)

```python
from followup.context import load_deal_context

context = await load_deal_context(
    opportunity_id="822639e5-9bf7-40f1-8882-a11140362339",
    workspace_id="your-workspace-uuid",
    user_id="authenticated-user-uuid",
    role_id=None,       # optional; falls back to TWENTY_READER_ROLE_ID / TWENTY_ROLE_ID
    use_llm=True,       # False = deterministic map_crm only
)
```

**Pipeline:**

1. `fetch_opportunity_bundle()` — parallel agent-bridge reads
2. `extract_deal_context()` — LLM normalization (or skip when `use_llm=False`)
3. `map_deal_context_fallback()` — deterministic fallback on LLM failure
4. `enrich_context()` — engagement metrics, `loaded_at`, provenance, completeness

### HTTP endpoints

**Inspect context:**

```http
GET /followup/context/{opportunity_id}?workspace_id=...&user_id=...&use_llm=false
```

**Evaluate risk:**

```http
POST /followup/risk/{opportunity_id}/evaluate?workspace_id=...&user_id=...
```

| Query param | Default | Description |
|-------------|---------|-------------|
| `workspace_id` | required | Twenty workspace UUID |
| `user_id` | required | Authenticated user UUID |
| `role_id` | env fallback | Bridge role UUID |
| `use_llm_context` | `false` | LLM context normalization |
| `use_llm_copy` | `false` | LLM notification prose |
| `persist_notifications` | `false` | Save to in-memory dev repository |

**Example curl:**

```bash
curl -X POST "http://localhost:8001/followup/risk/OPP_ID/evaluate\
?workspace_id=WORKSPACE_ID\
&user_id=USER_ID\
&use_llm_context=false\
&use_llm_copy=false\
&persist_notifications=false"
```

**Error mapping:**

| Code | HTTP |
|------|------|
| `OPPORTUNITY_NOT_FOUND` | 404 |
| `BRIDGE_UNREACHABLE` | 503 |
| `MAP_FAILED` | 500 |
| `INVALID_IDENTITY` | 400 |

### Environment variables

```bash
TWENTY_WORKSPACE_ID=...
TWENTY_USER_ID=...              # authenticated user for bridge calls
TWENTY_READER_ROLE_ID=...       # or TWENTY_ROLE_ID
NODE_BRIDGE_BASE_URL=http://localhost:3000/agent-bridge

# Optional for LLM context normalization
LLM_API_KEY=...
LLM_MODEL=...
```

### Dev scripts

```bash
cd packages/twenty-ai-service

# Load and print context
python scripts/try_load_deal_context.py OPPORTUNITY_ID

# Single-deal risk evaluation (shows triggered / not-triggered / skipped / completeness)
python scripts/try_run_risk_agent.py OPPORTUNITY_ID --no-llm
python scripts/try_run_risk_agent.py --fixture risk_context_platform_migration.json --no-llm

# Daily sweep over active opportunities
python scripts/try_run_risk_sweep.py --no-llm --limit 3
python scripts/try_run_risk_sweep.py --no-llm --json
```

---

## Context completeness guide for agent authors

When building agents that consume `DealContext`:

1. **Check `context_completeness` before inferring absence.** An empty `meetings` list with `status=unavailable` does not mean "no meetings scheduled."
2. **Use engagement metrics for recency, not raw timeline length.** `days_since_last_activity=null` means no trustworthy dated activity — not "zero days."
3. **Proposal evidence** can come from undated timeline text (`emailText`/`notes`) even when timeline status is `partial`.
4. **Stage rules** depend on canonical stages and `stage_entered_at` — do not substitute `updated_at`.

Live fetch sets completeness in `crm_fetch.py` via `build_bridge_fetch_completeness()`. Current bridge limitations (as of v2):

- Tasks: unavailable (no opportunity relationship filter on reader tool)
- Meetings: unavailable (no supported meeting query configured)
- Timeline: partial when scalar fields load but linked activity records do not

---

## Risk Agent

### Design principle

The Risk Agent is a **pure function of `DealContext`**. It does not call CRM, the bridge, or the database. Context loading and notification persistence happen outside the agent via integration helpers.

### End-to-end flow

```text
DealContext + FollowUpEvent + existing_notifications[]
        │
        ▼
  compute_risk_score()          ← 8 deterministic rules (rules.py)
        │
        ▼
  evaluate_all_rules()          ← per-rule explanations (evaluation.py)
        │
        ▼
  optional deal_risk_report       ← read-only CRM enrichment (non-blocking)
        │
        ▼
  should_notify()               ← 0–2 notification drafts
        │
        ▼
  generate_notification_copy()    ← LLM or template fallback
        │
        ▼
  apply_notification_lifecycle() ← dedupe + dismiss suppression
        │
        ▼
  RiskNotificationAgentResult { risk_score, notifications[], reasoning_summary }
```

### 8 risk rules

| Rule ID | Points | Severity | Trigger summary | Completeness behavior |
|---------|--------|----------|-----------------|----------------------|
| `no_activity_7d` | 25 | high | No trustworthy dated activity ≥ 7 days | Skips if timeline `unavailable`; may trigger on `partial` |
| `no_future_meeting` | 20 | medium | No future meeting (non-closed stages) | **Skips** if meetings `unavailable` |
| `stalled_stage` | 20 | medium | Days in stage > SLA | **Skips** if `stage_entered_at` is null |
| `missing_decision_maker` | 15 | medium | Past MEETING stage, no decision-maker contact | Normal evaluation |
| `missing_proposal` | 20 | high | Stage ≥ PROPOSAL, no proposal evidence in timeline | Normal evaluation |
| `overdue_tasks` | 15 | medium | Any overdue task | **Skips** if tasks `unavailable` |
| `engagement_drop` | 10 | low | Activity down >50% vs prior 14 days | Requires prior baseline |
| `past_expected_close_date` | 20 | high | Expected close date has passed | Normal evaluation |

**Score levels:**

| Level | Range |
|-------|-------|
| LOW | 0–39 |
| MEDIUM | 40–69 |
| HIGH | 70+ |

Score is capped at 100.

### Rule evaluations

Each rule produces a `RuleEvaluation`:

```python
class RuleEvaluation:
    rule_id: str
    status: "triggered" | "not_triggered" | "skipped"
    reason: str
    factor: RiskFactor | None  # present when triggered
```

Example explanations:

```text
stalled_stage:          Skipped because stage_entered_at is unavailable.
missing_proposal:       Not triggered because proposal evidence was found in opportunity content.
overdue_tasks:          Skipped because task data could not be verified from the available reader tools.
no_future_meeting:      Skipped because meeting data could not be verified from the available reader tools.
```

Use `evaluate_all_rules(context, now=...)` for debugging without running the full agent.

### Notifications

**When notifications fire:**

- Score ≥ 40 **or** any high-severity factor
- Max **2** notifications per run (priority-ordered)
- Closed stages (`CLOSED_WON`, `CLOSED_LOST`): score computed, **no notifications**

**Lifecycle:**

| Scenario | Behavior |
|----------|----------|
| First run | Creates new notifications |
| Identical re-run | No duplicates (dedupe by `opportunity_id` + `rule_id`) |
| Dismissed within 7 days | That `rule_id` is suppressed |
| Dismissed after 7 days | May be recreated if factor still applies |
| Acted-on | Follows existing lifecycle policy |

### Platform Migration reference case

Opportunity ID: `822639e5-9bf7-40f1-8882-a11140362339`

```text
Score: 60 (MEDIUM)

Triggered:
  no_activity_7d          (+25)
  missing_decision_maker    (+15)
  past_expected_close_date  (+20)

Skipped:
  no_future_meeting         (meetings unavailable)
  stalled_stage             (stage_entered_at unavailable)
  overdue_tasks             (tasks unavailable)
  engagement_drop           (no prior baseline)

Not triggered:
  missing_proposal          (proposal evidence in emailText)

Notifications (max 2):
  past_expected_close_date
  no_activity_7d
```

Note: score is **60**, not 80 — `no_future_meeting` does not fire when meetings cannot be verified.

### Reasoning summary

The agent builds a deterministic `reasoning_summary` that distinguishes:

- Confirmed risks (triggered factors)
- Rules not triggered (with reason)
- Sections that could not be verified (meetings, tasks unavailable)

Example:

```text
Risk score is 60 (MEDIUM). The main risks are no_activity_7d: No trustworthy
dated activity is available.; missing_decision_maker: No decision-maker contact
is identified at the PROPOSAL stage.; past_expected_close_date: The expected
close date passed 136 days ago.. Proposal evidence was found in opportunity
content, so the missing-proposal rule did not trigger. Stage age is unavailable,
so stalled-stage was not evaluated. Meetings and tasks status could not be
verified from the available reader tools.
```

---

## Integration boundaries

### Entry points

| Function | Module | Role |
|----------|--------|------|
| `run_risk_notification_agent()` | `agents/risk/agent.py` | Pure agent — no I/O |
| `run_risk_agent_for_pipeline()` | `integration/risk_for_pipeline.py` | Loads context if missing, loads existing notifications, persists new ones |
| `run_daily_risk_sweep()` | `workflows/risk_sweep/sweep.py` | Batch evaluation + snapshots |
| `compare_score_to_previous()` | `workflows/risk_sweep/compare.py` | Snapshot storage and delta comparison |

### Pipeline integration

```python
from followup.integration.risk_for_pipeline import run_risk_agent_for_pipeline

result = await run_risk_agent_for_pipeline(
    event,
    list_existing_notifications,
    context=preloaded_context,          # optional — skips CRM fetch when provided
    notification_repository=repository,  # optional — persists new notifications
    use_llm_context=True,
    now=evaluation_time,                 # optional — deterministic testing
)
```

### Notification persistence

```python
from followup.notifications.repository import NotificationRepository
from followup.notifications.in_memory_repository import InMemoryNotificationRepository
```

The repository stores what the agent already decided should be persisted. **Do not duplicate deduplication logic** in the repository — lifecycle is handled in `apply_notification_lifecycle()`.

`InMemoryNotificationRepository` is development-only. Production should implement `NotificationRepository` against Postgres or CRM.

### Daily sweep identity

The sweep separates **authentication identity** from **opportunity ownership**:

| Field | Source | Used for |
|-------|--------|----------|
| `user_id` | `TWENTY_USER_ID` (authenticated user) | Bridge context loading — **always preferred** |
| `owner_id` | CRM `ownerId` | Future recipient selection, ownership reporting |

```python
# sweep.py resolves authenticated user for context loading:
user_id = opportunity.get("user_id") or opportunity.get("owner_id", "")
```

The dev sweep script (`try_run_risk_sweep.py`) sets both fields explicitly:

```python
{
    "id": opportunity_id,
    "owner_id": str(opportunity.get("ownerId") or ""),
    "user_id": TWENTY_USER_ID,
    "stage": stage,
    "name": name,
}
```

Required env vars for sweep: `TWENTY_WORKSPACE_ID`, `TWENTY_USER_ID`, `TWENTY_READER_ROLE_ID` (or `TWENTY_ROLE_ID`).

### Daily sweep behavior

```text
list active opportunities (exclude CLOSED_WON / CLOSED_LOST)
    │
    for each opportunity:
        load DealContext (authenticated user_id)
        run Risk Agent
        load previous snapshot
        compare score → persist snapshot
        persist new notifications
        continue on individual failures
```

**Snapshot significance** (via `compare_score_to_previous()`):

- Score worsens by ≥ 10 → significant
- Crosses into MEDIUM or HIGH → significant
- Improving score → recorded, no re-engagement draft
- First snapshot → no delta

---

## Testing & verification

```bash
cd packages/twenty-ai-service

# Full test suite
python -m pytest followup/tests -v

# Single deal (fixture)
python scripts/try_run_risk_agent.py --fixture risk_context_platform_migration.json --no-llm

# Single deal (live)
python scripts/try_run_risk_agent.py 822639e5-9bf7-40f1-8882-a11140362339 --no-llm

# API server
python -m uvicorn main:app --reload --port 8001

# Daily sweep
python scripts/try_run_risk_sweep.py --no-llm --limit 3
```

**Test files:**

| File | Coverage |
|------|----------|
| `test_context_loader.py` | Loader pipeline, mapping, enrichment |
| `test_risk_agent.py` | Rules, notifications, sweep/compare |
| `test_risk_integration.py` | Persistence, completeness-aware rules, pipeline |
| `test_risk_api.py` | HTTP risk evaluate endpoint |
| `test_risk_sweep_identity.py` | Sweep authenticated user vs owner |

---

## Quick reference: imports

```python
# Context
from followup.context import load_deal_context, DealContext

# Risk agent (pure)
from followup.agents.risk import run_risk_notification_agent
from followup.agents.risk.evaluation import evaluate_all_rules
from followup.agents.risk.rules import compute_risk_score

# Integration (with I/O)
from followup.integration.risk_for_pipeline import run_risk_agent_for_pipeline

# Sweep
from followup.workflows.risk_sweep import run_daily_risk_sweep, compare_score_to_previous

# Persistence (dev)
from followup.notifications.in_memory_repository import InMemoryNotificationRepository
from followup.store.risk_snapshot_store import InMemoryRiskSnapshotStore
```
