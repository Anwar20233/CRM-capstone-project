import { Injectable, Logger } from '@nestjs/common';

import { isDefined } from 'twenty-shared/utils';

import { SecureHttpClientService } from 'src/engine/core-modules/secure-http-client/secure-http-client.service';
import { TwentyConfigService } from 'src/engine/core-modules/twenty-config/twenty-config.service';

// A high-risk write the Python orchestrator paused on, awaiting user approval.
export type OrchestratorInterrupt = {
  action: string;
  args: Record<string, unknown>;
  summary: string;
};

// Normalised result of a /agent/chat or /agent/resume call.
export type OrchestratorResult =
  | { type: 'response'; response: string }
  | { type: 'interrupt'; interrupt: OrchestratorInterrupt };

// Live callbacks fired while a streaming turn runs: stage updates (progress
// labels) and answer tokens. The returned OrchestratorResult is the terminal
// outcome (full text, already accumulated, or an interrupt).
export type OrchestratorStreamHandlers = {
  onStage?: (text: string) => void;
  onToken?: (text: string) => void;
};

// One NDJSON event emitted by the Python /agent/*/stream endpoints.
type OrchestratorStreamEvent =
  | { kind: 'stage'; text: string }
  | { kind: 'token'; text: string }
  | { kind: 'interrupt'; interrupt: OrchestratorInterrupt }
  | { kind: 'error'; message: string }
  | { kind: 'done' };

// Thin client for the Python orchestrator (twenty-ai-service). Shares the same
// AI_SERVICE_URL base as the NER/masking client; the route selects the
// capability. The orchestrator keeps per-session state keyed by session_id,
// which we map 1:1 to the chat threadId.
@Injectable()
export class AgentOrchestratorClientService {
  private readonly logger = new Logger(AgentOrchestratorClientService.name);

  constructor(
    private readonly secureHttpClientService: SecureHttpClientService,
    private readonly twentyConfigService: TwentyConfigService,
  ) {}

  async chat(sessionId: string, message: string): Promise<OrchestratorResult> {
    return this.post('/agent/chat', {
      session_id: sessionId,
      message,
    });
  }

  async resume(
    sessionId: string,
    approved: boolean,
  ): Promise<OrchestratorResult> {
    return this.post('/agent/resume', {
      session_id: sessionId,
      approved,
    });
  }

  // Streaming variants: same routes with a `/stream` suffix. The Python service
  // emits NDJSON progress events; handlers fire live and the accumulated result
  // is returned at the end.
  async chatStream(
    sessionId: string,
    message: string,
    handlers: OrchestratorStreamHandlers,
  ): Promise<OrchestratorResult> {
    return this.postStream(
      '/agent/chat/stream',
      { session_id: sessionId, message },
      handlers,
    );
  }

  async resumeStream(
    sessionId: string,
    approved: boolean,
    handlers: OrchestratorStreamHandlers,
  ): Promise<OrchestratorResult> {
    return this.postStream(
      '/agent/resume/stream',
      { session_id: sessionId, approved },
      handlers,
    );
  }

  private async postStream(
    route: string,
    body: Record<string, unknown>,
    handlers: OrchestratorStreamHandlers,
  ): Promise<OrchestratorResult> {
    const baseUrl = this.twentyConfigService.get('AI_SERVICE_URL');
    const httpClient = this.secureHttpClientService.getInternalHttpClient({
      timeout: 120_000,
    });

    const response = await httpClient.post(`${baseUrl}${route}`, body, {
      responseType: 'stream',
    });
    const stream = response.data as NodeJS.ReadableStream;

    let buffer = '';
    let collectedText = '';
    let interrupt: OrchestratorInterrupt | undefined;

    const handleEvent = (event: OrchestratorStreamEvent) => {
      switch (event.kind) {
        case 'stage':
          handlers.onStage?.(event.text);
          break;
        case 'token':
          collectedText += event.text;
          handlers.onToken?.(event.text);
          break;
        case 'interrupt':
          interrupt = event.interrupt;
          break;
        default:
          break;
      }
    };

    await new Promise<void>((resolve, reject) => {
      stream.on('data', (chunk: Buffer) => {
        buffer += chunk.toString('utf8');

        let newlineIndex = buffer.indexOf('\n');

        while (newlineIndex >= 0) {
          const line = buffer.slice(0, newlineIndex).trim();

          buffer = buffer.slice(newlineIndex + 1);

          if (line.length > 0) {
            let event: OrchestratorStreamEvent | undefined;

            try {
              event = JSON.parse(line) as OrchestratorStreamEvent;
            } catch {
              event = undefined;
            }

            if (event?.kind === 'error') {
              reject(new Error(event.message || 'Orchestrator stream error'));

              return;
            }

            if (isDefined(event)) {
              handleEvent(event);
            }
          }

          newlineIndex = buffer.indexOf('\n');
        }
      });
      stream.on('end', () => resolve());
      stream.on('error', (error: Error) => reject(error));
    });

    if (isDefined(interrupt)) {
      return { type: 'interrupt', interrupt };
    }

    return { type: 'response', response: collectedText };
  }

  private async post(
    route: string,
    body: Record<string, unknown>,
  ): Promise<OrchestratorResult> {
    const baseUrl = this.twentyConfigService.get('AI_SERVICE_URL');

    // Trusted internal service (loopback in dev) — use the internal client.
    const httpClient = this.secureHttpClientService.getInternalHttpClient({
      timeout: 120_000,
    });

    try {
      const { data } = await httpClient.post<{
        type?: string;
        response?: string;
        interrupt?: OrchestratorInterrupt;
      }>(`${baseUrl}${route}`, body);

      if (data?.type === 'interrupt' && isDefined(data.interrupt)) {
        return { type: 'interrupt', interrupt: data.interrupt };
      }

      return { type: 'response', response: data?.response ?? '' };
    } catch (error) {
      this.logger.error(`Orchestrator call to ${route} failed: ${error}`);
      throw error;
    }
  }
}
