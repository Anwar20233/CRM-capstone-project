import { Injectable, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';

import { google } from 'googleapis';
import { IsNull, Not, Repository } from 'typeorm';
import { ConnectedAccountProvider } from 'twenty-shared/types';
import { isDefined } from 'twenty-shared/utils';

import {
  type BookCalendarEventToolInput,
  BookCalendarEventToolInputZodSchema,
} from 'src/engine/core-modules/tool/tools/calendar-tool/calendar-tool.schema';
import { type ToolExecutionContext } from 'src/engine/core-modules/tool/types/tool-execution-context.type';
import { type ToolOutput } from 'src/engine/core-modules/tool/types/tool-output.type';
import { type Tool } from 'src/engine/core-modules/tool/types/tool.type';
import { ConnectedAccountEntity } from 'src/engine/metadata-modules/connected-account/entities/connected-account.entity';
import { OAuth2ClientManagerService } from 'src/modules/connected-account/oauth2-client-manager/services/oauth2-client-manager.service';

// Creates a real event on the user's Google Calendar via the connected account.
// Twenty's calendar integration is import-only, so we never write the local
// calendarEvent row ourselves — the existing calendar sync re-imports this event
// (matching attendee emails to CRM people), which is what makes it appear in the
// record's Calendar tab. So the contact must be an attendee for it to link back.
@Injectable()
export class BookCalendarEventTool implements Tool {
  private readonly logger = new Logger(BookCalendarEventTool.name);

  description =
    'Create an event on a connected Google Calendar and invite attendees. ' +
    'The event is created in the real Google Calendar and syncs back into Twenty automatically. ' +
    'Include the contact email in attendees so the event links to their CRM record.';
  inputSchema = BookCalendarEventToolInputZodSchema;

  constructor(
    @InjectRepository(ConnectedAccountEntity)
    private readonly connectedAccountRepository: Repository<ConnectedAccountEntity>,
    private readonly oAuth2ClientManagerService: OAuth2ClientManagerService,
  ) {}

  async execute(
    parameters: BookCalendarEventToolInput,
    context: ToolExecutionContext,
  ): Promise<ToolOutput> {
    try {
      const connectedAccount = await this.resolveConnectedAccount(
        parameters.connectedAccountId,
        context.workspaceId,
      );

      if (!isDefined(connectedAccount)) {
        return {
          success: false,
          message: 'Failed to create calendar event',
          error:
            'No Google connected account was found for this workspace. ' +
            'Connect a Google account with calendar access in Settings > Accounts.',
        };
      }

      if (connectedAccount.provider !== ConnectedAccountProvider.GOOGLE) {
        return {
          success: false,
          message: 'Failed to create calendar event',
          error: `Calendar event creation is only supported for Google accounts (got "${connectedAccount.provider}").`,
        };
      }

      const oAuth2Client =
        await this.oAuth2ClientManagerService.getGoogleOAuth2Client(
          connectedAccount,
        );

      const googleCalendarClient = google.calendar({
        version: 'v3',
        auth: oAuth2Client,
      });

      const { data } = await googleCalendarClient.events.insert({
        calendarId: 'primary',
        sendUpdates: 'all',
        requestBody: {
          summary: parameters.title,
          description: parameters.description,
          start: { dateTime: parameters.startsAt },
          end: { dateTime: parameters.endsAt },
          attendees: (parameters.attendees ?? [])
            .filter((email) => isDefined(email) && email.length > 0)
            .map((email) => ({ email })),
        },
      });

      this.logger.log(
        `Calendar event "${parameters.title}" created on ${connectedAccount.handle} (${data.id})`,
      );

      return {
        success: true,
        message: `Calendar event "${parameters.title}" created and invitations sent.`,
        result: {
          eventId: data.id,
          htmlLink: data.htmlLink,
          status: data.status,
          connectedAccountId: connectedAccount.id,
        },
      };
    } catch (error) {
      this.logger.error(`Failed to create calendar event: ${error}`);

      return {
        success: false,
        message: 'Failed to create calendar event',
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  // Use the explicit account when given; otherwise fall back to the workspace's
  // Google account (the one whose calendar is synced). Require a refresh token:
  // seeded/placeholder Google accounts have a null token and can't authenticate
  // (events.insert would throw "Refresh token is required"), so we skip them and
  // pick a genuinely connected account.
  private async resolveConnectedAccount(
    connectedAccountId: string | undefined,
    workspaceId: string,
  ): Promise<ConnectedAccountEntity | null> {
    if (isDefined(connectedAccountId)) {
      return this.connectedAccountRepository.findOne({
        where: { id: connectedAccountId, workspaceId },
      });
    }

    return this.connectedAccountRepository.findOne({
      where: {
        workspaceId,
        provider: ConnectedAccountProvider.GOOGLE,
        refreshToken: Not(IsNull()),
      },
    });
  }
}
