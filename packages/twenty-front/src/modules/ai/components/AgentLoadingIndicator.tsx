import { styled } from '@linaria/react';

// The animated water-drop SVG ships as a static asset and is rendered through an
// <img> on purpose: the file carries its own <style>/@keyframes, and an image
// element isolates those animations from the page (no global class leakage).
const StyledLoadingImage = styled.img`
  height: 56px;
  width: auto;
`;

export const AgentLoadingIndicator = ({
  className,
}: {
  className?: string;
}) => {
  return (
    <StyledLoadingImage
      className={className}
      src="/images/ai/agent-loading.svg"
      alt=""
      aria-hidden
    />
  );
};
