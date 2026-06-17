# Risk Scoring & Notification Agent (Person 3)

> **Team usage guide:** see [DEAL_CONTEXT_AND_RISK_AGENT.md](../DEAL_CONTEXT_AND_RISK_AGENT.md) for the current DealContext schema and Risk Agent behavior.

The risk agent answers: **“Is this deal at risk, and should we alert the sales rep?”**

It scores deal health from **0–100** using deterministic rules, optionally enriches with the read-only `deal_risk_report` CRM workflow, and returns **0–2 notifications** per run with human-readable reasoning. It **never writes to CRM** and **never touches the database** — Person 1 (orchestrator) loads context, passes existing notifications, persists results, and exposes the API.

---

## How it works (end-to-end)

```text
DealContext + FollowUpEvent + existing_notifications[]
        │
        ▼
  rules.py ──► compute_risk_score / detect_risk_signals (0–100, LOW/MEDIUM/HIGH)
        │
        ▼
  agent.py ──► optional deal_risk_report enrichment (read-only, non-blocking)
        │
        ▼
  notifications.py ──► should_notify (0–2 drafts) ──► generate_notification_copy (LLM or template)
        │
        ▼
  notifications.py ──► apply_notification_lifecycle (dedupe by opportunity + rule_id)
        │
        ▼
  RiskNotificationAgentResult { risk_score, notifications[], reasoning_summary }
        │
        ▼
  [P1 persists to Postgres + serves GET /followup/notifications]
```

**Daily sweep** (`workflows/risk_sweep/`) runs the same agent over all active opportunities, compares scores to previous snapshots, and flags deals that need a **re-engagement draft** (P4).

---

## Quick start

### Unit tests

```bash
cd packages/twenty-ai-service
python -m pytest followup/tests/test_risk_agent.py -q
```

### Live demo (dummy data, no CRM)

```bash
python scripts/demo_risk_agent.py --scenario stale
python scripts/demo_risk_agent.py --all
python scripts/demo_risk_agent.py --sweep
python scripts/demo_risk_agent.py --trend
```

### Production entry point (P1 calls this)

```python
from followup.agents.risk import run_risk_notification_agent
# or
from followup.integration import run_risk_agent_for_pipeline
```

See **[P1 Integration Guide](../../P1_INTEGRATION.md)** for orchestrator wiring.

---

## Risk rules (7 rules, cap 100)

| Rule ID | Points | Trigger |
|---------|--------|---------|
| `no_activity_7d` | +25 | No activity ≥ 7 days |
| `no_future_meeting` | +20 | No scheduled meeting (non-closed stages) |
| `stalled_stage` | +20 | Days in stage > SLA |
| `missing_decision_maker` | +15 | Past Discovery, no decision-maker contact |
| `missing_proposal` | +20 | Stage ≥ Proposal, no proposal evidence in timeline |
| `overdue_tasks` | +15 | Any overdue task |
| `engagement_drop` | +10 | Activity down >50% vs prior 14 days |

**Levels:** LOW 0–39 · MEDIUM 40–69 · HIGH 70+

**Notifications fire when:** score ≥ 40 **or** any HIGH-severity factor · max **2** per run · same `rule_id` not re-sent if dismissed within **7 days**.

---

## File reference

### Core agent (`followup/agents/risk/`)

| File | Purpose |
|------|---------|
| **`agent.py`** | Main entry: `run_risk_notification_agent(context, event, existing_notifications, *, llm_generator=None)`. Orchestrates rules → enrichment → notify → LLM/template copy → lifecycle dedupe. Returns `RiskNotificationAgentResult`. |
| **`rules.py`** | Deterministic scoring. `RULE_DEFINITIONS` (7 rules), helpers (`days_in_stage`, `stage_past_discovery`, `has_proposal_evidence`), `compute_risk_score()`, `detect_risk_signals()`, `score_to_level()`. Pure functions of `DealContext` only. |
| **`notifications.py`** | Notification lifecycle. `should_notify()` picks 0–2 drafts; `generate_notification_copy()` writes body via LLM or template fallback; `apply_notification_lifecycle()` dedupes by `(opportunity_id, rule_id)`. Exports `template_notification_copy` for demos/tests. |
| **`schemas.py`** | Pydantic models: `RiskFactor`, `RiskScore`, `RiskScoreBreakdown`, `Notification`, `NotificationDraft`, `RiskNotificationAgentResult`, `RiskScoreSnapshot`, `RiskSweepResult`, etc. |
| **`__init__.py`** | Public exports for P1: `run_risk_notification_agent`, schemas. |

### Daily sweep (`followup/workflows/risk_sweep/`)

