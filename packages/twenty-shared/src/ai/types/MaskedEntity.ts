export type MaskedEntityType =
  | 'person'
  | 'company'
  | 'opportunity'
  | 'money'
  | 'other';

// An entity detected in a chat message by the NER/text-masking service, with its
// masked value when one exists. Used to highlight spans in the user's own message
// and reveal the mask on hover.
export type MaskedEntity = {
  // Raw NER label, e.g. person | company | deal | money | date | email address.
  label: string;
  type: MaskedEntityType;
  originalText: string;
  // Offsets into the original message text (what the UI displays).
  start: number | null;
  end: number | null;
  // Token (CONTACT_001) / obfuscated number for money, or null when not masked.
  masked: string | null;
  token: string | null;
  recordId?: string;
};
