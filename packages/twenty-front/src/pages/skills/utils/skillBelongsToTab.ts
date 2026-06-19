import { type FollowupSkillTab } from '~/pages/skills/types/FollowupSkill';

export const skillBelongsToTab = (
  skillName: string,
  tab: FollowupSkillTab,
): boolean =>
  tab.prefixes.some((prefix) => skillName.startsWith(prefix)) ||
  tab.singletons.includes(skillName);
