import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { useMemo } from 'react';
import { H2Title, IconPlus } from 'twenty-ui/display';
import { Button } from 'twenty-ui/input';
import { Section } from 'twenty-ui/layout';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import {
  type EditableSkill,
  type FollowupSkillKind,
  type FollowupSkillTab,
  skillBelongsToTab,
} from '~/pages/skills/constants/FollowupSkillCategories';

type FollowupSkillsListProps = {
  tab: FollowupSkillTab;
  skills: EditableSkill[];
  onCreate: (kind: FollowupSkillKind) => void;
  onEdit: (skill: EditableSkill) => void;
};

const StyledAddRow = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: ${themeCssVariables.spacing[2]};
  margin-bottom: ${themeCssVariables.spacing[4]};
`;

const StyledList = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledRow = styled.button`
  align-items: center;
  background: ${themeCssVariables.background.secondary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  cursor: pointer;
  display: flex;
  gap: ${themeCssVariables.spacing[3]};
  padding: ${themeCssVariables.spacing[3]} ${themeCssVariables.spacing[4]};
  text-align: left;
  width: 100%;

  &:hover {
    background: ${themeCssVariables.background.tertiary};
  }
`;

const StyledRowText = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[1]};
  min-width: 0;
`;

const StyledRowTitle = styled.span`
  color: ${themeCssVariables.font.color.primary};
  font-weight: ${themeCssVariables.font.weight.medium};
`;

const StyledRowPreview = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const StyledInactiveTag = styled.span`
  background: ${themeCssVariables.background.quaternary};
  border-radius: ${themeCssVariables.border.radius.sm};
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.xs};
  margin-left: auto;
  padding: ${themeCssVariables.spacing[1]} ${themeCssVariables.spacing[2]};
`;

const StyledEmpty = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
  padding: ${themeCssVariables.spacing[4]} 0;
`;

export const FollowupSkillsList = ({
  tab,
  skills,
  onCreate,
  onEdit,
}: FollowupSkillsListProps) => {
  const { t } = useLingui();

  const tabSkills = useMemo(
    () =>
      skills
        .filter((skill) => skillBelongsToTab(skill.name, tab))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [skills, tab],
  );

  return (
    <Section>
      <H2Title title={tab.title} description={tab.description} />

      <StyledAddRow>
        {tab.addableKinds.map((kind) => (
          <Button
            key={kind.id}
            Icon={IconPlus}
            title={t`New ${kind.label}`}
            variant="secondary"
            size="small"
            onClick={() => onCreate(kind)}
          />
        ))}
      </StyledAddRow>

      {tabSkills.length === 0 ? (
        <StyledEmpty>
          {t`No skills yet. Create one above — the agent falls back to its built-in defaults until you do.`}
        </StyledEmpty>
      ) : (
        <StyledList>
          {tabSkills.map((skill) => (
            <StyledRow key={skill.id} onClick={() => onEdit(skill)}>
              <StyledRowText>
                <StyledRowTitle>{skill.label}</StyledRowTitle>
                <StyledRowPreview>
                  {skill.description || skill.content.slice(0, 120)}
                </StyledRowPreview>
              </StyledRowText>
              {!skill.isActive && (
                <StyledInactiveTag>{t`Inactive`}</StyledInactiveTag>
              )}
            </StyledRow>
          ))}
        </StyledList>
      )}
    </Section>
  );
};
