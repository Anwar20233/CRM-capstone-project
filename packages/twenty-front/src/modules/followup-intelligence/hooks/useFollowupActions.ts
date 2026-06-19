import { useCallback, useEffect, useState } from 'react';

import { currentUserState } from '@/auth/states/currentUserState';
import { FOLLOWUP_ACTIONS_POLL_INTERVAL_MS } from '@/followup-intelligence/constants/followup-polling';
import {
  acceptFollowupAction,
  fetchFollowupActions,
  rejectFollowupAction,
  reviseFollowupAction,
} from '@/followup-intelligence/services/followup-api';
import { type FollowupAction } from '@/followup-intelligence/types/followup-action';
import { useSnackBar } from '@/ui/feedback/snack-bar-manager/hooks/useSnackBar';
import { useAtomStateValue } from '@/ui/utilities/state/jotai/hooks/useAtomStateValue';

type UseFollowupActionsResult = {
  actions: FollowupAction[];
  loading: boolean;
  error: string | null;
  isMutating: boolean;
  refetch: () => Promise<void>;
  acceptAction: (actionId: string) => Promise<void>;
  rejectAction: (actionId: string) => Promise<void>;
  reviseAction: (actionId: string, instructions: string) => Promise<void>;
};

export const useFollowupActions = (
  opportunityId: string,
): UseFollowupActionsResult => {
  const currentUser = useAtomStateValue(currentUserState);
  const { enqueueErrorSnackBar, enqueueSuccessSnackBar } = useSnackBar();
  const [actions, setActions] = useState<FollowupAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isMutating, setIsMutating] = useState(false);

  const refetch = useCallback(async () => {
    if (!opportunityId) {
      setActions([]);
      setLoading(false);
      return;
    }

    try {
      setError(null);
      const nextActions = await fetchFollowupActions(opportunityId);
      setActions(nextActions);
    } catch (fetchError) {
      const message =
        fetchError instanceof Error
          ? fetchError.message
          : 'Failed to load follow-up actions';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [opportunityId]);

  useEffect(() => {
    setLoading(true);
    void refetch();

    const intervalId = window.setInterval(() => {
      void refetch();
    }, FOLLOWUP_ACTIONS_POLL_INTERVAL_MS);

    return () => {
      window.clearInterval(intervalId);
    };
  }, [refetch]);

  const requireUserId = () => {
    if (!currentUser?.id) {
      throw new Error('You must be signed in to review follow-up actions.');
    }

    return currentUser.id;
  };

  const acceptAction = async (actionId: string) => {
    setIsMutating(true);
    try {
      const result = await acceptFollowupAction(actionId, requireUserId());
      if (result.execution_status === 'completed') {
        enqueueSuccessSnackBar({ message: 'Follow-up action accepted.' });
      } else {
        enqueueErrorSnackBar({
          message: result.error ?? 'Follow-up action execution failed.',
        });
      }
      await refetch();
    } catch (mutationError) {
      enqueueErrorSnackBar({
        message:
          mutationError instanceof Error
            ? mutationError.message
            : 'Failed to accept follow-up action.',
      });
    } finally {
      setIsMutating(false);
    }
  };

  const rejectAction = async (actionId: string) => {
    setIsMutating(true);
    try {
      await rejectFollowupAction(actionId, requireUserId());
      enqueueSuccessSnackBar({ message: 'Follow-up action rejected.' });
      await refetch();
    } catch (mutationError) {
      enqueueErrorSnackBar({
        message:
          mutationError instanceof Error
            ? mutationError.message
            : 'Failed to reject follow-up action.',
      });
    } finally {
      setIsMutating(false);
    }
  };

  const reviseAction = async (actionId: string, instructions: string) => {
    setIsMutating(true);
    try {
      await reviseFollowupAction(actionId, requireUserId(), instructions);
      enqueueSuccessSnackBar({
        message: 'Revision requested — a new draft is being prepared.',
      });
      await refetch();
    } catch (mutationError) {
      enqueueErrorSnackBar({
        message:
          mutationError instanceof Error
            ? mutationError.message
            : 'Failed to revise follow-up action.',
      });
    } finally {
      setIsMutating(false);
    }
  };

  return {
    actions,
    loading,
    error,
    isMutating,
    refetch,
    acceptAction,
    rejectAction,
    reviseAction,
  };
};
