import { Command, CommandRunner } from 'nest-commander';

import { InjectMessageQueue } from 'src/engine/core-modules/message-queue/decorators/message-queue.decorator';
import { MessageQueue } from 'src/engine/core-modules/message-queue/message-queue.constants';
import { MessageQueueService } from 'src/engine/core-modules/message-queue/services/message-queue.service';
import {
  FOLLOWUP_EMAIL_SEND_OUTBOX_CRON_PATTERN,
  FollowupEmailSendOutboxCronJob,
} from 'src/engine/metadata-modules/ai/followup-workflows/crons/jobs/followup-email-send-outbox.cron.job';

@Command({
  name: 'cron:followup:email-send-outbox',
  description: 'Register cron to send accepted follow-up draft emails',
})
export class FollowupEmailSendOutboxCronCommand extends CommandRunner {
  constructor(
    @InjectMessageQueue(MessageQueue.cronQueue)
    private readonly messageQueueService: MessageQueueService,
  ) {
    super();
  }

  async run(): Promise<void> {
    await this.messageQueueService.addCron<undefined>({
      jobName: FollowupEmailSendOutboxCronJob.name,
      data: undefined,
      options: {
        repeat: { pattern: FOLLOWUP_EMAIL_SEND_OUTBOX_CRON_PATTERN },
      },
    });
  }
}
