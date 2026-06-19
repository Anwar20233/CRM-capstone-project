import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { useState } from 'react';
import { IconCheck, IconPencil, IconX } from 'twenty-ui/display';
import { Button } from 'twenty-ui/input';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { DraftPreview } from '@/followup-intelligence/components/DraftPreview';
import { OpportunityHealthPanel } from '@/followup-intelligence/components/OpportunityHealthPanel';
import { useFollowupActions } from '@/followup-intelligence/hooks/useFollowupActions';
import { useFollowupProfile } from '@/followup-intelligence/hooks/useFollowupProfile';
import { type FollowupAction } from '@/followup-intelligence/types/followup-action';
import { TextArea } from '@/ui/input/components/TextArea';
import { useTargetRecord } from '@/ui/layout/contexts/useTargetRecord';

const StyledContainer = styled.div`
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[4]};
  padding: ${themeCssVariables.spacing[4]};
  width: 100%;
`;

const StyledHeader = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.xl};
  font-weight: ${themeCssVariables.font.weight.semiBold};
`;

const StyledSubheader = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledActionCard = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[3]};
`;

const StyledMetaRow = styled.div`
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledBadge = styled.span<{ urgency: string }>`
  background: ${({ urgency }) =>
    urgency === 'high'
      ? themeCssVariables.tag.background.red
      : urgency === 'low'
        ? themeCssVariables.tag.background.green
        : themeCssVariables.tag.background.orange};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.sm};
  font-weight: ${themeCssVariables.font.weight.medium};
  padding: ${themeCssVariables.spacing[1]} ${themeCssVariables.spacing[2]};
  text-transform: capitalize;
`;

const StyledReasoning = styled.div`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.md};
  line-height: 1.5;
`;

const StyledExpiry = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledActions = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledEmptyState = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.md};
`;

const StyledError = styled.div`
  color: ${themeCssVariables.font.color.danger};
  font-size: ${themeCssVariables.font.size.md};
`;

const formatExpiry = (expiresAt: string | null) => {
  if (!expiresAt) {
    return null;
  }

  const expiryDate = new Date(expiresAt);
  const millisecondsRemaining = expiryDate.getTime() - Date.now();

  if (millisecondsRemaining <= 0) {
    return 'Expired';
  }

  const hoursRemaining = Math.floor(millisecondsRemaining / (1000 * 60 * 60));
  const minutesRemaining = Math.floor(
    (millisecondsRemaining % (1000 * 60 * 60)) / (1000 * 60),
  );

  if (hoursRemaining > 0) {
    return `Expires in ${hoursRemaining}h ${minutesRemaining}m`;
  }

  return `Expires in ${minutesRemaining}m`;
};

type FollowupActionCardProps = {
  action: FollowupAction;
  isMutating: boolean;
  onAccept: (actionId: string) => Promise<void>;
  onReject: (actionId: string) => Promise<void>;
  onRevise: (actionId: string, instructions: string) => Promise<void>;
};

const FollowupActionCard = ({
  action,
  isMutating,
  onAccept,
  onReject,
  onRevise,
}: FollowupActionCardProps) => {
  const { t } = useLingui();
  const [isRevising, setIsRevising] = useState(false);
  const [revisionInstructions, setRevisionInstructions] = useState('');
  const expiryLabel = formatExpiry(action.expires_at);

  const handleRevise = async () => {
    if (!revisionInstructions.trim()) {
      return;
    }

    await onRevise(action.id, revisionInstructions.trim());
    setRevisionInstructions('');
    setIsRevising(false);
  };

  return (
    <StyledActionCard>
      <StyledMetaRow>
        <StyledBadge urgency={action.urgency}>{action.urgency}</StyledBadge>
        <StyledSubheader>{action.action_type.replaceAll('_', ' ')}</StyledSubheader>
      </StyledMetaRow>
      {expiryLabel && <StyledExpiry>{expiryLabel}</StyledExpiry>}
      {action.reasoning && (
        <StyledReasoning>{action.reasoning}</StyledReasoning>
      )}
      <DraftPreview action={action} />
      {isRevising && (
        <TextArea
          textAreaId={`followup-revise-${action.id}`}
          placeholder={t`Tell the agent what to change…`}
          value={revisionInstructions}
          onChange={setRevisionInstructions}
          minRows={3}
        />
      )}
      <StyledActions>
        <Button
          variant="primary"
          accent="blue"
          size="small"
          Icon={IconCheck}
          title={t`Accept`}
          disabled={isMutating}
          onClick={() => onAccept(action.id)}
        />
        <Button
          variant="secondary"
          accent="danger"
          size="small"
          Icon={IconX}
          title={t`Reject`}
          disabled={isMutating}
          onClick={() => onReject(action.id)}
        />
        <Button
          variant="secondary"
          size="small"
          Icon={IconPencil}
          title={isRevising ? t`Submit revision` : t`Revise`}
          disabled={isMutating}
          onClick={() => {
            if (isRevising) {
              void handleRevise();
              return;
            }

            setIsRevising(true);
          }}
        />
      </StyledActions>
    </StyledActionCard>
  );
};

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
    reviseAction,
  } = useFollowupActions(opportunityId);

  const {
    profile,
    loading: profileLoading,
    error: profileError,
  } = useFollowupProfile(opportunityId);

  return (
    <StyledContainer>
      <StyledHeader>{t`Follow-Up Intelligence`}</StyledHeader>
      <StyledSubheader>
        {t`Review AI-drafted follow-ups before they are sent to clients.`}
      </StyledSubheader>

      <OpportunityHealthPanel
        profile={profile}
        pendingActions={actions}
        profileLoading={profileLoading}
        profileError={profileError}
      />

      {loading && <StyledSubheader>{t`Loading drafts…`}</StyledSubheader>}
      {error && <StyledError>{error}</StyledError>}

      {!loading && !error && actions.length === 0 && (
        <StyledEmptyState>
          {t`No pending follow-up drafts for this opportunity.`}
        </StyledEmptyState>
      )}

      {actions.map((action) => (
        <FollowupActionCard
          key={action.id}
          action={action}
          isMutating={isMutating}
          onAccept={acceptAction}
          onReject={rejectAction}
          onRevise={reviseAction}
        />
      ))}
    </StyledContainer>
  );
};
