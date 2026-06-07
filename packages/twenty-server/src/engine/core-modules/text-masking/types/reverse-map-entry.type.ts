// One row of the persisted reverse map: how to turn a masked token back into
// the real record during the later un-masking phase.
export type ReverseMapEntry = {
  token: string; // CONTACT_001 / ORG_001 / DEAL_001
  type: 'person' | 'company' | 'opportunity';
  recordId: string;
  originalText: string; // the source span that was matched
};

export type ReverseMap = Record<string, ReverseMapEntry>;
