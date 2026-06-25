import type {
  ExtendedUIMessage,
  ExtendedUIMessagePart,
} from 'twenty-shared/ai';

import type { BrowsingContextType } from 'src/engine/metadata-modules/ai/ai-agent/types/browsingContext.type';

export type StreamAgentChatJobData = {
  threadId: string;
  streamId: string;
  userWorkspaceId: string;
  workspaceId: string;
  messages: ExtendedUIMessage[];
  browsingContext: BrowsingContextType | null;
  // The user's IANA timezone from the browser, so the follow-up agent resolves
  // clock times ("1pm") as local and converts to UTC before booking.
  timezone?: string | null;
  modelId?: string;
  lastUserMessageText: string;
  lastUserMessageParts: ExtendedUIMessagePart[];
  hasTitle: boolean;
  existingTurnId?: string;
  conversationSizeTokens: number;
  // Set only on the external-orchestrator resume path: true = user approved the
  // paused write, false = rejected. When present, the job calls /agent/resume
  // instead of /agent/chat. The user-message persistence is also skipped.
  resume?: boolean;
};
