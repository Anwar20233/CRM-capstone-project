import { Injectable, Logger } from '@nestjs/common';

import { SecureHttpClientService } from 'src/engine/core-modules/secure-http-client/secure-http-client.service';
import { TwentyConfigService } from 'src/engine/core-modules/twenty-config/twenty-config.service';

type EmailFetchResult = {
  fetched: number;
  enqueued: number;
  skipped_duplicate: number;
};

type EmailReviewResult = {
  claimed: number;
  processed: number;
  skipped: number;
  failed: number;
};

type EmailSendOutboxResult = {
  claimed: number;
  sent: number;
  skipped: number;
  failed: number;
};

@Injectable()
export class FollowupWorkflowClientService {
  private readonly logger = new Logger(FollowupWorkflowClientService.name);

  constructor(
    private readonly secureHttpClientService: SecureHttpClientService,
    private readonly twentyConfigService: TwentyConfigService,
  ) {}

  async fetchInboundEmails(workspaceId: string): Promise<EmailFetchResult> {
    return this.post<EmailFetchResult>('/followup/workflows/email/fetch', {
      workspace_id: workspaceId,
    });
  }

  async reviewPendingEmails(
    workspaceId: string,
    batchSize = 10,
  ): Promise<EmailReviewResult> {
    return this.post<EmailReviewResult>('/followup/workflows/email/review', {
      workspace_id: workspaceId,
      batch_size: batchSize,
    });
  }

  async sendOutboxEmails(
    workspaceId: string,
    batchSize = 20,
  ): Promise<EmailSendOutboxResult> {
    return this.post<EmailSendOutboxResult>(
      '/followup/workflows/email/send-outbox',
      {
        workspace_id: workspaceId,
        batch_size: batchSize,
      },
    );
  }

  private async post<T>(
    route: string,
    body: Record<string, unknown>,
  ): Promise<T> {
    const baseUrl = this.twentyConfigService.get('AI_SERVICE_URL');
    const httpClient = this.secureHttpClientService.getInternalHttpClient({
      timeout: 120_000,
    });

    try {
      const { data } = await httpClient.post<T>(`${baseUrl}${route}`, body);

      return data;
    } catch (error) {
      this.logger.error(`Follow-up workflow call to ${route} failed: ${error}`);
      throw error;
    }
  }
}
