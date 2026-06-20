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
  // Real product/service offerings the drafter grounds proposals in.
  PRODUCT_CATALOG: 'followup-product-catalog-',
  SERVICE_CATALOG: 'followup-service-catalog-',
} as const;
