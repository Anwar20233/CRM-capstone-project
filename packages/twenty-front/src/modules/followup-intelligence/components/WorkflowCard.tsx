import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { type ComponentType, useState } from 'react';
import {
  IconAlertTriangle,
  IconArrowRight,
  IconCalendarEvent,
  IconCheck,
  IconCheckbox,
  IconChevronDown,
  IconChevronRight,
  IconInfoCircle,
  IconMail,
  IconNotes,
  IconPencil,
  IconX,
  type TablerIconsProps,
} from 'twenty-ui/display';
import { Button, Toggle } from 'twenty-ui/input';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { type FollowupStepEdit } from '@/followup-intelligence/services/followup-api';
import {
  type FollowupAction,
  type FollowupWorkflowStep,
} from '@/followup-intelligence/types/followup-action';

const StyledCard = styled.div`
  background: ${themeCssVariables.background.secondary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  box-shadow: ${themeCssVariables.boxShadow.light};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[3]};
  padding: ${themeCssVariables.spacing[4]};
`;

const StyledCardHeader = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
  justify-content: space-between;
`;

const StyledHeaderTitle = styled.div`
  align-items: center;
  color: ${themeCssVariables.font.color.primary};
  display: flex;
  font-size: ${themeCssVariables.font.size.md};
  font-weight: ${themeCssVariables.font.weight.semiBold};
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledUrgencyPill = styled.span<{ urgency: string }>`
  background: ${({ urgency }) =>
    urgency === 'high'
      ? themeCssVariables.background.transparent.danger
      : urgency === 'low'
        ? themeCssVariables.background.transparent.success
        : themeCssVariables.background.transparent.orange};
  border-radius: ${themeCssVariables.border.radius.pill};
  color: ${({ urgency }) =>
    urgency === 'high'
      ? themeCssVariables.color.red
      : urgency === 'low'
        ? themeCssVariables.color.turquoise
        : themeCssVariables.color.orange};
  font-size: ${themeCssVariables.font.size.xs};
  font-weight: ${themeCssVariables.font.weight.semiBold};
  letter-spacing: 0.03em;
  padding: ${themeCssVariables.spacing[1]} ${themeCssVariables.spacing[2]};
  text-transform: uppercase;
`;

// "Why this workflow" — an info callout with a blue accent edge so the rep can
// instantly see the cause (the triggering email + the agent's rationale).
const StyledCausePanel = styled.div`
  background: ${themeCssVariables.background.transparent.blue};
  border: 1px solid ${themeCssVariables.color.transparent.blue2};
  border-left: 3px solid ${themeCssVariables.color.blue};
  border-radius: ${themeCssVariables.border.radius.sm};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  padding: ${themeCssVariables.spacing[3]};
`;

const StyledCauseHeader = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[1]};
  justify-content: space-between;
`;

const StyledCauseLabel = styled.div`
  align-items: center;
  color: ${themeCssVariables.color.blue};
  display: flex;
  font-size: ${themeCssVariables.font.size.sm};
  font-weight: ${themeCssVariables.font.weight.semiBold};
  gap: ${themeCssVariables.spacing[1]};
  min-width: 0;
`;

const StyledIconButton = styled.button`
  align-items: center;
  background: transparent;
  border: none;
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${themeCssVariables.font.color.tertiary};
  cursor: pointer;
  display: flex;
  flex-shrink: 0;
  padding: ${themeCssVariables.spacing[1]};
  transition: background 0.1s ease;

  &:hover {
    background: ${themeCssVariables.background.transparent.light};
    color: ${themeCssVariables.font.color.primary};
  }

  &:disabled {
    cursor: default;
    opacity: 0.4;
  }
`;

// The triggering email, rendered as a quoted block so it reads like a real
// message the rep is responding to.
const StyledEmailQuote = styled.div`
  border-left: 2px solid ${themeCssVariables.border.color.medium};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[1]};
  padding-left: ${themeCssVariables.spacing[2]};
