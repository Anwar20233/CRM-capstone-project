import { styled } from '@linaria/react';
import { Tag, type TagColor } from 'twenty-ui/components';
import { themeCssVariables } from 'twenty-ui/theme-constants';

// One card in a skill section — used identically for a user-authored skill and
// for a built-in knowledge file, so both read as one library.
type SkillCardProps = {
  title: string;
  subtitle: string;
  tagText: string;
  tagColor: TagColor;
  muted?: boolean;
  onClick: () => void;
};

const StyledCard = styled.button<{ muted?: boolean }>`
  background: ${themeCssVariables.background.secondary};
  border: 1px solid ${themeCssVariables.border.color.medium};
  border-radius: ${themeCssVariables.border.radius.md};
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: ${themeCssVariables.spacing[2]};
  min-width: 0;
  opacity: ${({ muted }) => (muted ? 0.55 : 1)};
  padding: ${themeCssVariables.spacing[3]} ${themeCssVariables.spacing[4]};
  text-align: left;
  transition:
    border-color 0.1s ease,
    background 0.1s ease;

  &:hover {
    background: ${themeCssVariables.background.tertiary};
    border-color: ${themeCssVariables.border.color.strong};
  }
`;

const StyledHeader = styled.div`
  align-items: center;
  display: flex;
  gap: ${themeCssVariables.spacing[2]};
  justify-content: space-between;
`;

const StyledTitle = styled.span`
  color: ${themeCssVariables.font.color.primary};
  font-weight: ${themeCssVariables.font.weight.semiBold};
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const StyledSubtitle = styled.span`
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  color: ${themeCssVariables.font.color.tertiary};
  display: -webkit-box;
  font-size: ${themeCssVariables.font.size.sm};
  line-height: 1.4;
  min-height: ${themeCssVariables.spacing[8]};
  overflow: hidden;
`;

export const SkillCard = ({
  title,
  subtitle,
  tagText,
  tagColor,
  muted,
  onClick,
}: SkillCardProps) => (
  <StyledCard type="button" muted={muted} onClick={onClick}>
    <StyledHeader>
      <StyledTitle>{title}</StyledTitle>
      <Tag color={tagColor} text={tagText} weight="medium" preventShrink />
    </StyledHeader>
    <StyledSubtitle>{subtitle}</StyledSubtitle>
  </StyledCard>
);
