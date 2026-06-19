import { REACT_APP_AI_SERVICE_URL } from '@/followup-intelligence/utils/get-ai-service-base-url';
import {
  type FollowupAcceptResult,
  type FollowupAction,
  type FollowupProfile,
  type FollowupReviseResult,
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

export const acceptFollowupAction = async (
  actionId: string,
  userId: string,
): Promise<FollowupAcceptResult> => {
  return followupRequest<FollowupAcceptResult>(
    `/followup/actions/${actionId}/accept`,
    {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
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
