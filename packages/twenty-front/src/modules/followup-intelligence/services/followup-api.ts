import { REACT_APP_AI_SERVICE_URL } from '@/followup-intelligence/utils/get-ai-service-base-url';
import {
  type FollowupAcceptResult,
  type FollowupAction,
  type FollowupProfile,
  type FollowupReviseResult,
  type FollowupRisk,
} from '@/followup-intelligence/types/followup-action';

const followupRequest = async <TResponse>(
  path: string,
  options?: RequestInit,
): Promise<TResponse> => {
  const response = await fetch(`${REACT_APP_AI_SERVICE_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(
      detail || `Follow-up API request failed with status ${response.status}`,
    );
  }

  return response.json() as Promise<TResponse>;
};

export const fetchFollowupActions = async (
  opportunityId: string,
  status = 'pending',
): Promise<FollowupAction[]> => {
  const params = new URLSearchParams({
    opportunity_id: opportunityId,
    status,
  });

  return followupRequest<FollowupAction[]>(`/followup/actions?${params}`);
};

export const fetchFollowupProfile = async (
  opportunityId: string,
): Promise<FollowupProfile> => {
  return followupRequest<FollowupProfile>(`/followup/profile/${opportunityId}`);
};

export const fetchFollowupRisk = async (
  opportunityId: string,
): Promise<FollowupRisk> => {
  return followupRequest<FollowupRisk>(`/followup/risk/${opportunityId}`);
};

export const acceptFollowupAction = async (
  actionId: string,
  userId: string,
  disabledStepIndices: number[] = [],
): Promise<FollowupAcceptResult> => {
  return followupRequest<FollowupAcceptResult>(
    `/followup/actions/${actionId}/accept`,
    {
      method: 'POST',
      body: JSON.stringify({
        user_id: userId,
        disabled_step_indices: disabledStepIndices,
      }),
    },
  );
};

export const rejectFollowupAction = async (
  actionId: string,
  userId: string,
  reason?: string,
): Promise<{ action_id: string; status: string }> => {
  return followupRequest<{ action_id: string; status: string }>(
    `/followup/actions/${actionId}/reject`,
    {
      method: 'POST',
      body: JSON.stringify({ user_id: userId, reason }),
    },
  );
};

export type FollowupStepEdit = {
  index: number;
  email_subject?: string | null;
  email_body?: string | null;
  title?: string | null;
  detail?: string | null;
};

export const editFollowupAction = async (
  actionId: string,
  userId: string,
  steps: FollowupStepEdit[],
): Promise<FollowupAction> => {
  const result = await followupRequest<{ action: FollowupAction }>(
    `/followup/actions/${actionId}/edit`,
    {
      method: 'POST',
      body: JSON.stringify({ user_id: userId, steps }),
    },
  );

  return result.action;
};

export const reviseFollowupAction = async (
  actionId: string,
  userId: string,
  instructions: string,
): Promise<FollowupReviseResult> => {
  return followupRequest<FollowupReviseResult>(
    `/followup/actions/${actionId}/revise`,
    {
      method: 'POST',
      body: JSON.stringify({ user_id: userId, instructions }),
    },
  );
};
