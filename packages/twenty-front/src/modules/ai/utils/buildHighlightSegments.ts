import { type MaskedEntity } from 'twenty-shared/ai';
import { isDefined } from 'twenty-shared/utils';

export type HighlightSegment =
  | { kind: 'text'; text: string }
  | { kind: 'entity'; text: string; entity: MaskedEntity };

// Splits the message text into interleaved plain-text and entity segments using
// the entity offsets. Overlapping spans are resolved by keeping the longer one;
// spans with missing/out-of-range offsets are ignored (the text still renders).
export const buildHighlightSegments = (
  text: string,
  entities: MaskedEntity[],
): HighlightSegment[] => {
  const validSpans = entities
    .filter(
      (entity) =>
        isDefined(entity.start) &&
        isDefined(entity.end) &&
        entity.start >= 0 &&
        entity.end <= text.length &&
        entity.start < entity.end,
    )
    .sort((a, b) => {
      const startA = a.start as number;
      const startB = b.start as number;

      return (
        startA - startB ||
        (b.end as number) - startB - ((a.end as number) - startA)
      );
    });

  const segments: HighlightSegment[] = [];
  let cursor = 0;

  for (const entity of validSpans) {
    const start = entity.start as number;
    const end = entity.end as number;

    // Skip spans that overlap an already-emitted one.
    if (start < cursor) {
      continue;
    }

    if (start > cursor) {
      segments.push({ kind: 'text', text: text.slice(cursor, start) });
    }

    segments.push({ kind: 'entity', text: text.slice(start, end), entity });
    cursor = end;
  }

  if (cursor < text.length) {
    segments.push({ kind: 'text', text: text.slice(cursor) });
  }

  return segments;
};
