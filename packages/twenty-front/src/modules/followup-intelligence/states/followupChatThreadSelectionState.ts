import { createAtomState } from '@/ui/utilities/state/jotai/utils/createAtomState';

// Bookkeeping for the per-deal chat thread selection. `opportunityId` is the
// deal we last selected a thread for; `baselineThreadId` is the thread we set at
// that moment. A newly created thread is one that differs from the baseline
// while still on the same deal — which is how we know to persist it (and avoid
// mis-saving the stale pre-switch thread during a deal change). `locked` is set
// once the deal has a thread, so later global-thread changes (e.g. the user
// opening the orchestrator chat in the side panel while this panel stays
// mounted) are never mis-captured as this deal's thread.
export type FollowupChatThreadSelection = {
  opportunityId: string | null;
  baselineThreadId: string | null;
  locked: boolean;
};

export const followupChatThreadSelectionState =
  createAtomState<FollowupChatThreadSelection>({
    key: 'followup/followupChatThreadSelectionState',
    defaultValue: {
      opportunityId: null,
      baselineThreadId: null,
      locked: false,
    },
  });
