import { gql } from '@apollo/client';

export const SEND_CHAT_MESSAGE = gql`
  mutation SendChatMessage(
    $threadId: UUID!
    $text: String!
    $messageId: UUID!
    $browsingContext: JSON
    $timezone: String
    $modelId: String
    $fileIds: [UUID!]
  ) {
    sendChatMessage(
      threadId: $threadId
      text: $text
      messageId: $messageId
      browsingContext: $browsingContext
      timezone: $timezone
      modelId: $modelId
      fileIds: $fileIds
    ) {
      messageId
      queued
      streamId
    }
  }
`;
