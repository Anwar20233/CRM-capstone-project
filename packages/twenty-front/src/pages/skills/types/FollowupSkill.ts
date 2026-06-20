import { type TagColor } from 'twenty-ui/components';

// A kind the user can create from a section. `key` becomes the trailing segment
// of the skill name (prefix + slug(key)); the agent matches on it (playbook key =
// pipeline stage, template key = draft type).
export type FollowupSkillKind = {
  id: string;
  prefix: string;
  label: string;
  icon: string;
  keyLabel: string;
  keyPlaceholder: string;
};

// A skill row as returned by FindManySkills (skillFragment), the shape the
// Skills page reads and edits.
export type EditableSkill = {
  id: string;
  name: string;
  label: string;
  description?: string | null;
  icon?: string | null;
  content: string;
  isActive: boolean;
  isCustom: boolean;
};

// One category within a tab (e.g. "Email templates"). A section merges the
// user's own skills and the agent's built-in knowledge files of the same kind
// into a single list, and optionally lets the user add a new skill of that kind.
export type FollowupSkillSection = {
  id: string;
  title: string;
  description: string;
  // Tabler icon name shown in the section header.
  icon: string;
  // Colour of the category tag shown on each card.
  tagColor: TagColor;
  // Skills belonging here: any name with one of these prefixes, or these exact
  // names (for one-off frameworks like BANT that aren't a prefix family).
  skillPrefixes: string[];
  skillSingletons: string[];
  // Built-in knowledge files belonging here, by their parent folder key.
  fileFolders: string[];
  // When set, the section shows a "New …" button that creates a skill.
  addable?: FollowupSkillKind;
};

export type FollowupSkillTab = {
  id: string;
  title: string;
  description: string;
  // The agent (ai-service directory) whose built-in knowledge files this tab
  // surfaces alongside the user-authored skills.
  agent: 'emailer' | 'next_step';
  sections: FollowupSkillSection[];
};