| File | Purpose |
|------|---------|
| **`sweep.py`** | `run_daily_risk_sweep(workspace_id, ...)` — iterates active opportunities, runs risk agent per deal, saves snapshots via `compare_score_to_previous`, optional profile fact updates via `ProfileServiceProtocol`. Returns `RiskSweepResult` with `needs_re_engagement_draft` flags for P4. |
| **`compare.py`** | `compare_score_to_previous()` — stores `RiskScoreSnapshot`, computes `delta`, `level_crossed_up`, `threshold_crossed` (re-engagement when \|delta\| ≥ 10 or level crosses up). `needs_re_engagement_draft()` helper. |
| **`__init__.py`** | Exports `run_daily_risk_sweep`, `compare_score_to_previous`. |

### Storage protocols (`followup/store/`)

| File | Purpose |
|------|---------|
| **`risk_snapshot_store.py`** | `RiskSnapshotStore` protocol + `InMemoryRiskSnapshotStore` for tests/demos. **P1 implements Postgres-backed store** for production `risk_score_snapshots` table. |
| **`protocols.py`** | `FollowUpStoreProtocol` — methods P1 repository must implement: `list_notifications_for_opportunity`, `save_risk_score`, `save_notification`, `save_risk_score_snapshot`, etc. |
| **`__init__.py`** | Re-exports snapshot store types. |

### P1 integration (`followup/integration/`)

| File | Purpose |
|------|---------|
| **`risk_for_pipeline.py`** | `run_risk_agent_for_pipeline()` — loads existing notifications via callback, then calls agent. `EVENT_TYPES_WITH_RISK_AGENT` constant for routing table alignment. |
| **`__init__.py`** | Public integration exports. |

### Shared contracts (P1 owns long-term; P3 scaffolded minimal versions)

| File | Purpose |
|------|---------|
| **`followup/context/schemas.py`** | `DealContext`, `OpportunitySnapshot`, `EngagementMetrics`, etc. Input to all agents. P1 extends with profile fields. |
| **`followup/events/schemas.py`** | `FollowUpEvent`, `FollowUpEventType`. Event payload from CRM/BullMQ. |
| **`followup/schemas/agents.py`** | Re-exports risk agent result models for P1 `schemas/agents.py` contract. |
| **`followup/profile/protocols.py`** | `ProfileServiceProtocol.apply_fact_updates()` — optional hook after sweep/score for Client Profile memory (P1 implements). |

### Tests & fixtures

| File | Purpose |
|------|---------|
| **`followup/tests/test_risk_agent.py`** | 14 tests covering all 10 spec scenarios + sweep/compare integration. |
| **`followup/tests/conftest.py`** | Registers `pytest-asyncio` plugin. |
| **`followup/tests/fixtures/risk_context_healthy.json`** | Low-risk deal fixture (Discovery, active engagement, future meeting). |
| **`followup/tests/fixtures/risk_context_stale.json`** | High-risk deal fixture (Proposal, no activity, overdue tasks, no meeting). |

### Scripts

| File | Purpose |
|------|---------|
| **`scripts/demo_risk_agent.py`** | CLI demo: scenarios, daily sweep, score trend, dismiss suppression. Uses template mode by default (`--use-llm` for real LLM). |

### Documentation

| File | Purpose |
|------|---------|
| **`followup/P1_INTEGRATION.md`** | Step-by-step checklist for P1: LangGraph node, Postgres schema, persist, API, cron sweep. |

---

## Main function signatures

```python
async def run_risk_notification_agent(
    context: DealContext,
    event: FollowUpEvent,
    existing_notifications: list[Notification],
    *,
    llm_generator: LLMCopyGenerator | None = None,
) -> RiskNotificationAgentResult

async def run_risk_agent_for_pipeline(
    context: DealContext,
    event: FollowUpEvent,
    list_existing_notifications: Callable[[str, str], Awaitable[list[Notification]]],
) -> RiskNotificationAgentResult

async def run_daily_risk_sweep(
    workspace_id: str,
    *,
    list_active_opportunities: ...,
    load_deal_context: ...,
    snapshot_store: RiskSnapshotStore,
    list_existing_notifications: ... | None = None,
    profile_service: ProfileServiceProtocol | None = None,
    llm_generator: LLMCopyGenerator | None = None,
) -> RiskSweepResult

async def compare_score_to_previous(
    opportunity_id: str,
    workspace_id: str,
    new_score: int,
    factors: list[RiskFactor],
    snapshot_store: RiskSnapshotStore,
    source: str = "event",
) -> RiskScoreSnapshot
```

---

## Dependencies

| Import from | Used for |
|-------------|----------|
| `followup/context/schemas.py` | `DealContext` input |
| `followup/events/schemas.py` | `FollowUpEvent` input |
| `agent/tools/workflows.py` | Optional `_deal_risk_report` read (enrichment) |
| `agent/llm_client.py` | Notification body prose (production default) |

**Does not import:** CRM write tools, database drivers, FastAPI (except via P1 API layer).

---

## Spec reference

Full system spec: [`FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md`](../../FOLLOWUP_INTELLIGENCE_LAYER_FINAL_SPEC.md) — Person 3 section + Part A architecture.

P1 wiring guide: [`followup/P1_INTEGRATION.md`](../../P1_INTEGRATION.md).
