import { useCallback, useEffect, useState } from 'react';

import { fetchFollowupProfile } from '@/followup-intelligence/services/followup-api';
import { type FollowupProfile } from '@/followup-intelligence/types/followup-action';

type UseFollowupProfileResult = {
  profile: FollowupProfile | null;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
};

export const useFollowupProfile = (
  opportunityId: string,
): UseFollowupProfileResult => {
  const [profile, setProfile] = useState<FollowupProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    if (!opportunityId) {
      setProfile(null);
      setLoading(false);
      return;
    }

    try {
      setError(null);
      const nextProfile = await fetchFollowupProfile(opportunityId);
      setProfile(nextProfile);
    } catch (fetchError) {
      const message =
        fetchError instanceof Error
          ? fetchError.message
          : 'Failed to load follow-up profile';
      setError(message);
      setProfile(null);
    } finally {
      setLoading(false);
    }
  }, [opportunityId]);

  useEffect(() => {
    setLoading(true);
    void refetch();
  }, [refetch]);

  return { profile, loading, error, refetch };
};
