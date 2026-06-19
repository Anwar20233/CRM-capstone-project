import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { IconCopy } from 'twenty-ui/display';
import { Button } from 'twenty-ui/input';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { type FollowupAction } from '@/followup-intelligence/types/followup-action';
import { useCopyToClipboard } from '~/hooks/useCopyToClipboard';

const StyledContainer = styled.div`
  background: ${themeCssVariables.background.transparent.lighter};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[3]};
  padding: ${themeCssVariables.spacing[3]};
`;

const StyledLabel = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
  font-weight: ${themeCssVariables.font.weight.medium};
`;

const StyledSubject = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.lg};
  font-weight: ${themeCssVariables.font.weight.semiBold};
`;

const StyledBody = styled.pre`
  color: ${themeCssVariables.font.color.secondary};
  font-family: inherit;
  font-size: ${themeCssVariables.font.size.md};
  line-height: 1.5;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
`;

const StyledActions = styled.div`
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

type DraftPreviewProps = {
  action: FollowupAction;
};

export const DraftPreview = ({ action }: DraftPreviewProps) => {
  const { t } = useLingui();
  const { copyToClipboard } = useCopyToClipboard();

  const subject = action.draft_subject ?? t`No subject`;
  const body = action.draft_body ?? t`No draft body available.`;

  const handleCopySubject = () => {
    copyToClipboard(subject, t`Subject copied to clipboard`);
  };

  const handleCopyBody = () => {
    copyToClipboard(body, t`Body copied to clipboard`);
  };

  const handleCopyAll = () => {
    copyToClipboard(
      `Subject: ${subject}\n\n${body}`,
      t`Draft copied to clipboard`,
    );
  };

  return (
    <StyledContainer>
      <StyledLabel>{t`Subject`}</StyledLabel>
      <StyledSubject>{subject}</StyledSubject>
      <StyledLabel>{t`Body`}</StyledLabel>
      <StyledBody>{body}</StyledBody>
      <StyledActions>
        <Button
          variant="secondary"
          size="small"
          Icon={IconCopy}
          title={t`Copy subject`}
          onClick={handleCopySubject}
        />
        <Button
          variant="secondary"
          size="small"
          Icon={IconCopy}
          title={t`Copy body`}
          onClick={handleCopyBody}
        />
        <Button
          variant="secondary"
          size="small"
          Icon={IconCopy}
          title={t`Copy all`}
          onClick={handleCopyAll}
        />
      </StyledActions>
    </StyledContainer>
  );
};
