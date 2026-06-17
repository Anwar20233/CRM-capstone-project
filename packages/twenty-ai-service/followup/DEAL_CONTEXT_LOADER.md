# Standalone Deal Context Loader

> **Team usage guide:** see [DEAL_CONTEXT_AND_RISK_AGENT.md](./DEAL_CONTEXT_AND_RISK_AGENT.md) for the current DealContext schema and Risk Agent behavior.

This document describes the **P1-independent deal context loading pipeline** added under `followup/context/`. It lets any follow-up agent (risk, next-step, drafting) fetch live opportunity data from Twenty and receive a normalized `DealContext` without going through the Follow-Up Orchestrator.

---

## Problem it solves

The [Follow-Up Intelligence Layer spec](../FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md) originally assigned `load_deal_context()` to **Person 1 (Follow-Up Orchestrator)**. In practice:

- Risk, next-step, and drafting agents already consume `DealContext` as input.
- Before this work, context only existed as **JSON test fixtures** or **injected callables** in tests/demos.
- Agents could not load live CRM data on their own.

This module provides a **shared, importable** `load_deal_context()` that any agent can call directly.

---

## Architecture

```text
Agent (risk / next-step / drafting)
        │
        ▼
  load_deal_context()          ← public entry point (loader.py)
        │
        ├─► fetch_opportunity_bundle()   ← parallel bridge reads (crm_fetch.py)
        │         │
        │         └── agent-bridge → Twenty GraphQL (read-only)
        │
        ├─► extract_deal_context()       ← LLM maps raw JSON → DealContext (llm_extract.py)
        │         │
        │         └── on failure ──► map_deal_context_fallback() (map_crm.py)
        │
        └─► enrich_context()             ← deterministic engagement metrics (enrich.py)
                  │
                  └── DealContext
```

| Step | Module | Uses LLM? | Responsibility |
|------|--------|---------|----------------|
| CRM fetch | `crm_fetch.py` | No | Exact read-only data from Twenty via agent-bridge |
| Normalization | `llm_extract.py` | Yes | Map messy CRM JSON (notes, nested fields) into `DealContext` |
| Fallback mapping | `map_crm.py` | No | Deterministic mapper when LLM fails or is disabled |
| Engagement metrics | `enrich.py` | No | `days_since_last_activity`, 14-day counts, `has_future_meeting` |

**Design principle:** Database reads and date math are always deterministic. The LLM is only used to normalize unstructured CRM shapes into the frozen `DealContext` schema.

---

## Files added

```
followup/context/
├── crm_identity.py    # CrmIdentity + resolve_crm_identity()
├── crm_fetch.py       # fetch_opportunity_bundle() → RawOpportunityBundle
├── map_crm.py         # map_deal_context_fallback()
├── llm_extract.py     # extract_deal_context()
├── enrich.py          # enrich_context()
├── loader.py          # load_deal_context() — main public API
├── errors.py          # ContextLoadError, LlmExtractError
├── protocols.py       # DealContextExtractor protocol (for tests/mocking)
└── schemas.py         # DealContext and snapshot models (pre-existing)

followup/api/
├── router.py          # FastAPI router mount
└── routes_context.py  # GET /followup/context/{opportunity_id}

followup/tests/
├── test_context_loader.py
└── fixtures/raw_opportunity_bundle.json
```

### Modified files

| File | Change |
|------|--------|
| `followup/context/__init__.py` | Barrel exports for loader and helpers |
| `followup/integration/risk_for_pipeline.py` | Auto-loads context when not provided |
| `followup/workflows/risk_sweep/sweep.py` | Defaults to `load_deal_context` |
| `main.py` | Mounts followup API router |

---

## Public API

### Python import (primary)

```python
from followup.context import load_deal_context

context = await load_deal_context(
    opportunity_id="...",
    workspace_id="...",
    user_id="...",
    role_id=None,       # optional; falls back to TWENTY_READER_ROLE_ID / TWENTY_ROLE_ID
    use_llm=True,       # set False for deterministic-only mapping
)
```

Returns a `DealContext` with:

