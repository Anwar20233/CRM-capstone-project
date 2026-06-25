// Slugify a user-provided key into the trailing segment of a skill name.
export const slugifySkillKey = (key: string): string =>
  key
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^a-z0-9_]/g, '');
