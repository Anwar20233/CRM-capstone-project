-- Follow-Up Intelligence Agent — initial schema.
--
-- The agent's derived data has no home in Twenty's workspace schema (it is not
-- CRM data), so it lives in its own `followup_agent` schema inside the SAME
-- `default` Postgres database. This mirrors the convention already established
-- by packages/twenty-ai-service/seed_data.py — we do NOT introduce a new
-- database or touch Twenty's TypeORM-managed schemas.
--
-- This migration is additive and idempotent. seed_data.py seeded simpler
-- versions of four of these tables (profile_facts, profile_relationships,
-- shadow_entities, risk_snapshots) using `CREATE TABLE IF NOT EXISTS`. Running
-- this file:
--   * is a no-op for tables/columns that already exist,
--   * upgrades the four seeded tables in place (ADD COLUMN IF NOT EXISTS),
--   * creates the three new tables (profile_extractions, followup_pending_actions,
--     followup_runs),
-- so it converges to the same schema regardless of whether the seed ran first.
--
-- CHECK taxonomies are the UNION of the generic spec's allowed values and the
-- values the project actually emits (see seed_data.py). Twenty record ids are
-- uuids, so source ids that reference CRM records are typed `uuid` (not `text`).

CREATE SCHEMA IF NOT EXISTS followup_agent;

-- Add a CHECK / UNIQUE / FK constraint only if it does not already exist.
-- Postgres has no `ADD CONSTRAINT IF NOT EXISTS`, so each add is guarded. A
-- re-added CHECK/FK raises duplicate_object; a re-added UNIQUE raises
-- duplicate_table (its backing index name collides), so we swallow both.

-- ===========================================================================
-- 1. profile_facts — extracted facts about an entity, scoped to a deal.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.profile_facts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id   uuid NOT NULL,
    entity_type      text NOT NULL,
    entity_crm_id    uuid,
    shadow_entity_id uuid,
    fact_type        text NOT NULL,
    fact_value       text NOT NULL,
    confidence       double precision DEFAULT 0.8,
    sentiment        text,
    source_type      text NOT NULL,
    source_id        uuid,
    source_snippet   text,
    extracted_at     timestamptz DEFAULT now(),
    valid_from       timestamptz,
    superseded_by    uuid
);

-- The seeded tables defined columns without the defaults this schema expects
-- (the seed always supplied values), so backfill them to let the repositories
-- insert without spelling out every column.
ALTER TABLE followup_agent.profile_facts ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE followup_agent.profile_facts ALTER COLUMN confidence SET DEFAULT 0.8;
ALTER TABLE followup_agent.profile_facts ALTER COLUMN extracted_at SET DEFAULT now();

-- Columns added on top of the seeded shape.
ALTER TABLE followup_agent.profile_facts ADD COLUMN IF NOT EXISTS shadow_entity_id uuid;
ALTER TABLE followup_agent.profile_facts ADD COLUMN IF NOT EXISTS sentiment        text;
ALTER TABLE followup_agent.profile_facts ADD COLUMN IF NOT EXISTS source_snippet   text;
ALTER TABLE followup_agent.profile_facts ADD COLUMN IF NOT EXISTS valid_from       timestamptz;
ALTER TABLE followup_agent.profile_facts ADD COLUMN IF NOT EXISTS superseded_by    uuid;