- `opportunity`, `company`, `contacts`, `timeline`, `tasks`, `meetings`, `pipeline_meta`
- `engagement` (computed by `enrich.py`)
- `loaded_at`, `context_provenance` (`"hybrid"` if LLM succeeded, `"crm_fallback"` if fallback path)

### HTTP endpoint (optional)

```
GET /followup/context/{opportunity_id}?workspace_id=...&user_id=...
```

Query parameters:

| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `workspace_id` | Yes | — | Twenty workspace UUID |
| `user_id` | Yes | — | Acting user UUID |
| `role_id` | No | env fallback | Bridge role UUID |
| `use_llm` | No | `true` | Enable LLM normalization |

---

## Pipeline details

### 1. CRM fetch (`crm_fetch.py`)

Runs parallel agent-bridge calls:

- `find_one_opportunity`
- `find_notes` / `find_tasks` linked to the opportunity
- `get_field_metadata` for opportunity `stage` (pipeline stages)
- `find_one_company` when a company is linked
- `find_one_person` for point of contact

Output: `RawOpportunityBundle` — raw dicts/lists, no transformation.

### 2. LLM extraction (`llm_extract.py`)

Sends the bundle to the LLM with a JSON-only prompt. The model returns a `DealContext`-shaped object (excluding `engagement`, `loaded_at`, `context_provenance`).

On `ValidationError` or bad JSON → raises `LlmExtractError` → loader falls back to `map_crm.py`.

### 3. Deterministic fallback (`map_crm.py`)

Pure Python mapping:

- `amountMicros` → float `amount`
- Stage labels from metadata or raw values
- Notes/tasks → `timeline` and `task` snapshots
- Point of contact → `contacts[]`
- Pipeline stages → `pipeline_meta`

### 4. Enrichment (`enrich.py`)

Always runs after mapping. Computes:

- `engagement.days_since_last_activity`
- `engagement.activity_count_14d` / `activity_count_prior_14d`
- `engagement.has_future_meeting`
- Re-validates `task.is_overdue`
- Sets `loaded_at` and final `context_provenance`

---

## Environment requirements

For live CRM reads, these must be set (see `.env.example`):

```bash
TWENTY_WORKSPACE_ID=...
TWENTY_USER_ID=...
TWENTY_READER_ROLE_ID=...   # or TWENTY_ROLE_ID as fallback
NODE_BRIDGE_BASE_URL=http://localhost:3000/agent-bridge

# For LLM extraction (when use_llm=True)
LLM_API_KEY=...
LLM_MODEL=...
```

---

## Error handling

| Error | Code | Behavior |
|-------|------|----------|
| Opportunity not found | `OPPORTUNITY_NOT_FOUND` | Raised; sweep skips opportunity |
| Bridge unreachable | `BRIDGE_UNREACHABLE` | Raised |
| LLM returns invalid JSON | — | Logged; falls back to `map_crm` |
| Mapping fails entirely | `MAP_FAILED` | Raised with bundle detail |

---

## How this works with the Risk Agent

The risk agent (`followup/agents/risk/`) is a **pure function of `DealContext`**. It does not fetch CRM data itself. The new loader sits **upstream** and supplies that input.

### Data flow

```text
FollowUpEvent (opportunity_id, workspace_id, user_id, event_type)
        │
        ▼
run_risk_agent_for_pipeline()          ← integration/risk_for_pipeline.py
        │
        ├─► load_deal_context()        ← NEW: auto-called when context=None
        │         └── DealContext
        │
        ├─► list_existing_notifications()
        │
        └─► run_risk_notification_agent(context, event, existing)
                  │
                  ├─► compute_risk_score(context)      ← rules.py (7 rules)
                  ├─► detect_risk_signals(context)
                  ├─► merge_deal_risk_report_signals()  ← optional extra CRM read
                  ├─► should_notify()
                  ├─► generate_notification_copy()      ← LLM for notification text
                  └─► RiskNotificationAgentResult
```

### What the risk agent reads from `DealContext`

The 7 risk rules in `rules.py` depend on these context fields:

