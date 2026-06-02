import { CombinedGraphQLErrors } from '@apollo/client/errors';
import { useApolloClient } from '@apollo/client/react';
import { useStore } from 'jotai';
import { useCallback, useState } from 'react';
import { type ExtendedUIMessage } from 'twenty-shared/ai';
import { isDefined, isValidUuid } from 'twenty-shared/utils';
import { v4 } from 'uuid';

import { AGENT_CHAT_INSTANCE_ID } from '@/ai/constants/AgentChatInstanceId';
import { AGENT_CHAT_RESTORE_EDITOR_CONTENT_EVENT_NAME } from '@/ai/constants/AgentChatRestoreEditorContentEventName';
import { AGENT_CHAT_SEND_MESSAGE_EVENT_NAME } from '@/ai/constants/AgentChatSendMessageEventName';
import { AGENT_CHAT_STOP_EVENT_NAME } from '@/ai/constants/AgentChatStopEventName';
import { STOP_AGENT_CHAT_STREAM } from '@/ai/graphql/mutations/stopAgentChatStream';
import { useOptimisticallyUnarchiveOnSend } from '@/ai/hooks/useOptimisticallyUnarchiveOnSend';
import {
  AGENT_CHAT_NEW_THREAD_DRAFT_KEY,
  agentChatDraftsByThreadIdState,
} from '@/ai/states/agentChatDraftsByThreadIdState';
import { agentChatErrorComponentFamilyState } from '@/ai/states/agentChatErrorComponentFamilyState';
import { agentChatInputState } from '@/ai/states/agentChatInputState';
import { agentChatMaskingSessionByThreadIdState } from '@/ai/states/agentChatMaskingSessionByThreadIdState';
import { agentChatMessagesComponentFamilyState } from '@/ai/states/agentChatMessagesComponentFamilyState';
import { agentChatSelectedFilesState } from '@/ai/states/agentChatSelectedFilesState';
import { agentChatUploadedFilesState } from '@/ai/states/agentChatUploadedFilesState';
import { currentAiChatThreadState } from '@/ai/states/currentAiChatThreadState';
import { maskText } from '@/ai/utils/maskText';
import { tokenPairState } from '@/auth/states/tokenPairState';
import { useListenToBrowserEvent } from '@/browser-event/hooks/useListenToBrowserEvent';
import { dispatchBrowserEvent } from '@/browser-event/utils/dispatchBrowserEvent';
import { useSnackBar } from '@/ui/feedback/snack-bar-manager/hooks/useSnackBar';
import { useAtomState } from '@/ui/utilities/state/jotai/hooks/useAtomState';
import { useAtomStateValue } from '@/ui/utilities/state/jotai/hooks/useAtomStateValue';
import { useSetAtomState } from '@/ui/utilities/state/jotai/hooks/useSetAtomState';