`;

const StyledEmailFrom = styled.div`
  align-items: center;
  color: ${themeCssVariables.font.color.primary};
  display: flex;
  font-size: ${themeCssVariables.font.size.sm};
  font-weight: ${themeCssVariables.font.weight.medium};
  gap: ${themeCssVariables.spacing[1]};
`;

const StyledEmailBody = styled.div`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.sm};
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
`;

const StyledReasoning = styled.div`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.sm};
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
`;

const StyledShowCause = styled.button`
  align-items: center;
  align-self: flex-start;
  background: transparent;
  border: none;
  color: ${themeCssVariables.color.blue};
  cursor: pointer;
  display: flex;
  font-family: inherit;
  font-size: ${themeCssVariables.font.size.sm};
  font-weight: ${themeCssVariables.font.weight.medium};
  gap: ${themeCssVariables.spacing[1]};
  padding: 0;
`;

const StyledSteps = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledStep = styled.div<{ disabled: boolean }>`
  background: ${themeCssVariables.background.primary};
  border: 1px solid ${themeCssVariables.border.color.light};
  border-radius: ${themeCssVariables.border.radius.sm};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  opacity: ${({ disabled }) => (disabled ? 0.55 : 1)};
  padding: ${themeCssVariables.spacing[2]} ${themeCssVariables.spacing[3]};
  transition: border-color 0.1s ease;

  &:hover {
    border-color: ${themeCssVariables.border.color.medium};
  }
`;

const StyledStepMain = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledStepIcon = styled.div`
  align-items: center;
  background: ${themeCssVariables.background.transparent.light};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${themeCssVariables.font.color.secondary};
  display: flex;
  flex-shrink: 0;
  height: 28px;
  justify-content: center;
  width: 28px;
`;

const StyledStepText = styled.div`
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
`;

const StyledStepKindLabel = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.xs};
  font-weight: ${themeCssVariables.font.weight.medium};
  letter-spacing: 0.03em;
  text-transform: uppercase;
`;

const StyledStepTitle = styled.div`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.md};
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const StyledStepControls = styled.div`
  align-items: center;
  display: flex;
  flex-shrink: 0;
  gap: ${themeCssVariables.spacing[1]};
`;

const StyledEditPanel = styled.div`
  border-top: 1px solid ${themeCssVariables.border.color.light};
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  padding-top: ${themeCssVariables.spacing[2]};
`;

const StyledField = styled.label`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[1]};
`;

const StyledFieldLabel = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.xs};
  font-weight: ${themeCssVariables.font.weight.medium};
`;

const StyledInput = styled.input`
  background: ${themeCssVariables.background.secondary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.sm};
  box-sizing: border-box;
  color: ${themeCssVariables.font.color.primary};
  font-family: inherit;
  font-size: ${themeCssVariables.font.size.sm};
  padding: ${themeCssVariables.spacing[2]};
  width: 100%;

  &:focus {
    border-color: ${themeCssVariables.color.blue};
    outline: none;
  }
`;

const StyledTextarea = styled.textarea`
  background: ${themeCssVariables.background.secondary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.sm};
  box-sizing: border-box;
  color: ${themeCssVariables.font.color.primary};
  font-family: inherit;
  font-size: ${themeCssVariables.font.size.sm};
  line-height: 1.5;
  min-height: 104px;
  padding: ${themeCssVariables.spacing[2]};
  resize: vertical;
  width: 100%;

  &:focus {
    border-color: ${themeCssVariables.color.blue};
    outline: none;
  }
`;

const StyledMetaLine = styled.div`
  color: ${themeCssVariables.font.color.secondary};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledActions = styled.div`
  align-items: center;
  border-top: 1px solid ${themeCssVariables.border.color.light};
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
  padding-top: ${themeCssVariables.spacing[3]};
`;

const StyledSpacer = styled.div`
  flex: 1;
