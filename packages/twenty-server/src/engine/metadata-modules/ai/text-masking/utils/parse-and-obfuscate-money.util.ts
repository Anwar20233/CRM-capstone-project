// Leading currency symbol/code on a money span, preserved on the obfuscated output.
const CURRENCY_PREFIX_RE =
  /^\s*(?:[$€£¥]|USD|EUR|GBP|AED|SAR|EGP|GHS|CHF|JPY|CNY)\s?/i;

const MULTIPLIERS: Record<string, number> = {
  k: 1_000,
  m: 1_000_000,
  b: 1_000_000_000,
  million: 1_000_000,
  billion: 1_000_000_000,
};

// Parse a money span like "$45,000", "€1.2M", "AED 500k", "120,000 EGP".
// Returns the numeric value, or null when no number is present (e.g. "six figures").
export const parseMoneyValue = (text: string): number | null => {
  const lower = text.toLowerCase();

  const numberMatch = lower.match(/\d[\d,]*(?:\.\d+)?/);

  if (!numberMatch) {
    return null;
  }

  const base = Number(numberMatch[0].replace(/,/g, ''));

  if (Number.isNaN(base)) {
    return null;
  }

  const multiplierMatch = lower.match(/(million|billion|\b[kmb]\b|\d[kmb])/);
  const multiplierKey = multiplierMatch
    ? multiplierMatch[0].replace(/\d/g, '').trim()
    : undefined;

  const multiplier =
    multiplierKey && MULTIPLIERS[multiplierKey]
      ? MULTIPLIERS[multiplierKey]
      : 1;

  return base * multiplier;
};

// Build the obfuscated display string that replaces the span in masked text,
// preserving the original currency prefix and thousands formatting.
export const obfuscateMoneyText = (
  originalText: string,
  originalValue: number,
  priceFactor: number,
): string => {
  const obfuscatedValue = Math.round(originalValue * priceFactor);

  const prefixMatch = originalText.match(CURRENCY_PREFIX_RE);
  const prefix = prefixMatch ? prefixMatch[0].trimEnd() : '';
  const separator = prefix && !/[$€£¥]$/.test(prefix) ? ' ' : '';

  return `${prefix}${separator}${obfuscatedValue.toLocaleString('en-US')}`;
};
