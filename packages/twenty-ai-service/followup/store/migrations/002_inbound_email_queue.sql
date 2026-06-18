-- Inbound email queue for Phase 1 collect / Phase 2 review workflows.
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS followup_agent.inbound_email_queue (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    message_id       text NOT NULL,
    sender_email     text NOT NULL,
    subject          text NOT NULL DEFAULT '',
    body             text NOT NULL DEFAULT '',
    received_at      timestamptz NOT NULL,
    opportunity_id   uuid,
    status           text NOT NULL DEFAULT 'pending',
    pipeline_run_id  uuid,
    error            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
    ALTER TABLE followup_agent.inbound_email_queue
        ADD CONSTRAINT inbound_email_queue_message_id_key UNIQUE (message_id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE followup_agent.inbound_email_queue
        ADD CONSTRAINT inbound_email_queue_status_check
        CHECK (status IN ('pending', 'processing', 'processed', 'skipped', 'failed'));
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL; END $$;

CREATE INDEX IF NOT EXISTS inbound_email_queue_status_created_idx
    ON followup_agent.inbound_email_queue (status, created_at);

CREATE INDEX IF NOT EXISTS inbound_email_queue_workspace_status_idx
    ON followup_agent.inbound_email_queue (workspace_id, status);
