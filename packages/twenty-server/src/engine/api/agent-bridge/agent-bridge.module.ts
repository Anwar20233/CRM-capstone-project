import { Module } from '@nestjs/common';

import { AgentBridgeController } from 'src/engine/api/agent-bridge/controllers/agent-bridge.controller';
import { AgentBridgeService } from 'src/engine/api/agent-bridge/services/agent-bridge.service';
import { UserWorkspaceModule } from 'src/engine/core-modules/user-workspace/user-workspace.module';
import { ToolProviderModule } from 'src/engine/core-modules/tool-provider/tool-provider.module';
import { UserRoleModule } from 'src/engine/metadata-modules/user-role/user-role.module';

// GlobalWorkspaceOrmManager is provided by the @Global() GlobalWorkspaceDataSourceModule,
// so it does not need to be imported here.
@Module({
  imports: [ToolProviderModule, UserRoleModule, UserWorkspaceModule],
  controllers: [AgentBridgeController],
  providers: [AgentBridgeService],
  exports: [AgentBridgeService],
})
export class AgentBridgeModule {}
