import { Command, CommandRunner } from 'nest-commander';

import { InjectMessageQueue } from 'src/engine/core-modules/message-queue/decorators/message-queue.decorator';
import { MessageQueue } from 'src/engine/core-modules/message-queue/message-queue.constants';
import { MessageQueueService } from 'src/engine/core-modules/message-queue/services/message-queue.service';
import {
  FOLLOWUP_EMAIL_FETCH_CRON_PATTERN,
  FollowupEmailFetchCronJob,
} from 'src/engine/metadata-modules/ai/followup-workflows/crons/jobs/followup-email-fetch.cron.job';

@Command({
  name: 'cron:followup:email-fetch',
  description: 'Register cron to fetch inbound emails into the follow-up queue',
})
export class FollowupEmailFetchCronCommand extends CommandRunner {
  constructor(
    @InjectMessageQueue(MessageQueue.cronQueue)
    private readonly messageQueueService: MessageQueueService,
  ) {
    super();
  }

  async run(): Promise<void> {
    await this.messageQueueService.addCron<undefined>({
      jobName: FollowupEmailFetchCronJob.name,
      data: undefined,
      options: {
        repeat: { pattern: FOLLOWUP_EMAIL_FETCH_CRON_PATTERN },
      },
    });
  }
}
