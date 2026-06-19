import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { useState } from 'react';
import {
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconMail,
  IconX,
} from 'twenty-ui/display';
import { Button, Toggle } from 'twenty-ui/input';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import {
  type FollowupAction,
  type FollowupWorkflowStep,
} from '@/followup-intelligence/types/followup-action';

const StyledCard = styled.div`
  background: ${themeCssVariables.background.secondary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  padding: ${themeCssVariables.spacing[3]};
`;

const StyledCardHeader = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
  justify-content: space-between;
`;

const StyledHeaderTitle = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.md};
  font-weight: ${themeCssVariables.font.weight.semiBold};
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
  font-size: ${themeCssVariables.font.size.xs};
  font-weight: ${themeCssVariables.font.weight.medium};
  padding: ${themeCssVariables.spacing[1]} ${themeCssVariables.spacing[2]};
  text-transform: capitalize;
`;

const StyledDisclosure = styled.button`
  align-items: center;
  background: transparent;
  border: none;
  color: ${themeCssVariables.font.color.secondary};
  cursor: pointer;
  display: flex;
  font-family: inherit;
  font-size: ${themeCssVariables.font.size.sm};
  gap: ${themeCssVariables.spacing[1]};
  padding: 0;
  text-align: left;
`;

const StyledExpandedBox = styled.div`
  background: ${themeCssVariables.background.transparent.lighter};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.sm};
  line-height: 1.5;
  padding: ${themeCssVariables.spacing[2]};
  white-space: pre-wrap;
  word-break: break-word;
`;

const StyledStepRow = styled.div<{ disabled: boolean }>`
  align-items: flex-start;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
  opacity: ${({ disabled }) => (disabled ? 0.45 : 1)};
`;

const StyledStepBody = styled.div`
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[1]};
  min-width: 0;
`;

const StyledStepTitle = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.md};
`;

const StyledStepKind = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-weight: ${themeCssVariables.font.weight.medium};
`;

const StyledEmailSubject = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-weight: ${themeCssVariables.font.weight.medium};
  margin-bottom: ${themeCssVariables.spacing[1]};
`;

const StyledActions = styled.div`
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
  margin-top: ${themeCssVariables.spacing[1]};
