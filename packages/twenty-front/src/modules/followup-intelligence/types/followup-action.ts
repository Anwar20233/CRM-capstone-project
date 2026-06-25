export type FollowupWorkflowStep = {
  index: number;
  kind: string; // create_task | create_note | update_stage | draft_email | book_meeting
  title: string;
  detail: string | null;
  priority: string | null;
  email_subject: string | null;
  email_body: string | null;
  meeting_start: string | null;
  meeting_end: string | null;
  invitees: string[];
};

export type FollowupSourceEmail = {
  subject: string | null;
  body: string | null;
  sender_email: string | null;
};

export type FollowupAction = {
  id: string;
  opportunity_id: string;
  action_type: string;
  action_payload: Record<string, unknown>;
  reasoning: string | null;
  urgency: string;
  profile_narrative: string | null;
  draft_subject: string | null;
  draft_body: string | null;
  steps: FollowupWorkflowStep[];
  source_email: FollowupSourceEmail | null;
  status: string;
  created_at: string | null;
  expires_at: string | null;
};

export type FollowupRisk = {
  opportunity_id: string;
  risk_score: number | null; // 0-1 scale from the daily sweep
  risk_level: string | null; // low | medium | high
  top_factors: Array<Record<string, unknown>>;
  assessed_at: string | null;
};

export type FollowupAcceptResult = {
  action_id: string;
  execution_status: string;
  error: string | null;
};

export type FollowupReviseResult = {
  previous_action_id: string;
  new_action: FollowupAction;
};

export type FollowupProfile = {
  opportunity_id: string;
  narrative: string;
  contacts: Array<{
    crm_id: string;
    name: string;
    role: string | null;
    email: string | null;
  }>;
  key_facts: string[];
  relationships: string[];
  risk_score: number | null;
  generated_at: string;
};
