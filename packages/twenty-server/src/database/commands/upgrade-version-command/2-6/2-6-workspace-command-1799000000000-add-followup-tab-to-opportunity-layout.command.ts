import { Command } from 'nest-commander';
import { isDefined } from 'twenty-shared/utils';

import { ActiveOrSuspendedWorkspaceCommandRunner } from 'src/database/commands/command-runners/active-or-suspended-workspace.command-runner';
import { WorkspaceIteratorService } from 'src/database/commands/command-runners/workspace-iterator.service';
import { type RunOnWorkspaceArgs } from 'src/database/commands/command-runners/workspace.command-runner';
import { ApplicationService } from 'src/engine/core-modules/application/application.service';
import { RegisteredWorkspaceCommand } from 'src/engine/core-modules/upgrade/decorators/registered-workspace-command.decorator';
import { computeTwentyStandardApplicationAllFlatEntityMaps } from 'src/engine/workspace-manager/twenty-standard-application/utils/twenty-standard-application-all-flat-entity-maps.constant';
import { WorkspaceCacheService } from 'src/engine/workspace-cache/services/workspace-cache.service';
import { WorkspaceMigrationValidateBuildAndRunService } from 'src/engine/workspace-manager/workspace-migration/services/workspace-migration-validate-build-and-run-service';

// Universal identifiers defined in the standard opportunity page layout config.
const OPPORTUNITY_LAYOUT_UNIVERSAL_IDENTIFIER =
  '20202020-a103-4003-8003-0aa0b1ca1003';
const FOLLOWUP_TAB_UNIVERSAL_IDENTIFIER =
  '20202020-ab03-4003-8003-0aa0b1ca1300';
const FOLLOWUP_WIDGET_UNIVERSAL_IDENTIFIER =
  '20202020-ac03-4003-8003-0aa0b1ca1310';

// Adds the Follow-Up Intelligence tab (and its VIEW widget) to the opportunity
// record page layout of existing workspaces, and makes it the default tab in the
// side panel. New workspaces already get it from the standard config seed, so this
// only backfills workspaces that were seeded before the tab existed. Idempotent.
@RegisteredWorkspaceCommand('2.6.0', 1799000000000)
@Command({
  name: 'upgrade:2-6:add-followup-tab-to-opportunity-layout',
  description:
    'Add the Follow-Up Intelligence tab to the opportunity record page layout',
})
export class AddFollowupTabToOpportunityLayoutCommand extends ActiveOrSuspendedWorkspaceCommandRunner {
  constructor(
    protected readonly workspaceIteratorService: WorkspaceIteratorService,
    private readonly applicationService: ApplicationService,
    private readonly workspaceCacheService: WorkspaceCacheService,
    private readonly workspaceMigrationValidateBuildAndRunService: WorkspaceMigrationValidateBuildAndRunService,
  ) {
    super(workspaceIteratorService);
  }

  override async runOnWorkspace({
    workspaceId,
    options,
  }: RunOnWorkspaceArgs): Promise<void> {
    const isDryRun = options.dryRun ?? false;

    const { flatPageLayoutMaps, flatPageLayoutTabMaps } =
      await this.workspaceCacheService.getOrRecompute(workspaceId, [
        'flatPageLayoutMaps',
        'flatPageLayoutTabMaps',
      ]);

    const existingLayout =
      flatPageLayoutMaps.byUniversalIdentifier[
        OPPORTUNITY_LAYOUT_UNIVERSAL_IDENTIFIER
      ];

    if (!isDefined(existingLayout)) {
      this.logger.log(
        `No standard opportunity record page layout in workspace ${workspaceId}, skipping`,
      );

      return;
    }

    const alreadyHasFollowupTab =
      flatPageLayoutTabMaps.byUniversalIdentifier[
        FOLLOWUP_TAB_UNIVERSAL_IDENTIFIER
      ];

    if (isDefined(alreadyHasFollowupTab)) {
      this.logger.log(
        `Follow-Up tab already present in workspace ${workspaceId}, skipping`,
      );

      return;
    }

    const { twentyStandardFlatApplication } =
      await this.applicationService.findWorkspaceTwentyStandardAndCustomApplicationOrThrow(
        { workspaceId },
      );

    const { allFlatEntityMaps: standardMaps } =
      computeTwentyStandardApplicationAllFlatEntityMaps({
        now: new Date().toISOString(),
        workspaceId,
        twentyStandardApplicationId: twentyStandardFlatApplication.id,
      });

    const standardTab =
      standardMaps.flatPageLayoutTabMaps.byUniversalIdentifier[
        FOLLOWUP_TAB_UNIVERSAL_IDENTIFIER
      ];

    const standardWidget =
      standardMaps.flatPageLayoutWidgetMaps.byUniversalIdentifier[
        FOLLOWUP_WIDGET_UNIVERSAL_IDENTIFIER
      ];

    if (!isDefined(standardTab) || !isDefined(standardWidget)) {
      throw new Error(
        'Follow-Up tab/widget missing from standard application flat maps',
      );
    }

    // Re-point the standard tab at the workspace's existing opportunity layout.
    // The widget already references the standard tab id, which we keep as-is.
    const tabToCreate = {
      ...standardTab,
      pageLayoutId: existingLayout.id,
    };

    const layoutToUpdate = {
      ...existingLayout,
      defaultTabToFocusOnMobileAndSidePanelId: standardTab.id,
    };

    if (isDryRun) {
      this.logger.log(
        `[DRY RUN] Would add Follow-Up tab + widget to opportunity layout ${existingLayout.id} in workspace ${workspaceId}`,
      );

      return;
    }

    const result =
      await this.workspaceMigrationValidateBuildAndRunService.validateBuildAndRunWorkspaceMigration(
        {
          allFlatEntityOperationByMetadataName: {
            pageLayoutTab: {
              flatEntityToCreate: [tabToCreate],
              flatEntityToDelete: [],
              flatEntityToUpdate: [],
            },
            pageLayoutWidget: {
              flatEntityToCreate: [standardWidget],
              flatEntityToDelete: [],
              flatEntityToUpdate: [],
            },
            pageLayout: {
              flatEntityToCreate: [],
              flatEntityToDelete: [],
              flatEntityToUpdate: [layoutToUpdate],
            },
          },
          workspaceId,
          applicationUniversalIdentifier:
            twentyStandardFlatApplication.universalIdentifier,
        },
      );

    if (result.status === 'fail') {
      this.logger.error(
        `Failed to add Follow-Up tab in workspace ${workspaceId}:\n${JSON.stringify(result, null, 2)}`,
      );
      throw new Error(
        `Failed to add Follow-Up tab for workspace ${workspaceId}`,
      );
    }

    this.logger.log(
      `Added Follow-Up tab to opportunity layout for workspace ${workspaceId}`,
    );
  }
}
