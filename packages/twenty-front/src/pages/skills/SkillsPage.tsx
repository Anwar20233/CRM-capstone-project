import { CombinedGraphQLErrors } from '@apollo/client/errors';
import { useMutation, useQuery } from '@apollo/client/react';
import { useLingui } from '@lingui/react/macro';
import { useState } from 'react';

import { SaveAndCancelButtons } from '@/settings/components/SaveAndCancelButtons/SaveAndCancelButtons';
import { SettingsPageContainer } from '@/settings/components/SettingsPageContainer';
import { useSnackBar } from '@/ui/feedback/snack-bar-manager/hooks/useSnackBar';
import { ConfirmationModal } from '@/ui/layout/modal/components/ConfirmationModal';
import { useModal } from '@/ui/layout/modal/hooks/useModal';
import { SubMenuTopBarContainer } from '@/ui/layout/page/components/SubMenuTopBarContainer';
import { TabList } from '@/ui/layout/tab-list/components/TabList';
import { activeTabIdComponentState } from '@/ui/layout/tab-list/states/activeTabIdComponentState';
import { useAtomComponentStateValue } from '@/ui/utilities/state/jotai/hooks/useAtomComponentStateValue';
import {
  ActivateSkillDocument,
  CreateSkillDocument,
  DeactivateSkillDocument,
  DeleteSkillDocument,
  FindManySkillsDocument,
  UpdateSkillDocument,
} from '~/generated-metadata/graphql';
import { FollowupSkillFormFields } from '~/pages/skills/components/FollowupSkillFormFields';
import { FollowupSkillsList } from '~/pages/skills/components/FollowupSkillsList';
import {
  type EditableSkill,
  FOLLOWUP_SKILL_TABS,
  type FollowupSkillKind,
  slugifySkillKey,
} from '~/pages/skills/constants/FollowupSkillCategories';

// Top-level Skills tab: lets a company tune how its follow-up agents plan and
// draft. Only the agents' own skills are shown, grouped into Planner and Email
// Drafting tabs. Reuses Twenty's settings page shell and form primitives so it
// matches the rest of the product.
const SKILLS_TAB_LIST_INSTANCE_ID = 'followup-skills-tab-list';
const DELETE_SKILL_MODAL_ID = 'delete-followup-skill-modal';

type View =
  | { mode: 'list' }
  | { mode: 'create'; kind: FollowupSkillKind }
  | { mode: 'edit'; skill: EditableSkill };

