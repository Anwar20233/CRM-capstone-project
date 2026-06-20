import { FOLLOWUP_SKILL_PREFIXES } from '~/pages/skills/constants/FollowupSkillPrefixes';
import { FOLLOWUP_SKILL_SINGLETONS } from '~/pages/skills/constants/FollowupSkillSingletons';
import { type FollowupSkillTab } from '~/pages/skills/types/FollowupSkill';

// The Skills page groups each agent's customisation into category sections.
// Every section merges the user's own skills with the agent's built-in defaults
// of the same kind, so a company tunes one coherent library per category.
export const FOLLOWUP_SKILL_TABS: FollowupSkillTab[] = [
  {
    id: 'planner',
    agent: 'next_step',
    title: 'Planner',
    description:
      'Teach the Next-Step Planner how your team decides the best follow-up. ' +
      'It loads the relevant guidance for each deal before recommending an action.',
    sections: [
      {
        id: 'playbooks',
        title: 'Stage playbooks',
        description:
          'Per-stage guidance the planner follows as a deal moves through your pipeline.',
        icon: 'IconBook',
        tagColor: 'blue',
        skillPrefixes: [FOLLOWUP_SKILL_PREFIXES.PLAYBOOK],
        skillSingletons: [],
        fileFolders: ['playbooks'],
        addable: {
          id: 'playbook',
          prefix: FOLLOWUP_SKILL_PREFIXES.PLAYBOOK,
          label: 'Stage playbook',
          icon: 'IconBook',
          keyLabel: 'Stage',
          keyPlaceholder: 'e.g. negotiation',
        },
      },
      {
        id: 'frameworks',
        title: 'Qualification frameworks',
        description:
          'Frameworks the planner reasons from, like BANT gaps and engagement cadence.',
        icon: 'IconChecklist',
        tagColor: 'purple',
        skillPrefixes: [],
        skillSingletons: [
          FOLLOWUP_SKILL_SINGLETONS.BANT,
          FOLLOWUP_SKILL_SINGLETONS.BEST_PRACTICES,
        ],
        fileFolders: ['knowledge'],
      },
      {
        id: 'custom-tips',
        title: 'Custom guidance',
        description:
          'Your own tips for any situation — pricing objections, going dark, multi-threading.',
        icon: 'IconBulb',
        tagColor: 'green',
        skillPrefixes: [FOLLOWUP_SKILL_PREFIXES.PLANNER],
        skillSingletons: [],
        fileFolders: [],
        addable: {
          id: 'planner',
          prefix: FOLLOWUP_SKILL_PREFIXES.PLANNER,
          label: 'Guidance',
          icon: 'IconBulb',
          keyLabel: 'Topic',
          keyPlaceholder: 'e.g. handling-pricing-objections',
        },
      },
    ],
  },
  {
    id: 'email-drafting',
    agent: 'emailer',
    title: 'Email Drafting',
    description:
      'Shape how the Email Drafter writes. It retrieves these templates and ' +
      'catalogs to match your house style and ground proposals in real offerings.',
    sections: [
      {
        id: 'email-templates',
        title: 'Email templates',
        description:
          'House-style templates for follow-ups, recaps, and re-engagement emails.',
        icon: 'IconMail',
        tagColor: 'blue',
        skillPrefixes: [FOLLOWUP_SKILL_PREFIXES.EMAIL_TEMPLATE],
        skillSingletons: [],
        fileFolders: ['email_templates'],
        addable: {
          id: 'email',
          prefix: FOLLOWUP_SKILL_PREFIXES.EMAIL_TEMPLATE,
          label: 'Email template',
          icon: 'IconMail',
          keyLabel: 'Draft type',
          keyPlaceholder: 'e.g. follow_up',
        },
      },
      {
        id: 'proposals',
        title: 'Proposals',
        description:
          'Proposal structures the drafter follows when sending an offer.',
        icon: 'IconFileText',
        tagColor: 'purple',
        skillPrefixes: [FOLLOWUP_SKILL_PREFIXES.PROPOSAL_TEMPLATE],
        skillSingletons: [],
        fileFolders: ['proposal_templates'],
        addable: {
          id: 'proposal',
          prefix: FOLLOWUP_SKILL_PREFIXES.PROPOSAL_TEMPLATE,
          label: 'Proposal template',
          icon: 'IconFileText',
          keyLabel: 'Proposal type',
          keyPlaceholder: 'e.g. product_proposal',
        },
      },
      {
        id: 'product-catalog',
        title: 'Product catalog',
        description: 'Products the drafter cites and grounds proposals in.',
        icon: 'IconBox',
        tagColor: 'green',
        skillPrefixes: [FOLLOWUP_SKILL_PREFIXES.PRODUCT_CATALOG],
        skillSingletons: [],
        fileFolders: ['product_catalog'],
        addable: {
          id: 'product',
          prefix: FOLLOWUP_SKILL_PREFIXES.PRODUCT_CATALOG,
          label: 'Product',
          icon: 'IconBox',
          keyLabel: 'Product',
          keyPlaceholder: 'e.g. saas_platform',
        },
      },
      {
        id: 'service-catalog',
        title: 'Service catalog',
        description: 'Services the drafter cites and grounds proposals in.',
        icon: 'IconTool',
        tagColor: 'turquoise',
        skillPrefixes: [FOLLOWUP_SKILL_PREFIXES.SERVICE_CATALOG],
        skillSingletons: [],
        fileFolders: ['service_catalog'],
        addable: {
          id: 'service',
          prefix: FOLLOWUP_SKILL_PREFIXES.SERVICE_CATALOG,
          label: 'Service',
          icon: 'IconTool',
          keyLabel: 'Service',
          keyPlaceholder: 'e.g. implementation_services',
        },
      },
    ],
  },
];
