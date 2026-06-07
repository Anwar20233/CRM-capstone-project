import { createAtomState } from '@/ui/utilities/state/jotai/utils/createAtomState';

// Maps a chat threadId to the masking sessionId returned by the text-masking
// endpoint, so the session's reverse map stays consistent within a thread
// across successive messages.
export const agentChatMaskingSessionByThreadIdState = createAtomState<
  Record<string, string>
>({
  key: 'agentChatMaskingSessionByThreadIdState',
  defaultValue: {},
});