`;

type StepIcon = ComponentType<TablerIconsProps>;

const STEP_KIND_LABEL: Record<string, string> = {
  create_task: 'Task',
  create_note: 'Note',
  write_note: 'Note',
  update_stage: 'Update deal',
  draft_email: 'Email',
  book_meeting: 'Meeting',
};

const STEP_KIND_ICON: Record<string, StepIcon> = {
  draft_email: IconMail,
  write_note: IconNotes,
  create_note: IconNotes,
  create_task: IconCheckbox,
  book_meeting: IconCalendarEvent,
  update_stage: IconArrowRight,
};

// Steps whose content the rep can edit by hand before accepting.
const EDITABLE_KINDS = new Set([
  'draft_email',
  'write_note',
  'create_task',
  'book_meeting',
  'update_stage',
]);

// Per-step pending edits, mirroring the StepEdit fields the API accepts.
type StepEditDraft = {
  email_subject?: string;
  email_body?: string;
  title?: string;
  detail?: string;
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
  editDraft: StepEditDraft | undefined;
  isEditing: boolean;
  disabled: boolean;
  onToggle: (index: number, enabled: boolean) => void;
  onToggleEditing: (index: number) => void;
  onEditField: (
    index: number,
    field: keyof StepEditDraft,
    value: string,
  ) => void;
};

const WorkflowStepRow = ({
  step,
  enabled,
  editDraft,
  isEditing,
  disabled,
  onToggle,
  onToggleEditing,
  onEditField,
}: WorkflowStepRowProps) => {
  const { t } = useLingui();

  const kindLabel =
    STEP_KIND_LABEL[step.kind] ?? step.kind.replaceAll('_', ' ');
  const StepIconComponent = STEP_KIND_ICON[step.kind] ?? IconAlertTriangle;
  const meetingLabel = formatMeeting(step.meeting_start, step.meeting_end);
  const summary =
    step.kind === 'book_meeting' && meetingLabel
      ? meetingLabel
      : step.kind === 'draft_email'
        ? (step.email_subject ?? step.title)
        : step.title;

  const isEditable = EDITABLE_KINDS.has(step.kind);

  // Current value of an editable field: the rep's pending edit, else what the
  // agent authored.
  const valueOf = (field: keyof StepEditDraft, original: string | null) =>
    editDraft?.[field] ?? original ?? '';

  return (
    <StyledStep disabled={!enabled}>
      <StyledStepMain>
        <StyledStepIcon>
          <StepIconComponent size={16} />
        </StyledStepIcon>
        <StyledStepText>
          <StyledStepKindLabel>{kindLabel}</StyledStepKindLabel>
          <StyledStepTitle>{summary}</StyledStepTitle>
        </StyledStepText>
        <StyledStepControls>
          {isEditable && (
            <StyledIconButton
              type="button"
              disabled={disabled}
              onClick={() => onToggleEditing(step.index)}
              title={isEditing ? t`Done` : t`Edit`}
            >
              {isEditing ? (
                <IconChevronDown size={14} />
              ) : (
                <IconPencil size={14} />
              )}
            </StyledIconButton>
          )}
          <Toggle
            value={enabled}
            onChange={(value) => onToggle(step.index, value)}
          />
        </StyledStepControls>
      </StyledStepMain>

      {isEditing && step.kind === 'draft_email' && (
        <StyledEditPanel>
          <StyledField>
            <StyledFieldLabel>{t`Subject`}</StyledFieldLabel>
            <StyledInput
              value={valueOf('email_subject', step.email_subject)}
              disabled={disabled}
              onChange={(event) =>
                onEditField(step.index, 'email_subject', event.target.value)
              }
            />
          </StyledField>
          <StyledField>
            <StyledFieldLabel>{t`Body`}</StyledFieldLabel>
            <StyledTextarea
              value={valueOf('email_body', step.email_body)}
              disabled={disabled}
              onChange={(event) =>
                onEditField(step.index, 'email_body', event.target.value)
              }
            />
          </StyledField>
        </StyledEditPanel>
      )}

      {isEditing && step.kind === 'write_note' && (
        <StyledEditPanel>
          <StyledField>
            <StyledFieldLabel>{t`Note`}</StyledFieldLabel>
            <StyledTextarea
              value={valueOf('detail', step.detail)}
              disabled={disabled}
              onChange={(event) =>
                onEditField(step.index, 'detail', event.target.value)
              }
            />
          </StyledField>
        </StyledEditPanel>
      )}

      {isEditing && step.kind === 'create_task' && (
        <StyledEditPanel>
          <StyledField>
            <StyledFieldLabel>{t`Task`}</StyledFieldLabel>
            <StyledInput
              value={valueOf('title', step.title)}
              disabled={disabled}
              onChange={(event) =>
                onEditField(step.index, 'title', event.target.value)
              }
            />
          </StyledField>
        </StyledEditPanel>
      )}

      {isEditing && step.kind === 'book_meeting' && (
        <StyledEditPanel>
          {meetingLabel && (
            <StyledMetaLine>{t`When: ${meetingLabel}`}</StyledMetaLine>
          )}
          <StyledField>
            <StyledFieldLabel>{t`Meeting title`}</StyledFieldLabel>
            <StyledInput
              value={valueOf('title', step.title)}
              disabled={disabled}
              onChange={(event) =>
                onEditField(step.index, 'title', event.target.value)
              }
            />
          </StyledField>
          {step.invitees.length > 0 && (
            <StyledMetaLine>
              {t`Invitees: ${step.invitees.join(', ')}`}
            </StyledMetaLine>
          )}
        </StyledEditPanel>
      )}

      {isEditing && step.kind === 'update_stage' && (
        <StyledEditPanel>
          <StyledField>
            <StyledFieldLabel>{t`Desired change`}</StyledFieldLabel>
            <StyledTextarea
              value={valueOf('detail', step.detail)}
              disabled={disabled}
              onChange={(event) =>
                onEditField(step.index, 'detail', event.target.value)
              }
            />
          </StyledField>
        </StyledEditPanel>
      )}
    </StyledStep>
  );
};

type WorkflowCardProps = {
  action: FollowupAction;
  isMutating: boolean;
  onAccept: (actionId: string, disabledStepIndices: number[]) => Promise<void>;
  onReject: (actionId: string) => Promise<void>;
  onEdit: (actionId: string, steps: FollowupStepEdit[]) => Promise<unknown>;
};

export const WorkflowCard = ({
  action,
  isMutating,
  onAccept,
  onReject,
  onEdit,
}: WorkflowCardProps) => {
  const { t } = useLingui();
  // The cause stays collapsed by default to keep the card compact — it's one
  // click away via the "Why this workflow" toggle when the rep wants context.
  const [showCause, setShowCause] = useState(false);
  // Steps enabled by default; the rep toggles off the ones they don't want.
  const [enabledByIndex, setEnabledByIndex] = useState<Record<number, boolean>>(
    {},
  );
  const [editingByIndex, setEditingByIndex] = useState<Record<number, boolean>>(
    {},
  );
  const [editsByIndex, setEditsByIndex] = useState<
    Record<number, StepEditDraft>
  >({});

  const isStepEnabled = (index: number) => enabledByIndex[index] !== false;

  const handleToggle = (index: number, enabled: boolean) =>
    setEnabledByIndex((prev) => ({ ...prev, [index]: enabled }));

  const handleToggleEditing = (index: number) =>
    setEditingByIndex((prev) => ({ ...prev, [index]: !prev[index] }));

  const handleEditField = (
    index: number,
    field: keyof StepEditDraft,
    value: string,
  ) =>
    setEditsByIndex((prev) => ({
      ...prev,
      [index]: { ...prev[index], [field]: value },
    }));

  // Only ship fields the rep actually changed from what the agent authored.
  const buildStepEdits = (): FollowupStepEdit[] => {
    const result: FollowupStepEdit[] = [];
    for (const step of action.steps) {
      const draft = editsByIndex[step.index];
      if (draft === undefined) {
        continue;
      }
      const entry: FollowupStepEdit = { index: step.index };
      let changed = false;
      if (
        draft.email_subject !== undefined &&
        draft.email_subject !== (step.email_subject ?? '')
      ) {
        entry.email_subject = draft.email_subject;
        changed = true;
      }
      if (
        draft.email_body !== undefined &&
        draft.email_body !== (step.email_body ?? '')
      ) {
        entry.email_body = draft.email_body;
        changed = true;
      }
      if (draft.title !== undefined && draft.title !== (step.title ?? '')) {
        entry.title = draft.title;
        changed = true;
      }
      if (draft.detail !== undefined && draft.detail !== (step.detail ?? '')) {
        entry.detail = draft.detail;
        changed = true;
      }
      if (changed) {
        result.push(entry);
      }
    }
    return result;
  };

  const stepEdits = buildStepEdits();
  const isDirty = stepEdits.length > 0;

  const persistEdits = async () => {
    if (!isDirty) {
      return;
    }
    await onEdit(action.id, stepEdits);
    // The refreshed action (with saved values) replaces this card's props.
    setEditsByIndex({});
    setEditingByIndex({});
  };

  const handleSave = () => persistEdits();

  const handleAccept = async () => {
    // Persist any manual edits first so the executor runs the rep's content.
    await persistEdits();
    const disabled = action.steps
      .filter((step) => !isStepEnabled(step.index))
      .map((step) => step.index);
    return onAccept(action.id, disabled);
  };

  const allDisabled =
    action.steps.length > 0 &&
    action.steps.every((step) => !isStepEnabled(step.index));

  // The "cause": the agent's rationale (always present) plus the triggering
  // email when we have it. Shown so the rep sees WHY this workflow exists.
  const hasReasoning = action.reasoning !== null && action.reasoning !== '';
  const hasCause = hasReasoning || action.source_email !== null;

  return (
    <StyledCard>
      <StyledCardHeader>
        <StyledHeaderTitle>
          {action.source_email ? t`Workflow from email` : t`Suggested workflow`}
        </StyledHeaderTitle>
        <StyledUrgencyPill urgency={action.urgency}>
          {action.urgency}
        </StyledUrgencyPill>
      </StyledCardHeader>

      {hasCause &&
        (showCause ? (
          <StyledCausePanel>
            <StyledCauseHeader>
              <StyledCauseLabel>
                <IconInfoCircle size={14} />
                {t`Why this workflow`}
              </StyledCauseLabel>
              <StyledIconButton
                type="button"
                onClick={() => setShowCause(false)}
                title={t`Hide`}
              >
                <IconX size={14} />
              </StyledIconButton>
            </StyledCauseHeader>

            {action.source_email && (
              <StyledEmailQuote>
                <StyledEmailFrom>
                  <IconMail size={13} />
                  {action.source_email.sender_email
                    ? t`From ${action.source_email.sender_email}`
                    : t`Triggering email`}
                  {action.source_email.subject
                    ? ` — ${action.source_email.subject}`
                    : ''}
                </StyledEmailFrom>
                {action.source_email.body && (
                  <StyledEmailBody>{action.source_email.body}</StyledEmailBody>
                )}
              </StyledEmailQuote>
            )}

            {hasReasoning && (
              <StyledReasoning>{action.reasoning}</StyledReasoning>
            )}
          </StyledCausePanel>
        ) : (
          <StyledShowCause type="button" onClick={() => setShowCause(true)}>
            <IconChevronRight size={14} />
            {t`Why this workflow`}
          </StyledShowCause>
        ))}

      <StyledSteps>
        {action.steps.map((step) => (
          <WorkflowStepRow
            key={step.index}
            step={step}
            enabled={isStepEnabled(step.index)}
            editDraft={editsByIndex[step.index]}
            isEditing={Boolean(editingByIndex[step.index])}
            disabled={isMutating}
            onToggle={handleToggle}
            onToggleEditing={handleToggleEditing}
            onEditField={handleEditField}
          />
        ))}
      </StyledSteps>

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
        {isDirty && (
          <Button
            variant="secondary"
            size="small"
            Icon={IconPencil}
            title={t`Save changes`}
            disabled={isMutating}
            onClick={() => void handleSave()}
          />
        )}
        <StyledSpacer />
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
