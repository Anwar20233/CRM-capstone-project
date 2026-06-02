import { type DataMessagePart } from '@/ai/types/DataMessagePart';
import { type MaskedEntity } from '@/ai/types/MaskedEntity';
import { type Nullable } from '@/types';
import { type UIMessage } from 'ai';

export type AiChatUsageMetadata = {
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens: number;
  inputCredits: number;
  outputCredits: number;
  conversationSize: number;
};

export type AiChatModelMetadata = {
  contextWindowTokens: number;
};

type Metadata = {
  createdAt: string;
  usage?: AiChatUsageMetadata;
  model?: AiChatModelMetadata;
  // Entities detected by the text-masking service in the user's message, used to
  // highlight spans and reveal masked values on hover.
  entitySpans?: MaskedEntity[];
};

export type ExtendedUIMessage = UIMessage<Metadata, DataMessagePart> & {
  threadId?: Nullable<string>;
  status?: 'queued' | 'sent';
};
