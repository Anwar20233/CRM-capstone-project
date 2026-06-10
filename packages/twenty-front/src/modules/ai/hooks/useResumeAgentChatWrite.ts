import { useMutation } from '@apollo/client/react';
import { useState } from 'react';

import { RESUME_AGENT_CHAT_STREAM } from '@/ai/graphql/mutations/resumeAgentChatStream';
import { useSnackBar } from '@/ui/feedback/snack-bar-manager/hooks/useSnackBar';

// Resumes a paused external-orchestrator turn after the user approves or
// rejects a high-risk write (the `data-write-confirmation` part). The streamed
// continuation arrives over the existing agent-chat subscription, so this hook
// only fires the mutation and tracks in-flight state for the approval card.
export const useResumeAgentChatWrite = () => {
  const { enqueueErrorSnackBar } = useSnackBar();
  const [isResolving, setIsResolving] = useState(false);

  const [resumeMutation] = useMutation(RESUME_AGENT_CHAT_STREAM);

  const resolveWrite = async (threadId: string, approved: boolean) => {
    setIsResolving(true);
    try {
      await resumeMutation({ variables: { threadId, approved } });
    } catch (error) {
      enqueueErrorSnackBar({
        message:
          error instanceof Error
            ? error.message
            : 'Failed to resume the agent.',
      });
    } finally {
      setIsResolving(false);
    }
  };

  return { resolveWrite, isResolving };
};
