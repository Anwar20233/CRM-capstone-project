import { Logger, Scope } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';

import { createUIMessageStream, generateId } from 'ai';
import type {
  CodeExecutionData,
  ExtendedUIMessage,
  ExtendedUIMessagePart,
} from 'twenty-shared/ai';
import { Repository } from 'typeorm';

import { TwentyConfigService } from 'src/engine/core-modules/twenty-config/twenty-config.service';
import {
  AgentOrchestratorClientService,
  type FollowupChatContext,
  type OrchestratorResult,
} from 'src/engine/metadata-modules/ai/ai-chat/services/agent-orchestrator-client.service';

import { Process } from 'src/engine/core-modules/message-queue/decorators/process.decorator';
import { Processor } from 'src/engine/core-modules/message-queue/decorators/processor.decorator';
import { MessageQueue } from 'src/engine/core-modules/message-queue/message-queue.constants';
import { toDisplayCredits } from 'src/engine/core-modules/usage/utils/to-display-credits.util';
import { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';
import { AgentMessageRole } from 'src/engine/metadata-modules/ai/ai-agent-execution/entities/agent-message.entity';
import { computeCostBreakdown } from 'src/engine/metadata-modules/ai/ai-billing/utils/compute-cost-breakdown.util';
import { convertDollarsToBillingCredits } from 'src/engine/metadata-modules/ai/ai-billing/utils/convert-dollars-to-billing-credits.util';
import { extractCacheCreationTokens } from 'src/engine/metadata-modules/ai/ai-billing/utils/extract-cache-creation-tokens.util';
import { AgentChatThreadEntity } from 'src/engine/metadata-modules/ai/ai-chat/entities/agent-chat-thread.entity';
import { AgentChatCancelSubscriberService } from 'src/engine/metadata-modules/ai/ai-chat/services/agent-chat-cancel-subscriber.service';
import { AgentChatEventPublisherService } from 'src/engine/metadata-modules/ai/ai-chat/services/agent-chat-event-publisher.service';
import { AgentChatStreamingService } from 'src/engine/metadata-modules/ai/ai-chat/services/agent-chat-streaming.service';
import { AgentChatService } from 'src/engine/metadata-modules/ai/ai-chat/services/agent-chat.service';
import { ChatExecutionService } from 'src/engine/metadata-modules/ai/ai-chat/services/chat-execution.service';
import { getCancelChannel } from 'src/engine/metadata-modules/ai/ai-chat/utils/get-cancel-channel.util';
import type { AiModelConfig } from 'src/engine/metadata-modules/ai/ai-models/types/ai-model-config.type';

import { STREAM_AGENT_CHAT_JOB_NAME } from './stream-agent-chat-job-name.constant';
import { type StreamAgentChatJobData } from './stream-agent-chat-job.types';

export { STREAM_AGENT_CHAT_JOB_NAME, type StreamAgentChatJobData };

// When the chat is opened on an opportunity record page, scope the turn to the
// deal-aware Follow-Up agent. The browsing context (carried from the front-end)
// is the reliable signal; tab ids are random per workspace so we key off the
// opportunity record itself.
const getFollowupChatContext = (
  data: StreamAgentChatJobData,
): FollowupChatContext | undefined => {
  const browsingContext = data.browsingContext;

  if (
    browsingContext?.type === 'recordPage' &&
    browsingContext.objectNameSingular === 'opportunity'
  ) {
    return {
      opportunityId: browsingContext.recordId,
      workspaceId: data.workspaceId,
      userId: data.userWorkspaceId,
      timezone: data.timezone ?? undefined,
    };
  }

  return undefined;
};

@Processor({ queueName: MessageQueue.aiStreamQueue, scope: Scope.REQUEST })
export class StreamAgentChatJob {
  private readonly logger = new Logger(StreamAgentChatJob.name);

  constructor(
    @InjectRepository(AgentChatThreadEntity)
    private readonly threadRepository: Repository<AgentChatThreadEntity>,
    @InjectRepository(WorkspaceEntity)
    private readonly workspaceRepository: Repository<WorkspaceEntity>,
    private readonly agentChatService: AgentChatService,
    private readonly chatExecutionService: ChatExecutionService,
    private readonly eventPublisherService: AgentChatEventPublisherService,
    private readonly cancelSubscriberService: AgentChatCancelSubscriberService,
    private readonly agentChatStreamingService: AgentChatStreamingService,
    private readonly agentOrchestratorClientService: AgentOrchestratorClientService,
    private readonly twentyConfigService: TwentyConfigService,
  ) {}

  @Process(STREAM_AGENT_CHAT_JOB_NAME)
  async handle(data: StreamAgentChatJobData): Promise<void> {
    const workspace = await this.workspaceRepository.findOne({
      where: { id: data.workspaceId },
    });

    if (!workspace) {
      this.logger.error(`Workspace ${data.workspaceId} not found`);
      await this.eventPublisherService.publish({
        threadId: data.threadId,
        workspaceId: data.workspaceId,
        event: {
          type: 'stream-error',
          code: 'WORKSPACE_NOT_FOUND',
          message: `Workspace ${data.workspaceId} not found`,
        },
      });

      return;
    }

    const abortController = new AbortController();
    const cancelChannel = getCancelChannel(data.threadId);

    await this.cancelSubscriberService.subscribe(cancelChannel, () => {
      abortController.abort();
    });

    try {
      await this.executeStream(data, workspace, abortController.signal);
    } catch (error) {
      this.logger.error(
        `Stream ${data.streamId} failed: ${error instanceof Error ? error.message : String(error)}`,
      );
      await this.eventPublisherService
        .publish({
          threadId: data.threadId,
          workspaceId: data.workspaceId,
          event: {
            type: 'stream-error',
            code: 'STREAM_EXECUTION_FAILED',
            message:
              error instanceof Error
                ? error.message
                : 'Stream execution failed',
          },
        })
        .catch(() => {});
      throw error;
    } finally {
      await this.cancelSubscriberService.unsubscribe(cancelChannel);
      await this.threadRepository
        .createQueryBuilder()
        .update(AgentChatThreadEntity)
        .set({ activeStreamId: null })
        .where('id = :id AND "activeStreamId" = :streamId', {
          id: data.threadId,
          streamId: data.streamId,
        })
        .execute()
        .catch(() => {});

      if (!abortController.signal.aborted) {
        await this.agentChatStreamingService
          .flushNextQueuedMessage(
            data.threadId,
            data.userWorkspaceId,
            data.workspaceId,
            data.hasTitle,
          )
          .catch((error) => {
            this.logger.error(
              `Failed to flush queued message for thread ${data.threadId}: ${error instanceof Error ? error.message : String(error)}`,
            );
          });
      }
    }
  }

  private async executeStream(
    data: StreamAgentChatJobData,
    workspace: WorkspaceEntity,
    abortSignal: AbortSignal,
  ): Promise<void> {
    // When processing a promoted queued message, the user message already
    // exists in the DB with a turn — skip persisting it again.
    const userMessagePromise = data.existingTurnId
      ? Promise.resolve({ turnId: data.existingTurnId })
      : this.agentChatService.addMessage({
          threadId: data.threadId,
          uiMessage: {
            role: AgentMessageRole.USER,
            parts: data.lastUserMessageParts.filter(
              (part): part is ExtendedUIMessagePart =>
                part.type === 'text' || part.type === 'file',
            ),
          },
          workspaceId: data.workspaceId,
        });

    userMessagePromise.catch(() => {});

    const useExternalOrchestrator = this.twentyConfigService.get(
      'AI_AGENT_USE_EXTERNAL_ORCHESTRATOR',
    );

    // The built-in Node agent names threads with its own fast model. The
    // external path can't — the Node side has no AI provider configured — so it
    // asks the Python orchestrator for the title instead (see onTitle below).
    const titlePromise =
      data.hasTitle || useExternalOrchestrator
        ? Promise.resolve(null)
        : this.agentChatService
            .generateTitleIfNeeded({
              threadId: data.threadId,
              messageContent: data.lastUserMessageText,
              workspaceId: data.workspaceId,
            })
            .catch(() => null);

    // Route through the external Python orchestrator (with its human-in-the-loop
    // write-approval flow) when enabled; otherwise use the built-in Node agent.
    if (useExternalOrchestrator) {
      await this.buildAndPublishExternalAgentStream({
        data,
        userMessagePromise,
        titlePromise,
        abortSignal,
      });

      return;
    }

    await this.buildAndPublishStream({
      workspace,
      data,
      userMessagePromise,
      titlePromise,
      abortSignal,
    });
  }

  // Drives the UI stream from the external Python orchestrator instead of the
  // AI SDK streamText loop. Emits a single assistant message containing either
  // a text part (normal answer) or a `data-write-confirmation` part (a tier-3
  // write paused for the user's approval — see write_gate.py). The approval
  // card on the client resumes the flow via the resumeAgentChatStream mutation.
  private async buildAndPublishExternalAgentStream({
    data,
    userMessagePromise,
    titlePromise,
    abortSignal,
  }: {
    data: StreamAgentChatJobData;
    userMessagePromise: Promise<{ turnId: string | null }>;
    titlePromise: Promise<string | null>;
    abortSignal: AbortSignal;
  }): Promise<void> {
    const isResume = data.resume !== undefined;

    // Populated as the stream runs; read after the UI stream drains to decide
    // what to persist (a single text part, or the pending approval card).
    // Reassigned inside the async execute callback below; the cast keeps the
    // outer-scope type the full union (not narrowed to the 'response' literal)
    // so the interrupt branch is still reachable after the stream drains.
    let result = { type: 'response', response: '' } as OrchestratorResult;

    const uiStream = createUIMessageStream<ExtendedUIMessage>({
      execute: async ({ writer }) => {
        const generatedTitle = await titlePromise.catch(() => null);

        if (generatedTitle) {
          writer.write({
            type: 'data-thread-title' as const,
            id: `thread-title-${data.threadId}`,
            data: { title: generatedTitle },
          });
        }

        // One stable id per part so repeated writes reconcile in place: the
        // routing-status line updates as stages change, and the text part grows
        // delta by delta into a typed-out answer.
        const routingStatusId = `routing-status-${data.threadId}`;
        const textId = `text-${generateId()}`;
        let textStarted = false;

        const handlers = {
          onStage: (text: string) => {
            if (abortSignal.aborted) {
              return;
            }
            writer.write({
              type: 'data-routing-status' as const,
              id: routingStatusId,
              data: { text, state: 'loading' },
            });
          },
          onToken: (delta: string) => {
            if (abortSignal.aborted) {
              return;
            }
            if (!textStarted) {
              writer.write({ type: 'text-start', id: textId });
              textStarted = true;
            }
            writer.write({ type: 'text-delta', id: textId, delta });
          },
          // The Python orchestrator names the thread on its first turn; surface
          // the title to the client live and persist it for the sidebar.
          onTitle: (title: string) => {
            const trimmed = title.trim();

            if (trimmed.length === 0) {
              return;
            }

            writer.write({
              type: 'data-thread-title' as const,
              id: `thread-title-${data.threadId}`,
              data: { title: trimmed },
            });

            void this.agentChatService
              .persistGeneratedTitle({
                threadId: data.threadId,
                title: trimmed,
              })
              .catch(() => {});
          },
        };

        result = isResume
          ? await this.agentOrchestratorClientService.resumeStream(
              data.threadId,
              data.resume as boolean,
              handlers,
            )
          : await this.agentOrchestratorClientService.chatStream(
              data.threadId,
              data.lastUserMessageText,
              handlers,
              getFollowupChatContext(data),
              !data.hasTitle,
            );

        if (result.type === 'interrupt') {
          writer.write({
            type: 'data-write-confirmation' as const,
            id: `write-confirmation-${generateId()}`,
            data: {
              threadId: data.threadId,
              action: result.interrupt.action,
              args: result.interrupt.args,
              summary: result.interrupt.summary,
              status: 'pending',
            },
          } as never);
        } else if (textStarted) {
          writer.write({ type: 'text-end', id: textId });
        } else if (result.response.length > 0) {
          // No tokens streamed (edge case) — emit the whole answer at once.
          writer.write({ type: 'text-start', id: textId });
          writer.write({
            type: 'text-delta',
            id: textId,
            delta: result.response,
          });
          writer.write({ type: 'text-end', id: textId });
        }
      },
    });

    for await (const chunk of uiStream) {
      await this.eventPublisherService.publish({
        threadId: data.threadId,
        workspaceId: data.workspaceId,
        event: {
          type: 'stream-chunk',
          chunk: chunk as Record<string, unknown>,
        },
      });
    }

    if (abortSignal.aborted) {
      return;
    }

    // Persist only the durable outcome — the pending approval card, or the
    // final text. Routing-status stages are live-only progress, not persisted.
    const assistantParts: ExtendedUIMessagePart[] =
      result.type === 'interrupt'
        ? [
            {
              type: 'data-write-confirmation',
              id: `write-confirmation-${generateId()}`,
              data: {
                threadId: data.threadId,
                action: result.interrupt.action,
                args: result.interrupt.args,
                summary: result.interrupt.summary,
                status: 'pending',
              },
            } as ExtendedUIMessagePart,
          ]
        : [{ type: 'text', text: result.response }];

    // Persist the assistant turn so reopening the thread shows it.
    const threadStatus = await this.threadRepository.findOne({
      where: { id: data.threadId },
      select: ['id', 'deletedAt'],
    });

    if (threadStatus && !threadStatus.deletedAt) {
      const userMessage = await userMessagePromise;

      await this.agentChatService.addMessage({
        threadId: data.threadId,
        uiMessage: {
          role: AgentMessageRole.ASSISTANT,
          parts: assistantParts,
        },
        turnId: userMessage.turnId ?? undefined,
        workspaceId: data.workspaceId,
      });
    }

    await this.eventPublisherService.publish({
      threadId: data.threadId,
      workspaceId: data.workspaceId,
      event: { type: 'message-persisted', messageId: data.threadId },
    });
  }

  private async buildAndPublishStream({
    workspace,
    data,
    userMessagePromise,
    titlePromise,
    abortSignal,
  }: {
    workspace: WorkspaceEntity;
    data: StreamAgentChatJobData;
    userMessagePromise: Promise<{ turnId: string | null }>;
    titlePromise: Promise<string | null>;
    abortSignal: AbortSignal;
  }): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      let streamUsage = {
        inputTokens: 0,
        outputTokens: 0,
        inputCredits: 0,
        outputCredits: 0,
        cacheReadTokens: 0,
      };
      let lastStepConversationSize = 0;
      let totalCacheCreationTokens = 0;
      let streamError: unknown;
      let checkHasNoMoreAvailableCredits: () => boolean = () => false;

      // onFinish fires before the uiStream is fully drained. We use this
      // promise to coordinate: the IIFE waits for DB persist to complete
      // before publishing message-persisted (after all chunks).
      let resolveStreamFinished: () => void;
      const streamFinishedPromise = new Promise<void>((res) => {
        resolveStreamFinished = res;
      });

      abortSignal.addEventListener('abort', () => resolve(), { once: true });

      const uiStream = createUIMessageStream<ExtendedUIMessage>({
        execute: async ({ writer }) => {
          const onCodeExecutionUpdate = (
            codeExecutionData: CodeExecutionData,
          ) => {
            writer.write({
              type: 'data-code-execution' as const,
              id: `code-execution-${codeExecutionData.executionId}`,
              data: codeExecutionData,
            });
          };

          const onCompaction = () => {
            writer.write({
              type: 'data-compaction' as const,
              id: `compaction-${data.threadId}`,
              data: {},
            });
          };

          const { stream, modelConfig, hasNoMoreAvailableCredits } =
            await this.chatExecutionService.streamChat({
              workspace,
              userWorkspaceId: data.userWorkspaceId,
              messages: data.messages,
              browsingContext: data.browsingContext,
              modelId: data.modelId,
              onCodeExecutionUpdate,
              onCompaction,
              abortSignal,
              conversationSizeTokens: data.conversationSizeTokens,
            });

          checkHasNoMoreAvailableCredits = hasNoMoreAvailableCredits;

          const titleWritePromise = titlePromise.then((generatedTitle) => {
            if (generatedTitle) {
              writer.write({
                type: 'data-thread-title' as const,
                id: `thread-title-${data.threadId}`,
                data: { title: generatedTitle },
              });
            }
          });

          writer.merge(
            stream.toUIMessageStream({
              onError: (error) => {
                streamError = error;

                return error instanceof Error ? error.message : String(error);
              },
              sendStart: false,
              messageMetadata: ({ part }) => {
                return this.computeMessageMetadata({
                  part,
                  modelConfig,
                  lastStepConversationSize,
                  totalCacheCreationTokens,
                  onUpdateUsage: (usage) => {
                    streamUsage = usage;
                  },
                  onUpdateConversationSize: (size) => {
                    lastStepConversationSize = size;
                  },
                  onUpdateCacheCreationTokens: (tokens) => {
                    totalCacheCreationTokens = tokens;
                  },
                });
              },
              onFinish: async ({ responseMessage }) => {
                try {
                  await this.handleStreamFinish({
                    responseMessage,
                    threadId: data.threadId,
                    workspaceId: data.workspaceId,
                    streamUsage,
                    lastStepConversationSize,
                    totalCacheCreationTokens,
                    modelConfig,
                    userMessagePromise,
                  });
                  await titleWritePromise;
                  resolveStreamFinished();
                } catch (error) {
                  reject(error);
                }
              },
              sendReasoning: true,
            }),
          );
        },
      });

      // Publish all chunks first, then signal completion. This guarantees
      // message-persisted arrives after every stream-chunk on the client.
      void (async () => {
        try {
          for await (const chunk of uiStream) {
            await this.eventPublisherService.publish({
              threadId: data.threadId,
              workspaceId: data.workspaceId,
              event: {
                type: 'stream-chunk',
                chunk: chunk as Record<string, unknown>,
              },
            });
          }

          await streamFinishedPromise;

          if (streamError) {
            reject(streamError);
          } else if (checkHasNoMoreAvailableCredits()) {
            await this.eventPublisherService.publish({
              threadId: data.threadId,
              workspaceId: data.workspaceId,
              event: { type: 'credits-exhausted' },
            });
            resolve();
          } else {
            await this.eventPublisherService.publish({
              threadId: data.threadId,
              workspaceId: data.workspaceId,
              event: { type: 'message-persisted', messageId: data.threadId },
            });
            resolve();
          }
        } catch (error) {
          reject(error);
        }
      })();
    });
  }

  private computeMessageMetadata({
    part,
    modelConfig,
    lastStepConversationSize,
    totalCacheCreationTokens,
    onUpdateUsage,
    onUpdateConversationSize,
    onUpdateCacheCreationTokens,
  }: {
    part: {
      type: string;
      usage?: {
        inputTokens?: number;
        inputTokenDetails?: { cacheReadTokens?: number };
      };
      totalUsage?: {
        inputTokens?: number;
        outputTokens?: number;
        inputTokenDetails?: { cacheReadTokens?: number };
        outputTokenDetails?: { reasoningTokens?: number };
      };
      providerMetadata?: Record<string, Record<string, unknown> | undefined>;
    };
    modelConfig: AiModelConfig;
    lastStepConversationSize: number;
    totalCacheCreationTokens: number;
    onUpdateUsage: (usage: {
      inputTokens: number;
      outputTokens: number;
      inputCredits: number;
      outputCredits: number;
      cacheReadTokens: number;
    }) => void;
    onUpdateConversationSize: (size: number) => void;
    onUpdateCacheCreationTokens: (tokens: number) => void;
  }) {
    if (part.type === 'finish-step') {
      const stepInput = part.usage?.inputTokens ?? 0;
      const stepCached = part.usage?.inputTokenDetails?.cacheReadTokens ?? 0;
      const stepCacheCreation = extractCacheCreationTokens(
        part.providerMetadata,
      );

      onUpdateCacheCreationTokens(totalCacheCreationTokens + stepCacheCreation);
      onUpdateConversationSize(stepInput + stepCached + stepCacheCreation);
    }

    if (part.type === 'finish') {
      const breakdown = computeCostBreakdown(modelConfig, {
        inputTokens: part.totalUsage?.inputTokens,
        outputTokens: part.totalUsage?.outputTokens,
        cachedInputTokens: part.totalUsage?.inputTokenDetails?.cacheReadTokens,
        reasoningTokens: part.totalUsage?.outputTokenDetails?.reasoningTokens,
        cacheCreationTokens: totalCacheCreationTokens,
      });

      const inputCredits = Math.round(
        convertDollarsToBillingCredits(breakdown.inputCostInDollars),
      );
      const outputCredits = Math.round(
        convertDollarsToBillingCredits(breakdown.outputCostInDollars),
      );

      onUpdateUsage({
        inputTokens: breakdown.tokenCounts.totalInputTokens,
        outputTokens: part.totalUsage?.outputTokens ?? 0,
        inputCredits,
        outputCredits,
        cacheReadTokens: breakdown.tokenCounts.cachedInputTokens,
      });

      return {
        createdAt: new Date().toISOString(),
        usage: {
          inputTokens: breakdown.tokenCounts.totalInputTokens,
          outputTokens: part.totalUsage?.outputTokens ?? 0,
          cachedInputTokens: breakdown.tokenCounts.cachedInputTokens,
          inputCredits: toDisplayCredits(inputCredits),
          outputCredits: toDisplayCredits(outputCredits),
          conversationSize: lastStepConversationSize,
        },
        model: {
          contextWindowTokens: modelConfig.contextWindowTokens,
        },
      };
    }

    return undefined;
  }

  private async handleStreamFinish({
    responseMessage,
    threadId,
    workspaceId,
    streamUsage,
    lastStepConversationSize,
    totalCacheCreationTokens,
    modelConfig,
    userMessagePromise,
  }: {
    responseMessage: Omit<ExtendedUIMessage, 'id'>;
    threadId: string;
    workspaceId: string;
    streamUsage: {
      inputTokens: number;
      outputTokens: number;
      inputCredits: number;
      outputCredits: number;
      cacheReadTokens: number;
    };
    lastStepConversationSize: number;
    totalCacheCreationTokens: number;
    modelConfig: AiModelConfig;
    userMessagePromise: Promise<{ turnId: string | null }>;
  }): Promise<void> {
    if (responseMessage.parts.length === 0) {
      return;
    }

    const threadStatus = await this.threadRepository.findOne({
      where: { id: threadId },
      select: ['id', 'deletedAt'],
    });

    if (!threadStatus || threadStatus.deletedAt) {
      return;
    }

    const userMessage = await userMessagePromise;

    await this.agentChatService.addMessage({
      threadId,
      uiMessage: responseMessage,
      turnId: userMessage.turnId ?? undefined,
      workspaceId,
    });

    await this.threadRepository.update(threadId, {
      totalInputTokens: () => `"totalInputTokens" + ${streamUsage.inputTokens}`,
      totalOutputTokens: () =>
        `"totalOutputTokens" + ${streamUsage.outputTokens}`,
      totalInputCredits: () =>
        `"totalInputCredits" + ${streamUsage.inputCredits}`,
      totalOutputCredits: () =>
        `"totalOutputCredits" + ${streamUsage.outputCredits}`,
      totalCacheReadTokens: () =>
        `"totalCacheReadTokens" + ${streamUsage.cacheReadTokens}`,
      totalCacheCreationTokens: () =>
        `"totalCacheCreationTokens" + ${totalCacheCreationTokens}`,
      contextWindowTokens: modelConfig.contextWindowTokens,
      conversationSize: lastStepConversationSize,
    });
  }
}
