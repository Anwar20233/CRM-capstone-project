// One row of the persisted reverse map: how to turn a masked token back into
// the real value during the later un-masking phase.
export type ReverseMapEntry =
  | {
      token: string; // CONTACT_001 / ORG_001 / DEAL_001
      type: 'person' | 'company' | 'opportunity';
      recordId: string;
      originalText: string; // the source span that was matched
    }
  | {
      token: string; // AMOUNT_001 — enumeration key (the masked text shows the number)
      type: 'money';
      originalText: string; // the source money span, e.g. "$45,000"
      originalValue: number; // parsed numeric value
      obfuscatedValue: number; // originalValue * priceFactor
      obfuscatedText: string; // what replaces the span in masked text, e.g. "$49,500"
    };

export type ReverseMap = Record<string, ReverseMapEntry>;
