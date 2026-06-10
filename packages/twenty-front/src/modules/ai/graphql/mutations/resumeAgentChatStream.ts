import { gql } from '@apollo/client';

export const RESUME_AGENT_CHAT_STREAM = gql`
  mutation ResumeAgentChatStream($threadId: UUID!, $approved: Boolean!) {
    resumeAgentChatStream(threadId: $threadId, approved: $approved)
  }
`;