export const useAgentChat = (
  ensureThreadIdForSend: () => Promise<string | null>,
) => {
  const { applyOptimisticUnarchive } = useOptimisticallyUnarchiveOnSend();
  const apolloClient = useApolloClient();
  const { enqueueErrorSnackBar } = useSnackBar();
  const setCurrentAiChatThread = useSetAtomState(currentAiChatThreadState);
  const store = useStore();

  const agentChatSelectedFiles = useAtomStateValue(agentChatSelectedFilesState);

  const [, setPendingThreadIdAfterFirstSend] = useState<string | null>(null);

  const [agentChatUploadedFiles, setAgentChatUploadedFiles] = useAtomState(
    agentChatUploadedFilesState,
  );

  const [, setAgentChatInput] = useAtomState(agentChatInputState);
  const setAgentChatDraftsByThreadId = useSetAtomState(
    agentChatDraftsByThreadIdState,
  );

  const handleSendMessage = useCallback(async () => {
    const draftKey =
      store.get(currentAiChatThreadState.atom) ??
      AGENT_CHAT_NEW_THREAD_DRAFT_KEY;
    const contentToSend =
      draftKey === AGENT_CHAT_NEW_THREAD_DRAFT_KEY
        ? (
            store.get(agentChatDraftsByThreadIdState.atom)[
              AGENT_CHAT_NEW_THREAD_DRAFT_KEY
            ] ?? store.get(agentChatInputState.atom)
          ).trim()
        : store.get(agentChatInputState.atom).trim();

    if (contentToSend === '') {
      return;
    }

    const isLoading = agentChatSelectedFiles.length > 0;

    if (isLoading) {
      return;
    }

    const threadId = await ensureThreadIdForSend();

    if (!isDefined(threadId)) {
      return;
    }

    if (draftKey === AGENT_CHAT_NEW_THREAD_DRAFT_KEY) {
      setPendingThreadIdAfterFirstSend(threadId);
    }

    setAgentChatInput('');
    setAgentChatDraftsByThreadId((prev) => ({
      ...prev,
      [draftKey]: '',
    }));

    const messageId = v4();
    const optimisticMessageCreatedAt = new Date().toISOString();
    const rollbackOptimisticUnarchive = applyOptimisticUnarchive(
      threadId,
      optimisticMessageCreatedAt,
    );

    const optimisticUserMessage: ExtendedUIMessage = {
      id: messageId,
      role: 'user',
      parts: [
        { type: 'text' as const, text: contentToSend },
        ...agentChatUploadedFiles,
      ],
      metadata: {
        createdAt: optimisticMessageCreatedAt,
      },
      status: 'sent',
    };

    const messagesAtom = agentChatMessagesComponentFamilyState.atomFamily({
      instanceId: AGENT_CHAT_INSTANCE_ID,
      familyKey: { threadId },
    });
    const errorAtom = agentChatErrorComponentFamilyState.atomFamily({
      instanceId: AGENT_CHAT_INSTANCE_ID,
      familyKey: { threadId },
    });

    const currentMessages = store.get(messagesAtom);

    store.set(messagesAtom, [...currentMessages, optimisticUserMessage]);
    store.set(errorAtom, null);

    setAgentChatUploadedFiles([]);

    try {
      // The LLM is intentionally bypassed: the message is routed to our
      // text-masking endpoint (NER), and the detected entities are attached to
      // the user's own message for inline highlighting. No assistant reply.
      const token = store.get(tokenPairState.atom)
        ?.accessOrWorkspaceAgnosticToken?.token;

      if (!isDefined(token)) {
        throw new Error('Missing authentication token for text masking');
      }

      const existingSessionId = store.get(
        agentChatMaskingSessionByThreadIdState.atom,
      )[threadId];

      const { entities, sessionId } = await maskText({
        text: contentToSend,
        token,
        sessionId: existingSessionId,
      });

      store.set(agentChatMaskingSessionByThreadIdState.atom, (prev) => ({
        ...prev,
        [threadId]: sessionId,
      }));

      const latestMessages = store.get(messagesAtom);

      store.set(
        messagesAtom,
        latestMessages.map((message) =>
          message.id === messageId
            ? {
                ...message,
                metadata: {
                  ...(message.metadata ?? {
                    createdAt: optimisticMessageCreatedAt,
                  }),
                  entitySpans: entities,
                },
              }
            : message,
        ),
      );

      setPendingThreadIdAfterFirstSend((pendingId) => {
        if (isDefined(pendingId)) {
          setCurrentAiChatThread(pendingId);
        }

        return null;
      });
    } catch (error) {
      const restoredDraftKey =
        draftKey === AGENT_CHAT_NEW_THREAD_DRAFT_KEY ? threadId : draftKey;

      rollbackOptimisticUnarchive?.();

      setAgentChatInput(contentToSend);
      setAgentChatDraftsByThreadId((prev) => ({
        ...prev,
        [restoredDraftKey]: contentToSend,
        ...(draftKey === AGENT_CHAT_NEW_THREAD_DRAFT_KEY
          ? { [AGENT_CHAT_NEW_THREAD_DRAFT_KEY]: '' }
          : {}),
      }));

      const latestMessages = store.get(messagesAtom);

      store.set(
        messagesAtom,
        latestMessages.filter((message) => message.id !== messageId),
      );

      store.set(
        errorAtom,
        CombinedGraphQLErrors.is(error) || error instanceof Error
          ? error
          : new Error('An unexpected error occurred'),
      );

      dispatchBrowserEvent(AGENT_CHAT_RESTORE_EDITOR_CONTENT_EVENT_NAME, {
        content: contentToSend,
      });

      if (draftKey === AGENT_CHAT_NEW_THREAD_DRAFT_KEY) {
        setCurrentAiChatThread(threadId);
      }

      setPendingThreadIdAfterFirstSend(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    store,
    agentChatSelectedFiles,
    ensureThreadIdForSend,
    setAgentChatInput,
    agentChatUploadedFiles,
    setAgentChatUploadedFiles,
    setAgentChatDraftsByThreadId,
    setCurrentAiChatThread,
    applyOptimisticUnarchive,
  ]);

  useListenToBrowserEvent({
    eventName: AGENT_CHAT_SEND_MESSAGE_EVENT_NAME,
    onBrowserEvent: handleSendMessage,
  });

  const handleStop = useCallback(async () => {
    const threadId = store.get(currentAiChatThreadState.atom);

    if (!isDefined(threadId) || !isValidUuid(threadId)) {
      return;
    }

    try {
      await apolloClient.mutate({
        mutation: STOP_AGENT_CHAT_STREAM,
        variables: { threadId },
      });
    } catch (error) {
      enqueueErrorSnackBar({
        apolloError: CombinedGraphQLErrors.is(error) ? error : undefined,
      });
    }
  }, [store, apolloClient, enqueueErrorSnackBar]);

  useListenToBrowserEvent({
    eventName: AGENT_CHAT_STOP_EVENT_NAME,
    onBrowserEvent: handleStop,
  });

  return {
    handleSendMessage,
    handleStop,
  };
};
