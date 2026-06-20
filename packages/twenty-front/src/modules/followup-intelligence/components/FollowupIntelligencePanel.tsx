import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { AiChatTab } from '@/ai/components/AiChatTab';
import { OpportunityHealthPanel } from '@/followup-intelligence/components/OpportunityHealthPanel';
import { WorkflowCard } from '@/followup-intelligence/components/WorkflowCard';
import { useFollowupActions } from '@/followup-intelligence/hooks/useFollowupActions';
import { useFollowupChatThread } from '@/followup-intelligence/hooks/useFollowupChatThread';
import { useFollowupRisk } from '@/followup-intelligence/hooks/useFollowupRisk';
import { useTargetRecord } from '@/ui/layout/contexts/useTargetRecord';

const StyledContainer = styled.div`
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  width: 100%;
`;

// Workflows scroll on top; the chat is pinned to the bottom of the tab.
const StyledWorkflows = styled.div`
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: ${themeCssVariables.spacing[3]};
  max-height: 55%;
  overflow-y: auto;
  padding: ${themeCssVariables.spacing[4]};
`;

const StyledHeader = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.lg};
  font-weight: ${themeCssVariables.font.weight.semiBold};
`;

const StyledMessage = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.md};
`;

const StyledError = styled.div`
  color: ${themeCssVariables.font.color.danger};
  font-size: ${themeCssVariables.font.size.md};
`;

const StyledChatSection = styled.div`
  border-top: 1px solid ${themeCssVariables.border.color.medium};
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
`;

export const FollowupIntelligencePanel = () => {
  const { t } = useLingui();
  const targetRecord = useTargetRecord();
  const opportunityId = targetRecord.id;

  const {
    actions,
    loading,
    error,
    isMutating,
    acceptAction,
    rejectAction,
    editAction,
  } = useFollowupActions(opportunityId);

  const { risk, loading: riskLoading } = useFollowupRisk(opportunityId);

  // Make the embedded platform chat track this deal (own thread per deal).
  useFollowupChatThread(opportunityId);

  return (
    <StyledContainer>
      <StyledWorkflows>
        <StyledHeader>{t`Follow-Up Intelligence`}</StyledHeader>

        <OpportunityHealthPanel
          risk={risk}
          riskLoading={riskLoading}
          pendingCount={actions.length}
        />

        {loading && <StyledMessage>{t`Loading workflows…`}</StyledMessage>}
        {error && <StyledError>{error}</StyledError>}

        {!loading && !error && actions.length === 0 && (
          <StyledMessage>
            {t`No pending follow-up workflows for this opportunity.`}
          </StyledMessage>
        )}

        {actions.map((action) => (
          <WorkflowCard
            key={action.id}
            action={action}
            isMutating={isMutating}
            onAccept={acceptAction}
            onReject={rejectAction}
            onEdit={editAction}
          />
        ))}
      </StyledWorkflows>

      {/* The actual platform chat — routed to the Follow-Up agent because an
          opportunity record page is open (see stream-agent-chat.job). */}
      <StyledChatSection>
        <AiChatTab />
      </StyledChatSection>
    </StyledContainer>
  );
};
