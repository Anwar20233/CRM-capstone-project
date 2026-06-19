import { Command, CommandRunner } from 'nest-commander';

import { InjectMessageQueue } from 'src/engine/core-modules/message-queue/decorators/message-queue.decorator';
import { MessageQueue } from 'src/engine/core-modules/message-queue/message-queue.constants';
import { MessageQueueService } from 'src/engine/core-modules/message-queue/services/message-queue.service';
import {
  FOLLOWUP_EMAIL_REVIEW_CRON_PATTERN,
  FollowupEmailReviewCronJob,
} from 'src/engine/metadata-modules/ai/followup-workflows/crons/jobs/followup-email-review.cron.job';

@Command({
  name: 'cron:followup:email-review',
  description: 'Register cron to review queued inbound follow-up emails',
})
export class FollowupEmailReviewCronCommand extends CommandRunner {
  constructor(
    @InjectMessageQueue(MessageQueue.cronQueue)
    private readonly messageQueueService: MessageQueueService,
  ) {
    super();
  }

  async run(): Promise<void> {
    await this.messageQueueService.addCron<undefined>({
      jobName: FollowupEmailReviewCronJob.name,
      data: undefined,
      options: {
        repeat: { pattern: FOLLOWUP_EMAIL_REVIEW_CRON_PATTERN },
      },
    });
  }
}
