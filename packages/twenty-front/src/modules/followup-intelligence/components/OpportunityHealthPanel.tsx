import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { type FollowupAction, type FollowupProfile } from '@/followup-intelligence/types/followup-action';

const StyledContainer = styled.div`
  background: ${themeCssVariables.background.transparent.lighter};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  padding: ${themeCssVariables.spacing[3]};
`;

const StyledTitle = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.md};
  font-weight: ${themeCssVariables.font.weight.semiBold};
`;

const StyledMetricRow = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledMetricLabel = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledMetricValue = styled.span`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.md};
  font-weight: ${themeCssVariables.font.weight.medium};
`;

const StyledNarrative = styled.div`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.sm};
  line-height: 1.5;
`;

const formatRiskScore = (riskScore: number | null | undefined) => {
  if (riskScore === null || riskScore === undefined) {
    return '—';
  }

  return `${Math.round(riskScore * 100)}%`;
};

type OpportunityHealthPanelProps = {
  profile: FollowupProfile | null;
  pendingActions: FollowupAction[];
  profileLoading: boolean;
  profileError: string | null;
};

export const OpportunityHealthPanel = ({
  profile,
  pendingActions,
  profileLoading,
  profileError,
}: OpportunityHealthPanelProps) => {
  const { t } = useLingui();

  return (
    <StyledContainer>
      <StyledTitle>{t`Opportunity health`}</StyledTitle>
      <StyledMetricRow>
        <StyledMetricLabel>{t`Risk score`}</StyledMetricLabel>
        <StyledMetricValue>
          {profileLoading ? t`Loading…` : formatRiskScore(profile?.risk_score)}
        </StyledMetricValue>
      </StyledMetricRow>
      <StyledMetricRow>
        <StyledMetricLabel>{t`Pending actions`}</StyledMetricLabel>
        <StyledMetricValue>{pendingActions.length}</StyledMetricValue>
      </StyledMetricRow>
      {profileError && (
        <StyledNarrative>{profileError}</StyledNarrative>
      )}
      {!profileLoading && profile?.narrative && (
        <StyledNarrative>{profile.narrative}</StyledNarrative>
      )}
    </StyledContainer>
  );
};
