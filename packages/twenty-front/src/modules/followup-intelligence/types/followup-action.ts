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
  status: string;
  created_at: string | null;
  expires_at: string | null;
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
