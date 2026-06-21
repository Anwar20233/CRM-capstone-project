import { Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';

import { WorkspaceActivationStatus } from 'twenty-shared/workspace';
import { Repository } from 'typeorm';

import { SentryCronMonitor } from 'src/engine/core-modules/cron/sentry-cron-monitor.decorator';
import { ExceptionHandlerService } from 'src/engine/core-modules/exception-handler/exception-handler.service';
import { Process } from 'src/engine/core-modules/message-queue/decorators/process.decorator';
import { Processor } from 'src/engine/core-modules/message-queue/decorators/processor.decorator';
import { MessageQueue } from 'src/engine/core-modules/message-queue/message-queue.constants';
import { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';
import { FollowupWorkflowClientService } from 'src/engine/metadata-modules/ai/followup-workflows/services/followup-workflow-client.service';

export const FOLLOWUP_EMAIL_FETCH_CRON_PATTERN = '0 * * * *';

@Processor(MessageQueue.cronQueue)
export class FollowupEmailFetchCronJob {
  private readonly logger = new Logger(FollowupEmailFetchCronJob.name);

  constructor(
    @InjectRepository(WorkspaceEntity)
    private readonly workspaceRepository: Repository<WorkspaceEntity>,
    private readonly followupWorkflowClientService: FollowupWorkflowClientService,
    private readonly exceptionHandlerService: ExceptionHandlerService,
  ) {}

  @Process(FollowupEmailFetchCronJob.name)
  @SentryCronMonitor(
    FollowupEmailFetchCronJob.name,
    FOLLOWUP_EMAIL_FETCH_CRON_PATTERN,
  )
  async handle(): Promise<void> {
    const activeWorkspaces = await this.workspaceRepository.find({
      where: { activationStatus: WorkspaceActivationStatus.ACTIVE },
    });

    for (const workspace of activeWorkspaces) {
      try {
        const result =
          await this.followupWorkflowClientService.fetchInboundEmails(
            workspace.id,
          );

        this.logger.log(
          `Follow-up email fetch workspace=${workspace.id} enqueued=${result.enqueued}`,
        );
      } catch (error) {
        this.exceptionHandlerService.captureExceptions([error], {
          workspace: { id: workspace.id },
        });
      }
    }
  }
}
