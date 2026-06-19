export { DraftPreview } from '@/followup-intelligence/components/DraftPreview';
export { FollowupIntelligencePanel } from '@/followup-intelligence/components/FollowupIntelligencePanel';
export { OpportunityHealthPanel } from '@/followup-intelligence/components/OpportunityHealthPanel';
export { useFollowupActions } from '@/followup-intelligence/hooks/useFollowupActions';
export { useFollowupProfile } from '@/followup-intelligence/hooks/useFollowupProfile';
export {
  acceptFollowupAction,
  fetchFollowupActions,
  fetchFollowupProfile,
  rejectFollowupAction,
  reviseFollowupAction,
} from '@/followup-intelligence/services/followup-api';
export type {
  FollowupAcceptResult,
  FollowupAction,
  FollowupProfile,
  FollowupReviseResult,
} from '@/followup-intelligence/types/followup-action';
