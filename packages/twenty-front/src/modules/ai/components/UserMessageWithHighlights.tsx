import { styled } from '@linaria/react';
import { Fragment } from 'react';
import { type MaskedEntity } from 'twenty-shared/ai';
import { isDefined } from 'twenty-shared/utils';
import { AppTooltip, TooltipDelay } from 'twenty-ui/display';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { buildHighlightSegments } from '@/ai/utils/buildHighlightSegments';
import { getEntityHighlightColor } from '@/ai/utils/getEntityHighlightColor';

const StyledHighlight = styled.span<{ background: string; color: string }>`
  background: ${({ background }) => background};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${({ color }) => color};
  cursor: help;
  padding: 0 2px;
`;

const buildTooltipContent = (entity: MaskedEntity): string =>
  isDefined(entity.masked)
    ? `Masked as ${entity.masked}`
    : `Detected (${entity.label}) — not masked`;

type UserMessageWithHighlightsProps = {
  messageId: string;
  text: string;
  entitySpans: MaskedEntity[];
};

// Renders the user's own message as plain text with detected entities
// highlighted inline (consistent color per type). Hovering a highlight reveals
// the masked value, so the user can see their info would be protected.
export const UserMessageWithHighlights = ({
  messageId,
  text,
  entitySpans,
}: UserMessageWithHighlightsProps) => {
  const segments = buildHighlightSegments(text, entitySpans);

  return (
    <span>
      {segments.map((segment, index) => {
        if (segment.kind === 'text') {
          return <Fragment key={index}>{segment.text}</Fragment>;
        }

        const tooltipId = `mask-entity-${messageId}-${index}`;
        const { background, text: color } = getEntityHighlightColor(
          segment.entity.label,
        );

        return (
          <Fragment key={index}>
            <StyledHighlight
              background={background}
              color={color}
              data-tooltip-id={tooltipId}
            >
              {segment.text}
            </StyledHighlight>
            <AppTooltip
              anchorSelect={`[data-tooltip-id='${tooltipId}']`}
              content={buildTooltipContent(segment.entity)}
              delay={TooltipDelay.shortDelay}
              place="top"
            />
          </Fragment>
        );
      })}
    </span>
  );
};