| Rule | Context fields used |
|------|---------------------|
| `no_activity_7d` | `engagement.days_since_last_activity` |
| `no_future_meeting` | `engagement.has_future_meeting`, `opportunity.stage` |
| `stalled_stage` | `opportunity.stage`, `stage_entered_at`, `pipeline_meta.stage_sla_days` |
| `missing_decision_maker` | `contacts[].is_decision_maker`, `opportunity.stage` |
| `missing_proposal` | `timeline[]` (proposal keywords), `opportunity.stage` |
| `overdue_tasks` | `tasks[].is_overdue` |
| `engagement_drop` | `engagement.activity_count_14d`, `activity_count_prior_14d` |

Because `enrich.py` computes engagement metrics **after** context loading, the risk agent gets consistent, deterministic signals regardless of whether the LLM or fallback mapper produced the base context.

### Three ways to invoke risk with live context

#### Option A — Pipeline helper (recommended for events)

```python
from followup.integration.risk_for_pipeline import run_risk_agent_for_pipeline

result = await run_risk_agent_for_pipeline(
    event=follow_up_event,
    list_existing_notifications=my_notification_store.list,
    # context is loaded automatically from Twenty
)
```

Pass `context=preloaded_context` to skip the fetch (e.g. when P1 already loaded it). Pass `use_llm_context=False` for deterministic-only mapping.

#### Option B — Direct agent call (full control)

```python
from followup.context import load_deal_context
from followup.agents.risk.agent import run_risk_notification_agent

context = await load_deal_context(
    event.opportunity_id,
    event.workspace_id,
    event.user_id,
)

result = await run_risk_notification_agent(context, event, existing_notifications)
```

#### Option C — Daily risk sweep (batch)

```python
from followup.workflows.risk_sweep.sweep import run_daily_risk_sweep

result = await run_daily_risk_sweep(
    workspace_id="...",
    list_active_opportunities=my_list_fn,
    snapshot_store=my_snapshot_store,
    # load_deal_context_fn defaults to load_deal_context
)
```

The sweep loads context per opportunity, runs the risk agent, compares scores to previous snapshots, and flags re-engagement drafts when thresholds are crossed.

### Event types that route to risk

Defined in `EVENT_TYPES_WITH_RISK_AGENT`:

- `opportunity_created`, `opportunity_updated`, `opportunity_stage_changed`
- `email_sent`, `proposal_sent`, `task_completed`, `activity_logged`
- `daily_risk_sweep`

### Example: stale deal end-to-end

1. **Fetch** — `load_deal_context("opp-stale-001", ...)` reads Globex Renewal from Twenty.
2. **Map** — LLM (or fallback) produces `DealContext` with stage `Proposal`, 1 overdue task, low recent activity.
3. **Enrich** — `engagement.days_since_last_activity = 18`, `has_future_meeting = false`.
4. **Score** — Rules fire: `no_activity_7d`, `no_future_meeting`, `overdue_tasks`, possibly `stalled_stage`.
5. **Notify** — `should_notify()` creates drafts; `generate_notification_copy()` writes notification body via LLM.
6. **Return** — `RiskNotificationAgentResult` with `risk_score` and `notifications[]`.

---

## Testing

```bash
cd packages/twenty-ai-service
python -m pytest followup/tests/test_context_loader.py -q
python -m pytest followup/tests/test_risk_agent.py -q
```

- Context loader tests mock bridge and LLM — no live CRM or API keys required.
- Risk agent tests continue using JSON fixtures in `followup/tests/fixtures/`.
- Raw bridge fixture: `followup/tests/fixtures/raw_opportunity_bundle.json`.

---

## Future compatibility

When P1 Client Profile is implemented:

- `loader.py` will gain a `profile_primary` flag.
- Profile facts merge on top of CRM data; agents keep the same `load_deal_context` import.
- P1 orchestrator will **call** this loader, not replace it.

Next-step and drafting agents can adopt the same pattern:

```python
from followup.context import load_deal_context

context = await load_deal_context(opportunity_id, workspace_id, user_id)
# pass context to run_next_step_agent / run_drafting_agent
```
