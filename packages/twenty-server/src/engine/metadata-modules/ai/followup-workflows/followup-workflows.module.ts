import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';

import { SecureHttpClientModule } from 'src/engine/core-modules/secure-http-client/secure-http-client.module';
import { TwentyConfigModule } from 'src/engine/core-modules/twenty-config/twenty-config.module';
import { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';
import { FollowupEmailFetchCronCommand } from 'src/engine/metadata-modules/ai/followup-workflows/crons/commands/followup-email-fetch.cron.command';
import { FollowupEmailReviewCronCommand } from 'src/engine/metadata-modules/ai/followup-workflows/crons/commands/followup-email-review.cron.command';
import { FollowupEmailSendOutboxCronCommand } from 'src/engine/metadata-modules/ai/followup-workflows/crons/commands/followup-email-send-outbox.cron.command';
import { FollowupEmailFetchCronJob } from 'src/engine/metadata-modules/ai/followup-workflows/crons/jobs/followup-email-fetch.cron.job';
import { FollowupEmailReviewCronJob } from 'src/engine/metadata-modules/ai/followup-workflows/crons/jobs/followup-email-review.cron.job';
import { FollowupEmailSendOutboxCronJob } from 'src/engine/metadata-modules/ai/followup-workflows/crons/jobs/followup-email-send-outbox.cron.job';
import { FollowupWorkflowClientService } from 'src/engine/metadata-modules/ai/followup-workflows/services/followup-workflow-client.service';

@Module({
  imports: [
    TypeOrmModule.forFeature([WorkspaceEntity]),
    TwentyConfigModule,
    SecureHttpClientModule,
  ],
  providers: [
    FollowupWorkflowClientService,
    FollowupEmailFetchCronCommand,
    FollowupEmailReviewCronCommand,
    FollowupEmailSendOutboxCronCommand,
    FollowupEmailFetchCronJob,
    FollowupEmailReviewCronJob,
    FollowupEmailSendOutboxCronJob,
  ],
  exports: [
    FollowupWorkflowClientService,
    FollowupEmailFetchCronCommand,
    FollowupEmailReviewCronCommand,
    FollowupEmailSendOutboxCronCommand,
  ],
})
export class FollowupWorkflowsModule {}
