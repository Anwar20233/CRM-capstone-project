import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { Section } from 'twenty-ui/layout';
import { themeCssVariables } from 'twenty-ui/theme-constants';

// Edits one of an agent's knowledge files (playbooks, frameworks, templates,
// catalogs) in place. Shown in the Skills form area when a file row is opened.
type AgentFileEditorProps = {
  content: string;
  onContentChange: (content: string) => void;
};

const StyledHint = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.xs};
  margin-bottom: ${themeCssVariables.spacing[3]};
`;

const StyledTextArea = styled.textarea`
  background: ${themeCssVariables.background.primary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  color: ${themeCssVariables.font.color.primary};
  font-family: monospace;
  font-size: ${themeCssVariables.font.size.sm};
  min-height: 520px;
  padding: ${themeCssVariables.spacing[3]};
  resize: vertical;
  white-space: pre;
  width: 100%;
`;

export const AgentFileEditor = ({
  content,
  onContentChange,
}: AgentFileEditorProps) => {
  const { t } = useLingui();

  return (
    <Section>
      <StyledHint>
        {t`Editing a built-in default. Changes apply on the agent's next run.`}
      </StyledHint>
      <StyledTextArea
        value={content}
        spellCheck={false}
        onChange={(event) => onContentChange(event.target.value)}
      />
    </Section>
  );
};
