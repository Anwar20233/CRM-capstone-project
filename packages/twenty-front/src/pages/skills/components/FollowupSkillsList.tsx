import { styled } from '@linaria/react';
import { useLingui } from '@lingui/react/macro';
import { useEffect, useMemo, useState } from 'react';
import { H2Title } from 'twenty-ui/display';
import { Section } from 'twenty-ui/layout';
import { themeCssVariables } from 'twenty-ui/theme-constants';

import {
  type AgentFile,
  listAgentFiles,
} from '@/followup-intelligence/services/agent-files-api';
import { FollowupSkillSection } from '~/pages/skills/components/FollowupSkillSection';
import {
  type EditableSkill,
  type FollowupSkillKind,
  type FollowupSkillTab,
} from '~/pages/skills/types/FollowupSkill';

type FollowupSkillsListProps = {
  tab: FollowupSkillTab;
  skills: EditableSkill[];
  onCreate: (kind: FollowupSkillKind) => void;
  onEdit: (skill: EditableSkill) => void;
  onEditFile: (file: AgentFile) => void;
};

const StyledNotice = styled.div`
  color: ${themeCssVariables.font.color.tertiary};
  font-size: ${themeCssVariables.font.size.sm};
  margin-bottom: ${themeCssVariables.spacing[4]};
`;

export const FollowupSkillsList = ({
  tab,
  skills,
  onCreate,
  onEdit,
  onEditFile,
}: FollowupSkillsListProps) => {
  const { t } = useLingui();

  const [agentFiles, setAgentFiles] = useState<AgentFile[] | undefined>(
    undefined,
  );
  const [filesError, setFilesError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    listAgentFiles()
      .then((files) => !cancelled && setAgentFiles(files))
      .catch(() => !cancelled && setFilesError(true));
    return () => {
      cancelled = true;
    };
  }, []);

  const tabFiles = useMemo(
    () => (agentFiles ?? []).filter((file) => file.agent === tab.agent),
    [agentFiles, tab],
  );

  return (
    <Section>
      <H2Title title={tab.title} description={tab.description} />

      {filesError && (
        <StyledNotice>
          {t`Couldn't load the agent's built-in defaults — only your custom skills are shown. Is the AI service running?`}
        </StyledNotice>
      )}

      {tab.sections.map((section) => (
        <FollowupSkillSection
          key={section.id}
          section={section}
          skills={skills}
          files={tabFiles}
          onCreate={onCreate}
          onEdit={onEdit}
          onEditFile={onEditFile}
        />
      ))}
    </Section>
  );
};
