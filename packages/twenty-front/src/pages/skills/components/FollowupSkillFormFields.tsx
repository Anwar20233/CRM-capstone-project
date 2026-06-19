import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import {
  H2Title,
  IconArchive,
  IconArchiveOff,
  IconTrash,
} from 'twenty-ui/display';
import { Button } from 'twenty-ui/input';
import { Section } from 'twenty-ui/layout';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { IconPicker } from '@/ui/input/components/IconPicker';
import { SettingsTextInput } from '@/ui/input/components/SettingsTextInput';
import { TextArea } from '@/ui/input/components/TextArea';
import { type EditableSkill } from '~/pages/skills/constants/FollowupSkillCategories';

type FollowupSkillFormFieldsProps = {
  label: string;
  description: string;
  content: string;
  icon: string;
  apiName: string;
  onLabelChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onContentChange: (value: string) => void;
  onIconChange: (value: string) => void;
  // Present only when editing an existing skill (enables the danger zone).
  skill?: EditableSkill;
  onToggleActive?: () => void;
  onDelete?: () => void;
};

const StyledFormContainer = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[4]};
`;

const StyledIconNameRow = styled.div`
  align-items: flex-start;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledNameContainer = styled.div`
  flex: 1 1 auto;
  min-width: 0;
`;

const StyledApiName = styled.span`
  color: ${themeCssVariables.font.color.light};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledDangerButtons = styled.div`
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

export const FollowupSkillFormFields = ({
  label,
  description,
  content,
  icon,
  apiName,
  onLabelChange,
  onDescriptionChange,
  onContentChange,
  onIconChange,
  skill,
  onToggleActive,
  onDelete,
}: FollowupSkillFormFieldsProps) => {
  const { t } = useLingui();

  return (
    <>
      <Section>
        <H2Title
          title={t`About`}
          description={t`Give the skill a clear name and a short description of when the agent should use it.`}
        />
        <StyledFormContainer>
          <StyledIconNameRow>
            <IconPicker
              selectedIconKey={icon || 'IconSparkles'}
              onChange={({ iconKey }) => onIconChange(iconKey)}
            />
            <StyledNameContainer>
              <SettingsTextInput
                instanceId="followup-skill-label"
                placeholder={t`Skill name`}
                value={label}
                onChange={onLabelChange}
                fullWidth
              />
            </StyledNameContainer>
          </StyledIconNameRow>

          <StyledApiName>{t`Reference:`} {apiName}</StyledApiName>

          <TextArea
            textAreaId="followup-skill-description"
            placeholder={t`When should the agent use this skill?`}
            minRows={2}
            value={description}
            onChange={(value) => onDescriptionChange(value ?? '')}
          />
        </StyledFormContainer>
      </Section>

      <Section>
        <H2Title
          title={t`Instructions`}
          description={t`Markdown the agent reads and follows. Write the guidance, tips, or template the way your company works.`}
        />
        <TextArea
          textAreaId="followup-skill-content"
          placeholder={t`Markdown the agent will read and follow…`}
          minRows={16}
          maxRows={40}
          value={content}
          onChange={(value) => onContentChange(value ?? '')}
        />
      </Section>

      {skill && (
        <Section>
          <H2Title
            title={t`Danger zone`}
            description={t`Deactivate to hide this skill from the agent, or delete it permanently.`}
          />
          <StyledDangerButtons>
            <Button
              Icon={skill.isActive ? IconArchive : IconArchiveOff}
              title={skill.isActive ? t`Deactivate` : t`Activate`}
              size="small"
              onClick={onToggleActive}
            />
            <Button
              Icon={IconTrash}
              title={t`Delete`}
              size="small"
              accent="danger"
              variant="secondary"
              onClick={onDelete}
            />
          </StyledDangerButtons>
        </Section>
      )}
    </>
  );
};
