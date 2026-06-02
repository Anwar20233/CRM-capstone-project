export type SpanReplacement = {
  start: number;
  end: number;
  replacement: string;
};

// Replace character spans in the source text. Overlapping spans are resolved by
// keeping the longer one (more specific match), and replacements are applied
// right-to-left so earlier offsets stay valid.
export const applyReplacements = (
  text: string,
  replacements: SpanReplacement[],
): string => {
  const sorted = [...replacements].sort(
    (a, b) => a.start - b.start || b.end - b.start - (a.end - a.start),
  );

  const nonOverlapping: SpanReplacement[] = [];
  let lastEnd = -1;

  for (const span of sorted) {
    if (span.start >= lastEnd) {
      nonOverlapping.push(span);
      lastEnd = span.end;
    }
  }

  let result = text;

  for (const span of [...nonOverlapping].sort((a, b) => b.start - a.start)) {
    result =
      result.slice(0, span.start) + span.replacement + result.slice(span.end);
  }

  return result;
};
