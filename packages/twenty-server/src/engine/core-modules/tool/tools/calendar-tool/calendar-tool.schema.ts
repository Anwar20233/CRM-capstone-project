import { isValidUuid } from 'twenty-shared/utils';
import { z } from 'zod';

export const BookCalendarEventToolInputZodSchema = z.object({
  title: z.string().describe('The calendar event title / summary'),
  startsAt: z
    .string()
    .describe(
      'Event start time in ISO 8601 format with timezone, e.g. 2026-06-25T15:00:00Z',
    ),
  endsAt: z
    .string()
    .describe('Event end time in ISO 8601 format with timezone'),
  attendees: z
    .array(z.string())
    .describe(
      'Email addresses to invite as attendees. Include the contact so the event links back to their CRM record on sync.',
    )
    .optional()
    .default([]),
  description: z
    .string()
    .describe('Optional event description / body')
    .optional(),
  connectedAccountId: z
    .string()
    .refine((val) => isValidUuid(val))
    .describe(
      'UUID of the Google connected account to create the event on. Provide this only if you have it; otherwise the workspace default Google account is used.',
    )
    .optional(),
});

export type BookCalendarEventToolInput = z.infer<
  typeof BookCalendarEventToolInputZodSchema
>;
