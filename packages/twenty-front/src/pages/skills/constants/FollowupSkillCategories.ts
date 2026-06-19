// The follow-up agents read their knowledge from skills whose names follow a
// stable convention (mirrors packages/twenty-ai-service/followup/knowledge/
// skill_store.py). The Skills page only surfaces these, grouped into two tabs
// that map to the two agents.

export const FOLLOWUP_SKILL_PREFIXES = {
  // General planning skills the user authors (tips for planning any situation).
  PLANNER: 'followup-planner-',
  // Seeded defaults the planner also discovers.
  PLAYBOOK: 'followup-playbook-',
  EMAIL_TEMPLATE: 'followup-email-template-',
  PROPOSAL_TEMPLATE: 'followup-proposal-template-',
} as const;

export const FOLLOWUP_SKILL_SINGLETONS = {
  BANT: 'followup-bant',
  BEST_PRACTICES: 'followup-best-practices',
} as const;

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

export const FOLLOWUP_SKILL_TABS: FollowupSkillTab[] = [
  {
    id: 'planner',
    title: 'Planner',
    description:
      'The Next-Step Planner discovers these at run time and loads the ones ' +
      'relevant to a deal before recommending the next action. Write general ' +
      'tips for handling any situation (e.g. pricing objections, going dark, ' +
      'multi-threading) — not just per-stage playbooks. Edit or add skills to ' +
      'change how your company plans follow-ups.',
    prefixes: [
      FOLLOWUP_SKILL_PREFIXES.PLANNER,
      FOLLOWUP_SKILL_PREFIXES.PLAYBOOK,
    ],
    singletons: [
      FOLLOWUP_SKILL_SINGLETONS.BANT,
      FOLLOWUP_SKILL_SINGLETONS.BEST_PRACTICES,
    ],
    addableKinds: [
      {
        id: 'planner',
        prefix: FOLLOWUP_SKILL_PREFIXES.PLANNER,
        label: 'Planning skill',
        icon: 'IconBulb',
        keyLabel: 'Topic',
        keyPlaceholder: 'e.g. handling-pricing-objections',
      },
    ],
  },
  {
    id: 'email-drafting',
    title: 'Email Drafting',
    description:
      'The Email Drafter writes follow-up emails and proposals in your house ' +
      'style by retrieving these templates. Add or edit templates so drafts ' +
      'match the tone, structure, and language your company actually uses.',
    prefixes: [
      FOLLOWUP_SKILL_PREFIXES.EMAIL_TEMPLATE,
      FOLLOWUP_SKILL_PREFIXES.PROPOSAL_TEMPLATE,
    ],
    singletons: [],
    addableKinds: [
      {
        id: 'email',
        prefix: FOLLOWUP_SKILL_PREFIXES.EMAIL_TEMPLATE,
        label: 'Email template',
        icon: 'IconMail',
        keyLabel: 'Draft type',
        keyPlaceholder: 'e.g. follow_up',
      },
      {
        id: 'proposal',
        prefix: FOLLOWUP_SKILL_PREFIXES.PROPOSAL_TEMPLATE,
        label: 'Proposal template',
        icon: 'IconFileText',
        keyLabel: 'Proposal type',
        keyPlaceholder: 'e.g. product_proposal',
      },
    ],
  },
];

export const skillBelongsToTab = (
  skillName: string,
  tab: FollowupSkillTab,
): boolean =>
  tab.prefixes.some((prefix) => skillName.startsWith(prefix)) ||
  tab.singletons.includes(skillName);

// Slugify a user-provided key into the trailing segment of a skill name.
export const slugifySkillKey = (key: string): string =>
  key
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^a-z0-9_]/g, '');
