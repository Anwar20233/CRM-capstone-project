import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { useMemo } from 'react';
import { isDefined } from 'twenty-shared/utils';
import { IconPlus, useIcons } from 'twenty-ui/display';
import { Button } from 'twenty-ui/input';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import { type AgentFile } from '@/followup-intelligence/services/agent-files-api';
import { SkillCard } from '~/pages/skills/components/SkillCard';
import {
  type EditableSkill,
  type FollowupSkillKind,
  type FollowupSkillSection as Section,
} from '~/pages/skills/types/FollowupSkill';

type FollowupSkillSectionProps = {
  section: Section;
  skills: EditableSkill[];
  files: AgentFile[];
  onCreate: (kind: FollowupSkillKind) => void;
  onEdit: (skill: EditableSkill) => void;
  onEditFile: (file: AgentFile) => void;
};

const StyledSection = styled.section`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[3]};
  margin-bottom: ${themeCssVariables.spacing[8]};
`;

const StyledHeader = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[3]};
`;

const StyledIconBadge = styled.div`
  align-items: center;
  background: ${themeCssVariables.background.tertiary};
  border-radius: ${themeCssVariables.border.radius.md};
  color: ${themeCssVariables.font.color.secondary};
  display: flex;
  height: 32px;
  justify-content: center;
  width: 32px;
`;

const StyledHeaderText = styled.div`
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[1]};
  min-width: 0;
`;

const StyledTitleRow = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
`;

const StyledTitle = styled.span`
  color: ${themeCssVariables.font.color.primary};
  font-size: ${themeCssVariables.font.size.md};
  font-weight: ${themeCssVariables.font.weight.semiBold};
`;

const StyledCount = styled.span`
  background: ${themeCssVariables.background.quaternary};
  border-radius: ${themeCssVariables.border.radius.rounded};
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.xs};
  padding: 0 ${themeCssVariables.spacing[2]};
`;

const StyledDescription = styled.span`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
`;

const StyledSpacer = styled.div`
  flex: 1;
`;

const StyledGrid = styled.div`
  display: grid;
  gap: ${themeCssVariables.spacing[2]};
  grid-template-columns: repeat(auto-fill, minmax(264px, 1fr));
`;

const StyledEmpty = styled.div`
  border: 1px dashed ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
  padding: ${themeCssVariables.spacing[4]};
  text-align: center;
`;

export const FollowupSkillSection = ({
  section,
  skills,
  files,
  onCreate,
  onEdit,
  onEditFile,
}: FollowupSkillSectionProps) => {
  const { t } = useLingui();
  const { getIcon } = useIcons();
  const HeaderIcon = getIcon(section.icon);

  const sectionSkills = useMemo(
    () =>
      skills
        .filter(
          (skill) =>
            section.skillPrefixes.some((prefix) =>
              skill.name.startsWith(prefix),
            ) || section.skillSingletons.includes(skill.name),
        )
        .sort((a, b) => a.label.localeCompare(b.label)),
    [skills, section],
  );

  const sectionFiles = useMemo(
    () =>
      files
        .filter((file) => section.fileFolders.includes(file.folder))
        .sort((a, b) => a.title.localeCompare(b.title)),
    [files, section],
  );

  const count = sectionSkills.length + sectionFiles.length;

  return (
    <StyledSection>
      <StyledHeader>
        <StyledIconBadge>
          {isDefined(HeaderIcon) ? <HeaderIcon size={18} /> : null}
        </StyledIconBadge>
        <StyledHeaderText>
          <StyledTitleRow>
            <StyledTitle>{section.title}</StyledTitle>
            {count > 0 && <StyledCount>{count}</StyledCount>}
          </StyledTitleRow>
          <StyledDescription>{section.description}</StyledDescription>
        </StyledHeaderText>
        <StyledSpacer />
        {section.addable && (
          <Button
            Icon={IconPlus}
            title={t`Add`}
            variant="secondary"
            size="small"
            onClick={() => onCreate(section.addable as FollowupSkillKind)}
          />
        )}
      </StyledHeader>

      {count === 0 ? (
        <StyledEmpty>
          {section.addable
            ? t`Nothing here yet — add one, or the agent uses its built-in defaults.`
            : t`No items in this section yet.`}
        </StyledEmpty>
      ) : (
        <StyledGrid>
          {sectionSkills.map((skill) => (
            <SkillCard
              key={skill.id}
              title={skill.label}
              subtitle={skill.description || skill.content}
              tagText={skill.isActive ? t`Custom` : t`Inactive`}
              tagColor={skill.isActive ? section.tagColor : 'gray'}
              muted={!skill.isActive}
              onClick={() => onEdit(skill)}
            />
          ))}
          {sectionFiles.map((file) => (
            <SkillCard
              key={file.path}
              title={file.title}
              subtitle={file.preview}
              tagText={t`Default`}
              tagColor="gray"
              onClick={() => onEditFile(file)}
            />
          ))}
        </StyledGrid>
      )}
    </StyledSection>
  );
};
