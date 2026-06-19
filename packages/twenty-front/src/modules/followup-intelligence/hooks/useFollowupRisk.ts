import { useEffect, useState } from 'react';

import { fetchFollowupRisk } from '@/followup-intelligence/services/followup-api';
import { type FollowupRisk } from '@/followup-intelligence/types/followup-action';

type UseFollowupRiskResult = {
  risk: FollowupRisk | null;
  loading: boolean;
  error: string | null;
};

// Reads the daily-computed risk score from the DB (never recomputed at runtime).
export const useFollowupRisk = (
  opportunityId: string,
): UseFollowupRiskResult => {
  const [risk, setRisk] = useState<FollowupRisk | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      if (!opportunityId) {
        setRisk(null);
        setLoading(false);
        return;
      }

      setLoading(true);
      setError(null);
      try {
        const nextRisk = await fetchFollowupRisk(opportunityId);
        if (!cancelled) {
          setRisk(nextRisk);
        }
      } catch (fetchError) {
        if (!cancelled) {
          setError(
            fetchError instanceof Error
              ? fetchError.message
              : 'Failed to load risk score',
          );
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void load();

    return () => {
      cancelled = true;
    };
  }, [opportunityId]);

  return { risk, loading, error };
};
