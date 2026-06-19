import { type BrowsingContext } from '@/ai/types/BrowsingContext';
import { createAtomState } from '@/ui/utilities/state/jotai/utils/createAtomState';

// When set, the chat sends this browsing context instead of deriving it from the
// global context store. Surfaces (like the opportunity Follow-Up tab) that embed
// the chat for a specific record set this so the turn is deterministically
// scoped to that record — and routed to the matching agent — regardless of what
// the global context store currently holds.
export const aiChatForcedBrowsingContextState =
  createAtomState<BrowsingContext | null>({
    key: 'ai/aiChatForcedBrowsingContextState',
    defaultValue: null,
  });
