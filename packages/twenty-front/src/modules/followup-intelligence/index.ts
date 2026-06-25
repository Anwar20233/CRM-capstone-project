export { DraftPreview } from '@/followup-intelligence/components/DraftPreview';
export { FollowupIntelligencePanel } from '@/followup-intelligence/components/FollowupIntelligencePanel';
export { OpportunityHealthPanel } from '@/followup-intelligence/components/OpportunityHealthPanel';
export { useFollowupActions } from '@/followup-intelligence/hooks/useFollowupActions';
export { useFollowupProfile } from '@/followup-intelligence/hooks/useFollowupProfile';
export { useFollowupRisk } from '@/followup-intelligence/hooks/useFollowupRisk';
export {
  acceptFollowupAction,
  fetchFollowupActions,
  fetchFollowupProfile,
  fetchFollowupRisk,
  rejectFollowupAction,
  reviseFollowupAction,
} from '@/followup-intelligence/services/followup-api';
export type {
  FollowupAcceptResult,
  FollowupAction,
  FollowupProfile,
  FollowupReviseResult,
  FollowupRisk,
  FollowupSourceEmail,
  FollowupWorkflowStep,
} from '@/followup-intelligence/types/followup-action';