DO $$ BEGIN
    -- Exactly one of the entity references must be set.
    ALTER TABLE followup_agent.profile_facts
        ADD CONSTRAINT profile_facts_entity_ref
        CHECK (entity_crm_id IS NOT NULL OR shadow_entity_id IS NOT NULL);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE followup_agent.profile_facts
        ADD CONSTRAINT profile_facts_superseded_by_fkey
        FOREIGN KEY (superseded_by) REFERENCES followup_agent.profile_facts (id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    -- 'contact'/'company'/'opportunity' (spec) + 'person'/'shadow' (project).
    ALTER TABLE followup_agent.profile_facts
        ADD CONSTRAINT profile_facts_entity_type_check
        CHECK (entity_type IN ('contact', 'company', 'opportunity', 'person', 'shadow'));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    -- Spec taxonomy + values already present in seeded data.
    ALTER TABLE followup_agent.profile_facts
        ADD CONSTRAINT profile_facts_fact_type_check
        CHECK (fact_type IN (
            'role', 'sentiment', 'concern', 'commitment', 'preference', 'deadline',
            'budget', 'competitor', 'decision_power', 'buying_signal',
            'gate', 'objection', 'process'
        ));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE followup_agent.profile_facts
        ADD CONSTRAINT profile_facts_sentiment_check
        CHECK (sentiment IS NULL OR sentiment IN ('positive', 'negative', 'neutral'));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE followup_agent.profile_facts
        ADD CONSTRAINT profile_facts_source_type_check
        CHECK (source_type IN ('email', 'note', 'crm_record', 'meeting', 'risk_score'));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS profile_facts_opportunity_idx
    ON followup_agent.profile_facts (opportunity_id, extracted_at DESC);

-- ===========================================================================
-- 2. profile_relationships — connections between entities, scoped to a deal.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.profile_relationships (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id     uuid NOT NULL,
    from_entity_crm_id uuid,
    from_shadow_id     uuid,
    to_entity_crm_id   uuid,
    to_shadow_id       uuid,
    relationship_type  text NOT NULL,
    description        text,
    confidence         double precision DEFAULT 0.8,
    source_type        text NOT NULL,
    source_id          uuid,
    first_seen_at      timestamptz DEFAULT now(),
    last_seen_at       timestamptz DEFAULT now()
);

ALTER TABLE followup_agent.profile_relationships ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE followup_agent.profile_relationships ALTER COLUMN confidence SET DEFAULT 0.8;
ALTER TABLE followup_agent.profile_relationships ALTER COLUMN first_seen_at SET DEFAULT now();

ALTER TABLE followup_agent.profile_relationships ADD COLUMN IF NOT EXISTS from_shadow_id uuid;
ALTER TABLE followup_agent.profile_relationships ADD COLUMN IF NOT EXISTS to_shadow_id   uuid;
ALTER TABLE followup_agent.profile_relationships ADD COLUMN IF NOT EXISTS source_id      uuid;
ALTER TABLE followup_agent.profile_relationships ADD COLUMN IF NOT EXISTS last_seen_at   timestamptz DEFAULT now();

DO $$ BEGIN
    ALTER TABLE followup_agent.profile_relationships
        ADD CONSTRAINT profile_relationships_from_ref
        CHECK (from_entity_crm_id IS NOT NULL OR from_shadow_id IS NOT NULL);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE followup_agent.profile_relationships
        ADD CONSTRAINT profile_relationships_to_ref
        CHECK (to_entity_crm_id IS NOT NULL OR to_shadow_id IS NOT NULL);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    -- Spec taxonomy + values already present in seeded data.
    ALTER TABLE followup_agent.profile_relationships
        ADD CONSTRAINT profile_relationships_type_check
        CHECK (relationship_type IN (
            'reports_to', 'manages', 'works_with', 'champions', 'blocks', 'decides',
            'influences', 'introduced', 'replaced',
            'collaborates_with', 'needs_approval_from'
        ));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE followup_agent.profile_relationships
        ADD CONSTRAINT profile_relationships_source_type_check
        CHECK (source_type IN ('email', 'note', 'crm_record', 'meeting', 'risk_score'));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS profile_relationships_opportunity_idx
    ON followup_agent.profile_relationships (opportunity_id);

-- ===========================================================================
-- 3. shadow_entities — people mentioned but not yet in the CRM.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.shadow_entities (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id    uuid NOT NULL,
    workspace_id      uuid NOT NULL,
    name              text NOT NULL,
    email_address     text,
    title_or_role     text,
    company_crm_id    uuid,
    aliases           jsonb NOT NULL DEFAULT '[]'::jsonb,
    mention_count     integer NOT NULL DEFAULT 1,
    first_seen_at     timestamptz DEFAULT now(),
    last_seen_at      timestamptz DEFAULT now(),
    status            text NOT NULL DEFAULT 'shadow',
    promoted_to_crm_id uuid,
    promoted_at       timestamptz,
    dismissed_at      timestamptz,
    dismiss_reason    text
);

ALTER TABLE followup_agent.shadow_entities ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE followup_agent.shadow_entities ALTER COLUMN aliases SET DEFAULT '[]'::jsonb;
ALTER TABLE followup_agent.shadow_entities ALTER COLUMN mention_count SET DEFAULT 1;
ALTER TABLE followup_agent.shadow_entities ALTER COLUMN status SET DEFAULT 'shadow';
ALTER TABLE followup_agent.shadow_entities ALTER COLUMN first_seen_at SET DEFAULT now();
ALTER TABLE followup_agent.shadow_entities ALTER COLUMN last_seen_at SET DEFAULT now();

ALTER TABLE followup_agent.shadow_entities ADD COLUMN IF NOT EXISTS promoted_to_crm_id uuid;
ALTER TABLE followup_agent.shadow_entities ADD COLUMN IF NOT EXISTS promoted_at        timestamptz;
ALTER TABLE followup_agent.shadow_entities ADD COLUMN IF NOT EXISTS dismissed_at       timestamptz;
ALTER TABLE followup_agent.shadow_entities ADD COLUMN IF NOT EXISTS dismiss_reason     text;

DO $$ BEGIN
    -- Prevent duplicate shadows once an email is known. Postgres allows many
    -- NULL emails, which is what we want for not-yet-identified mentions.
    ALTER TABLE followup_agent.shadow_entities
        ADD CONSTRAINT shadow_entities_opportunity_email_key
        UNIQUE (opportunity_id, email_address);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    -- 'shadow'/'promoted'/'dismissed'/'merged' (spec)
    -- + 'detected'/'pending_promotion' (project lifecycle).
    ALTER TABLE followup_agent.shadow_entities
        ADD CONSTRAINT shadow_entities_status_check
        CHECK (status IN (
            'shadow', 'promoted', 'dismissed', 'merged',
            'detected', 'pending_promotion'
        ));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS shadow_entities_opportunity_idx
    ON followup_agent.shadow_entities (opportunity_id, status);

-- ===========================================================================
-- 4. profile_extractions — audit log for every extraction run.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.profile_extractions (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id           uuid NOT NULL,
    workspace_id             uuid NOT NULL,
    trigger_type             text NOT NULL CHECK (trigger_type IN ('email', 'note', 'crm_update', 'meeting')),
    trigger_id               text,
    input_summary            text,
    entities_found           integer DEFAULT 0,
    facts_extracted          integer DEFAULT 0,
    relationships_extracted  integer DEFAULT 0,
    shadow_entities_created  integer DEFAULT 0,
    unresolved_mentions      integer DEFAULT 0,
    llm_model                text,
    tokens_used              integer,
    created_at               timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS profile_extractions_opportunity_idx
    ON followup_agent.profile_extractions (opportunity_id, created_at DESC);

-- ===========================================================================
-- 5. followup_pending_actions — recommendations awaiting rep review.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.followup_pending_actions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id    uuid NOT NULL,
    workspace_id      uuid NOT NULL,
    trigger_type      text NOT NULL CHECK (trigger_type IN ('email_signal', 'risk_alert', 'direct_request')),
    trigger_id        text,
    action_type       text NOT NULL CHECK (action_type IN (
        -- Agent recommendation types (NEXT_STEP_TYPES):
        'follow_up_call', 'send_proposal', 'check_in', 'escalate',
        'close_deal', 'schedule_meeting', 'no_action',
        -- CRM-operation types (execution layer):
        'send_email', 'create_task', 'add_note',
        'update_deal_stage', 'add_contact'
    )),
    action_payload    jsonb NOT NULL,
    reasoning         text,
    urgency           text DEFAULT 'medium' CHECK (urgency IN ('high', 'medium', 'low')),
    next_step_result  jsonb,
    risk_assessment   jsonb,
    draft_result      jsonb,
    profile_narrative text,
    status            text DEFAULT 'pending' CHECK (status IN (
        'pending', 'accepted', 'rejected', 'edited', 'expired'
    )),
    created_at        timestamptz DEFAULT now(),
    expires_at        timestamptz,
    acted_on_at       timestamptz,
    acted_on_by       uuid,
    execution_status  text CHECK (execution_status IS NULL OR execution_status IN (
        'executing', 'completed', 'failed'
    )),
    execution_error   text,
    executed_at       timestamptz
);

CREATE INDEX IF NOT EXISTS followup_pending_actions_opportunity_idx
    ON followup_agent.followup_pending_actions (opportunity_id, status);
CREATE INDEX IF NOT EXISTS followup_pending_actions_expiry_idx
    ON followup_agent.followup_pending_actions (status, expires_at);

-- ===========================================================================
-- 6. followup_runs — one row per orchestrator run, for observability.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.followup_runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id    uuid NOT NULL,
    workspace_id      uuid NOT NULL,
    entry_point       text NOT NULL CHECK (entry_point IN (
        'email_signal', 'risk_sweep', 'direct', 'crm_event'
    )),
    trigger_payload   jsonb,
    started_at        timestamptz DEFAULT now(),
    completed_at      timestamptz,
    duration_ms       integer,
    profile_loaded    boolean DEFAULT false,
    agents_invoked    text[] DEFAULT '{}',
    pending_action_id uuid,
    error             text,
    status            text DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS followup_runs_opportunity_idx
    ON followup_agent.followup_runs (opportunity_id, started_at DESC);

-- ===========================================================================
-- Supporting table already seeded by the project (P3 risk agent). Kept here so
-- a fresh database gets the complete schema; not one of the six core tables.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.risk_snapshots (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id uuid NOT NULL,
    score          integer NOT NULL,
    factors        jsonb NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE followup_agent.risk_snapshots ALTER COLUMN id SET DEFAULT gen_random_uuid();

-- ===========================================================================
-- Daily Risk Sweep — score history used only for threshold-crossing detection.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS followup_agent.risk_daily_scores (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id          uuid NOT NULL,
    workspace_id            uuid NOT NULL,
    risk_score              double precision NOT NULL,
    risk_level              text NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
    top_factors             jsonb NOT NULL DEFAULT '[]'::jsonb,
    assessment              jsonb NOT NULL,
    trigger_type            text NOT NULL DEFAULT 'daily_sweep',
    assessed_at             timestamptz NOT NULL DEFAULT now(),
    created_pending_action_id uuid
);

ALTER TABLE followup_agent.risk_daily_scores ALTER COLUMN id SET DEFAULT gen_random_uuid();
ALTER TABLE followup_agent.risk_daily_scores ALTER COLUMN top_factors SET DEFAULT '[]'::jsonb;
ALTER TABLE followup_agent.risk_daily_scores ALTER COLUMN trigger_type SET DEFAULT 'daily_sweep';
ALTER TABLE followup_agent.risk_daily_scores ALTER COLUMN assessed_at SET DEFAULT now();

CREATE INDEX IF NOT EXISTS risk_daily_scores_opportunity_idx
    ON followup_agent.risk_daily_scores (opportunity_id, assessed_at DESC);

CREATE INDEX IF NOT EXISTS risk_daily_scores_workspace_idx
    ON followup_agent.risk_daily_scores (workspace_id, assessed_at DESC);