`;

const STEP_KIND_LABEL: Record<string, string> = {
  create_task: 'New task',
  create_note: 'New note',
  update_stage: 'Update deal',
  draft_email: 'Email draft',
  book_meeting: 'Calendar booking',
};

const formatMeeting = (start: string | null, end: string | null) => {
  if (!start) {
    return null;
  }

  const startDate = new Date(start);
  const day = startDate.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
  const time = startDate.toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  });
  const endTime = end
    ? new Date(end).toLocaleTimeString(undefined, {
        hour: 'numeric',
        minute: '2-digit',
      })
    : null;

  return `${day} · ${time}${endTime ? ` – ${endTime}` : ''}`;
};

type WorkflowStepRowProps = {
  step: FollowupWorkflowStep;
  enabled: boolean;
  onToggle: (index: number, enabled: boolean) => void;
};

const WorkflowStepRow = ({ step, enabled, onToggle }: WorkflowStepRowProps) => {
  const { t } = useLingui();
  const [expanded, setExpanded] = useState(false);

  const kindLabel =
    STEP_KIND_LABEL[step.kind] ?? step.kind.replaceAll('_', ' ');
  const meetingLabel = formatMeeting(step.meeting_start, step.meeting_end);
  const summary =
    step.kind === 'book_meeting' && meetingLabel ? meetingLabel : step.title;

  const hasDetail =
    Boolean(step.detail) ||
    Boolean(step.email_body) ||
    step.invitees.length > 0;

  return (
    <StyledStepRow disabled={!enabled}>
      <Toggle
        value={enabled}
        onChange={(value) => onToggle(step.index, value)}
      />
      <StyledStepBody>
        <StyledDisclosure
          type="button"
          onClick={() => hasDetail && setExpanded((prev) => !prev)}
        >
          {hasDetail &&
            (expanded ? (
              <IconChevronDown size={14} />
            ) : (
              <IconChevronRight size={14} />
            ))}
          <StyledStepTitle>
            <StyledStepKind>{kindLabel}:</StyledStepKind> {summary}
          </StyledStepTitle>
        </StyledDisclosure>
        {expanded && (
          <StyledExpandedBox>
            {step.kind === 'draft_email' && step.email_subject && (
              <StyledEmailSubject>{step.email_subject}</StyledEmailSubject>
            )}
            {step.kind === 'draft_email' && step.email_body
              ? step.email_body
              : null}
            {step.kind === 'book_meeting' && step.invitees.length > 0 && (
              <div>{t`Invitees: ${step.invitees.join(', ')}`}</div>
            )}
            {step.kind !== 'draft_email' && step.detail ? step.detail : null}
          </StyledExpandedBox>
        )}
      </StyledStepBody>
    </StyledStepRow>
  );
};

type WorkflowCardProps = {
  action: FollowupAction;
  isMutating: boolean;
  onAccept: (actionId: string, disabledStepIndices: number[]) => Promise<void>;
  onReject: (actionId: string) => Promise<void>;
};

export const WorkflowCard = ({
  action,
  isMutating,
  onAccept,
  onReject,
}: WorkflowCardProps) => {
  const { t } = useLingui();
  const [emailExpanded, setEmailExpanded] = useState(false);
  // Steps enabled by default; the rep toggles off the ones they don't want.
  const [enabledByIndex, setEnabledByIndex] = useState<Record<number, boolean>>(
    {},
  );

  const isStepEnabled = (index: number) => enabledByIndex[index] !== false;

  const handleToggle = (index: number, enabled: boolean) =>
    setEnabledByIndex((prev) => ({ ...prev, [index]: enabled }));

  const handleAccept = () => {
    const disabled = action.steps
      .filter((step) => !isStepEnabled(step.index))
      .map((step) => step.index);
    return onAccept(action.id, disabled);
  };

  const allDisabled =
    action.steps.length > 0 &&
    action.steps.every((step) => !isStepEnabled(step.index));

  return (
    <StyledCard>
      <StyledCardHeader>
        <StyledHeaderTitle>
          {action.source_email ? t`Workflow from email` : t`Suggested workflow`}
        </StyledHeaderTitle>
        <StyledBadge urgency={action.urgency}>{action.urgency}</StyledBadge>
      </StyledCardHeader>

      {action.source_email && (
        <>
          <StyledDisclosure
            type="button"
            onClick={() => setEmailExpanded((prev) => !prev)}
          >
            {emailExpanded ? (
              <IconChevronDown size={14} />
            ) : (
              <IconChevronRight size={14} />
            )}
            <IconMail size={14} />
            {action.source_email.subject ?? t`Triggering email`}
          </StyledDisclosure>
          {emailExpanded && (
            <StyledExpandedBox>
              {action.source_email.sender_email && (
                <StyledEmailSubject>
                  {t`From: ${action.source_email.sender_email}`}
                </StyledEmailSubject>
              )}
              {action.source_email.body}
            </StyledExpandedBox>
          )}
        </>
      )}

      {action.steps.map((step) => (
        <WorkflowStepRow
          key={step.index}
          step={step}
          enabled={isStepEnabled(step.index)}
          onToggle={handleToggle}
        />
      ))}

      <StyledActions>
        <Button
          variant="primary"
          accent="blue"
          size="small"
          Icon={IconCheck}
          title={t`Accept`}
          disabled={isMutating || allDisabled}
          onClick={() => void handleAccept()}
        />
        <Button
          variant="secondary"
          accent="danger"
          size="small"
          Icon={IconX}
          title={t`Reject`}
          disabled={isMutating}
          onClick={() => void onReject(action.id)}
        />
      </StyledActions>
    </StyledCard>
  );
};
