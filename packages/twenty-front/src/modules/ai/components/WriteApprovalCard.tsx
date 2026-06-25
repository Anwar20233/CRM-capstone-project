import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { type DataMessagePart } from 'twenty-shared/ai';
import { IconAlertTriangle, IconCheck, IconX } from 'twenty-ui/display';
import { Button } from 'twenty-ui/input';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { useResumeAgentChatWrite } from '@/ai/hooks/useResumeAgentChatWrite';

const StyledContainer = styled.div`
  background: ${themeCssVariables.background.transparent.lighter};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  padding: ${themeCssVariables.spacing[3]};
`;

const StyledHeader = styled.div`
  align-items: center;
  color: ${themeCssVariables.font.color.primary};
  display: flex;
  font-weight: ${themeCssVariables.font.weight.semiBold};
  gap: ${themeCssVariables.spacing[1]};
`;

const StyledSummary = styled.div`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.md};
`;

const StyledActions = styled.div`
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledResolved = styled.div<{ approved: boolean }>`
  color: ${({ approved }) =>
    approved
      ? themeCssVariables.tag.text.green
      : themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.md};
  font-weight: ${themeCssVariables.font.weight.medium};
`;

type WriteApprovalCardProps = {
  data: DataMessagePart['write-confirmation'];
};

export const WriteApprovalCard = ({ data }: WriteApprovalCardProps) => {
  const { t } = useLingui();
  const { resolveWrite, isResolving } = useResumeAgentChatWrite();

  // Once resolved, the part's status is persisted as approved/rejected so a
  // reopened thread shows the outcome instead of live buttons.
  if (data.status !== 'pending') {
    return (
      <StyledContainer>
        <StyledHeader>{t`Action review`}</StyledHeader>
        <StyledSummary>{data.summary}</StyledSummary>
        <StyledResolved approved={data.status === 'approved'}>
          {data.status === 'approved' ? t`Approved` : t`Rejected`}
        </StyledResolved>
      </StyledContainer>
    );
  }

  return (
    <StyledContainer>
      <StyledHeader>
        <IconAlertTriangle size={16} />
        {t`Approval required`}
      </StyledHeader>
      <StyledSummary>
        {t`The agent wants to run:`} <strong>{data.summary}</strong>
      </StyledSummary>
      <StyledActions>
        <Button
          variant="primary"
          accent="blue"
          size="small"
          Icon={IconCheck}
          title={t`Approve`}
          disabled={isResolving}
          onClick={() => resolveWrite(data.threadId, true)}
        />
        <Button
          variant="secondary"
          accent="danger"
          size="small"
          Icon={IconX}
          title={t`Reject`}
          disabled={isResolving}
          onClick={() => resolveWrite(data.threadId, false)}
        />
      </StyledActions>
    </StyledContainer>
  );
};
