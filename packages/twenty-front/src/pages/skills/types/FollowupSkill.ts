// A kind the user can create from a tab. `key` becomes the trailing segment of
// the skill name (prefix + slug(key)); the agent matches on it (playbook key =
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

export type FollowupSkillTab = {
  id: string;
  title: string;
  description: string;
  // Names handled by this tab: any of these prefixes, or these exact names.
  prefixes: string[];
  singletons: string[];
  addableKinds: FollowupSkillKind[];
};
