import { FollowupIntelligencePanel } from '@/followup-intelligence/components/FollowupIntelligencePanel';
import { type PageLayoutWidget } from '@/page-layout/types/PageLayoutWidget';
import { useLayoutRenderingContext } from '@/ui/layout/contexts/LayoutRenderingContext';
import { SidePanelProvider } from '@/ui/layout/side-panel/contexts/SidePanelContext';
import { styled } from '@linaria/react';

const StyledContainer = styled.div`
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  width: 100%;
`;

type FollowupIntelligenceWidgetProps = {
  widget: PageLayoutWidget;
};

export const FollowupIntelligenceWidget = ({
  widget: _widget,
}: FollowupIntelligenceWidgetProps) => {
  const { isInSidePanel } = useLayoutRenderingContext();

  return (
    <SidePanelProvider value={{ isInSidePanel }}>
      <StyledContainer>
        <FollowupIntelligencePanel />
      </StyledContainer>
    </SidePanelProvider>
  );
};
