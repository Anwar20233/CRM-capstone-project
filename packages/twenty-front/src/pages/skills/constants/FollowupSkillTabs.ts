import { FOLLOWUP_SKILL_PREFIXES } from '~/pages/skills/constants/FollowupSkillPrefixes';
import { FOLLOWUP_SKILL_SINGLETONS } from '~/pages/skills/constants/FollowupSkillSingletons';
import { type FollowupSkillTab } from '~/pages/skills/types/FollowupSkill';

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
