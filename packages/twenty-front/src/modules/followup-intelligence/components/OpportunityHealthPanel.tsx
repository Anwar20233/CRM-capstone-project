import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { type FollowupRisk } from '@/followup-intelligence/types/followup-action';

const StyledContainer = styled.div`
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledLabel = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledRiskBadge = styled.span<{ level: string }>`
  background: ${({ level }) =>
    level === 'high'
      ? themeCssVariables.tag.background.red
      : level === 'low'
        ? themeCssVariables.tag.background.green
        : themeCssVariables.tag.background.orange};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.sm};
  font-weight: ${themeCssVariables.font.weight.medium};
  padding: ${themeCssVariables.spacing[1]} ${themeCssVariables.spacing[2]};
  text-transform: capitalize;
`;

const StyledCount = styled.span`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.sm};
`;

// The daily score is 0-1; render as a whole percentage.
const formatRiskScore = (riskScore: number | null | undefined) => {
  if (riskScore === null || riskScore === undefined) {
    return null;
  }

  return `${Math.round(riskScore * 100)}%`;
};

type OpportunityHealthPanelProps = {
  risk: FollowupRisk | null;
  riskLoading: boolean;
  pendingCount: number;
};

export const OpportunityHealthPanel = ({
  risk,
  riskLoading,
  pendingCount,
}: OpportunityHealthPanelProps) => {
  const { t } = useLingui();

  const level = risk?.risk_level ?? 'medium';
  const scoreLabel = formatRiskScore(risk?.risk_score);

  return (
    <StyledContainer>
      <StyledLabel>{t`Deal risk`}</StyledLabel>
      {riskLoading ? (
        <StyledCount>{t`Loading…`}</StyledCount>
      ) : risk?.risk_level || scoreLabel ? (
        <StyledRiskBadge level={level}>
          {risk?.risk_level ?? t`Unknown`}
          {scoreLabel ? ` · ${scoreLabel}` : ''}
        </StyledRiskBadge>
      ) : (
        <StyledCount>{t`Not yet scored`}</StyledCount>
      )}
      <StyledCount>
        {pendingCount === 1
          ? t`1 pending workflow`
          : t`${pendingCount} pending workflows`}
      </StyledCount>
    </StyledContainer>
  );
};
