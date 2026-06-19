import { useEffect } from 'react';
import { isValidUuid } from 'twenty-shared/utils';

import { aiChatForcedBrowsingContextState } from '@/ai/states/aiChatForcedBrowsingContextState';
import { currentAiChatThreadState } from '@/ai/states/currentAiChatThreadState';
import { followupChatThreadSelectionState } from '@/followup-intelligence/states/followupChatThreadSelectionState';
import { useAtomStateValue } from '@/ui/utilities/state/jotai/hooks/useAtomStateValue';
import { useSetAtomState } from '@/ui/utilities/state/jotai/hooks/useSetAtomState';

// "Deal drives the AI chat": each opportunity gets its own follow-up thread.
// We persist the opportunity → threadId mapping in localStorage so reopening a
// deal restores its conversation. The thread itself lives server-side; this is
// just the per-deal pointer the embedded chat selects on mount.
// Versioned key so any mapping poisoned by an earlier capture bug is dropped.
const STORAGE_KEY = 'followup-chat-thread-by-opportunity-v3';

const readMap = (): Record<string, string> => {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
};

const writeMap = (map: Record<string, string>) => {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Ignore quota/availability errors — continuity is best-effort.
  }
};

export const useFollowupChatThread = (opportunityId: string) => {
  const setCurrentAiChatThread = useSetAtomState(currentAiChatThreadState);
  const currentAiChatThread = useAtomStateValue(currentAiChatThreadState);
  const setAiChatForcedBrowsingContext = useSetAtomState(
    aiChatForcedBrowsingContextState,
  );
  const followupChatThreadSelection = useAtomStateValue(
    followupChatThreadSelectionState,
  );
  const setFollowupChatThreadSelection = useSetAtomState(
    followupChatThreadSelectionState,
  );

  // Force every chat turn from this tab to carry the opportunity, so the server
  // always routes to the deal-aware Follow-Up agent for this deal — never the
  // generic orchestrator, regardless of whether the deal has pending tasks.
  useEffect(() => {
    if (!opportunityId) {
      return;
    }

    setAiChatForcedBrowsingContext({
      type: 'recordPage',
      objectNameSingular: 'opportunity',
      recordId: opportunityId,
    });

    return () => setAiChatForcedBrowsingContext(null);
  }, [opportunityId, setAiChatForcedBrowsingContext]);

  // When the deal changes, select its saved thread (or a fresh draft) and record
  // the baseline. We intentionally do not persist here — at this point
  // currentAiChatThread still holds the previous deal's thread.
  useEffect(() => {
    if (!opportunityId) {
      return;
    }

    const savedThreadId = readMap()[opportunityId] ?? null;
    setCurrentAiChatThread(savedThreadId);
    // If the deal already has a thread, lock immediately so we never re-capture.
    setFollowupChatThreadSelection({
      opportunityId,
      baselineThreadId: savedThreadId,
      locked: savedThreadId !== null,
    });
  }, [opportunityId, setCurrentAiChatThread, setFollowupChatThreadSelection]);

  // Persist this deal's thread exactly once — the first time a fresh draft
  // becomes a real thread (after the first send) — then lock. Later changes to
  // the global current thread (e.g. opening another chat in the side panel) are
  // ignored, so the deal's mapping can't be poisoned by the orchestrator thread.
  useEffect(() => {
    if (
      followupChatThreadSelection.locked ||
      followupChatThreadSelection.opportunityId !== opportunityId ||
      currentAiChatThread === null ||
      !isValidUuid(currentAiChatThread) ||
      currentAiChatThread === followupChatThreadSelection.baselineThreadId
    ) {
      return;
    }

    const map = readMap();
    if (map[opportunityId] !== currentAiChatThread) {
      writeMap({ ...map, [opportunityId]: currentAiChatThread });
    }
    setFollowupChatThreadSelection({
      opportunityId,
      baselineThreadId: currentAiChatThread,
      locked: true,
    });
  }, [
    opportunityId,
    currentAiChatThread,
    followupChatThreadSelection,
    setFollowupChatThreadSelection,
  ]);
};
