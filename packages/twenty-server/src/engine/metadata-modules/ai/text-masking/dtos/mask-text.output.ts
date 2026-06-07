export type MaskedEntityType =
  | 'person'
  | 'company'
  | 'opportunity'
  | 'money'
  | 'other';

// Every entity the NER service detected in the submitted text, annotated with
// its masked value when we have one. Used by the chat UI to highlight spans in
// the user's own message and reveal the mask on hover.
export type MaskedEntity = {
  // Raw NER label, e.g. person | company | deal | money | date | email address.
  label: string;
  type: MaskedEntityType;
  originalText: string;
  // Offsets into the ORIGINAL submitted text (what the UI displays).
  start: number | null;
  end: number | null;
  // Token (CONTACT_001 / ORG_001 / DEAL_001) for matched records, or null when
  // the entity is detected but not masked (money, dates, or not in the CRM).
  masked: string | null;
  // Stable enumeration key when masked (CONTACT_001 / ORG_001 / DEAL_001).
  token: string | null;
  // Present for entities matched to a CRM record.
  recordId?: string;
};

export type MaskTextOutput = {
  maskedText: string;
  sessionId: string;
  // All detected entities (masked and unmasked) with offsets.
  entities: MaskedEntity[];
};