export const SkillsPage = () => {
  const { t } = useLingui();
  const { enqueueSuccessSnackBar, enqueueErrorSnackBar } = useSnackBar();
  const { openModal, closeModal } = useModal();

  const { data, refetch } = useQuery(FindManySkillsDocument);
  const skills = (data?.skills ?? []) as EditableSkill[];

  const activeTabId = useAtomComponentStateValue(
    activeTabIdComponentState,
    SKILLS_TAB_LIST_INSTANCE_ID,
  );
  const activeTab =
    FOLLOWUP_SKILL_TABS.find((tab) => tab.id === activeTabId) ??
    FOLLOWUP_SKILL_TABS[0];

  const [view, setView] = useState<View>({ mode: 'list' });
  const [label, setLabel] = useState('');
  const [description, setDescription] = useState('');
  const [content, setContent] = useState('');
  const [icon, setIcon] = useState('IconSparkles');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [createSkill] = useMutation(CreateSkillDocument);
  const [updateSkill] = useMutation(UpdateSkillDocument);
  const [deleteSkill] = useMutation(DeleteSkillDocument);
  const [activateSkill] = useMutation(ActivateSkillDocument);
  const [deactivateSkill] = useMutation(DeactivateSkillDocument);

  const backToList = () => setView({ mode: 'list' });

  const enterCreate = (kind: FollowupSkillKind) => {
    setLabel('');
    setDescription('');
    setContent('');
    setIcon(kind.icon);
    setView({ mode: 'create', kind });
  };

  const enterEdit = (skill: EditableSkill) => {
    setLabel(skill.label);
    setDescription(skill.description ?? '');
    setContent(skill.content);
    setIcon(skill.icon ?? 'IconSparkles');
    setView({ mode: 'edit', skill });
  };

  const apiName =
    view.mode === 'create'
      ? `${view.kind.prefix}${slugifySkillKey(label) || '…'}`
      : view.mode === 'edit'
        ? view.skill.name
        : '';

  const canSave =
    label.trim().length > 0 &&
    content.trim().length > 0 &&
    (view.mode === 'create' ? slugifySkillKey(label).length > 0 : true) &&
    !isSubmitting;

  const reportError = (error: unknown) =>
    enqueueErrorSnackBar({
      apolloError: CombinedGraphQLErrors.is(error) ? error : undefined,
    });

  const handleSave = async () => {
    if (!canSave) {
      return;
    }
    setIsSubmitting(true);
    try {
      const shared = {
        label: label.trim(),
        description: description.trim() || undefined,
        content,
        icon,
      };
      if (view.mode === 'create') {
        await createSkill({
          variables: {
            input: { name: `${view.kind.prefix}${slugifySkillKey(label)}`, ...shared },
          },
        });
        enqueueSuccessSnackBar({ message: t`Skill created` });
      } else if (view.mode === 'edit') {
        await updateSkill({
          variables: { input: { id: view.skill.id, name: view.skill.name, ...shared } },
        });
        enqueueSuccessSnackBar({ message: t`Skill saved` });
      }
      await refetch();
      backToList();
    } catch (error) {
      reportError(error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleToggleActive = async () => {
    if (view.mode !== 'edit') {
      return;
    }
    setIsSubmitting(true);
    try {
      const mutate = view.skill.isActive ? deactivateSkill : activateSkill;
      await mutate({ variables: { id: view.skill.id } });
      await refetch();
      backToList();
    } catch (error) {
      reportError(error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleDelete = async () => {
    if (view.mode !== 'edit') {
      return;
    }
    setIsSubmitting(true);
    try {
      await deleteSkill({ variables: { id: view.skill.id } });
      closeModal(DELETE_SKILL_MODAL_ID);
      enqueueSuccessSnackBar({ message: t`Skill deleted` });
      await refetch();
      backToList();
    } catch (error) {
      reportError(error);
    } finally {
      setIsSubmitting(false);
    }
  };

  const isForm = view.mode !== 'list';
  const title =
    view.mode === 'create'
      ? t`New ${view.kind.label}`
      : view.mode === 'edit'
        ? view.skill.label
        : t`Skills`;

  return (
    <SubMenuTopBarContainer
      title={title}
      actionButton={
        isForm ? (
          <SaveAndCancelButtons
            onSave={handleSave}
            onCancel={backToList}
            isSaveDisabled={!canSave}
            isLoading={isSubmitting}
            isCancelDisabled={isSubmitting}
          />
        ) : undefined
      }
      links={[{ children: t`Skills` }]}
    >
      <SettingsPageContainer>
        {isForm ? (
          <FollowupSkillFormFields
            label={label}
            description={description}
            content={content}
            icon={icon}
            apiName={apiName}
            onLabelChange={setLabel}
            onDescriptionChange={setDescription}
            onContentChange={setContent}
            onIconChange={setIcon}
            skill={view.mode === 'edit' ? view.skill : undefined}
            onToggleActive={handleToggleActive}
            onDelete={() => openModal(DELETE_SKILL_MODAL_ID)}
          />
        ) : (
          <>
            <TabList
              tabs={FOLLOWUP_SKILL_TABS.map((tab) => ({
                id: tab.id,
                title: tab.title,
              }))}
              componentInstanceId={SKILLS_TAB_LIST_INSTANCE_ID}
            />
            <FollowupSkillsList
              tab={activeTab}
              skills={skills}
              onCreate={enterCreate}
              onEdit={enterEdit}
            />
          </>
        )}
      </SettingsPageContainer>

      <ConfirmationModal
        modalInstanceId={DELETE_SKILL_MODAL_ID}
        title={t`Delete skill`}
        subtitle={t`Are you sure you want to delete this skill? This action cannot be undone.`}
        onConfirmClick={handleDelete}
        confirmButtonText={t`Delete`}
        loading={isSubmitting}
      />
    </SubMenuTopBarContainer>
  );
};
