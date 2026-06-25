import { Module } from '@nestjs/common';

import { WorkspaceIteratorModule } from 'src/database/commands/command-runners/workspace-iterator.module';
import { AddFollowupTabToOpportunityLayoutCommand } from 'src/database/commands/upgrade-version-command/2-6/2-6-workspace-command-1799000000000-add-followup-tab-to-opportunity-layout.command';
import { ApplicationModule } from 'src/engine/core-modules/application/application.module';
import { WorkspaceCacheModule } from 'src/engine/workspace-cache/workspace-cache.module';
import { WorkspaceMigrationModule } from 'src/engine/workspace-manager/workspace-migration/workspace-migration.module';

@Module({
  imports: [
    ApplicationModule,
    WorkspaceCacheModule,
    WorkspaceIteratorModule,
    WorkspaceMigrationModule,
  ],
  providers: [AddFollowupTabToOpportunityLayoutCommand],
})
export class V2_6_UpgradeVersionCommandModule {}
