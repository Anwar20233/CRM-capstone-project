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
