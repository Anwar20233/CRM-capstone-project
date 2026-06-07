import { Injectable, Logger } from '@nestjs/common';

import { isDefined } from 'twenty-shared/utils';

import { UserWorkspaceService } from 'src/engine/core-modules/user-workspace/user-workspace.service';
import { type ToolContext } from 'src/engine/core-modules/tool-provider/types/tool-context.type';
import { type ToolIndexEntry } from 'src/engine/core-modules/tool-provider/types/tool-index-entry.type';
import { ToolRegistryService } from 'src/engine/core-modules/tool-provider/services/tool-registry.service';
import { GlobalWorkspaceOrmManager } from 'src/engine/twenty-orm/global-workspace-datasource/global-workspace-orm.manager';
import { buildSystemAuthContext } from 'src/engine/twenty-orm/utils/build-system-auth-context.util';
import { WorkspaceMemberWorkspaceEntity } from 'src/modules/workspace-member/standard-objects/workspace-member.workspace-entity';

// Envelope returned to the Python router. The bridge never throws across the
// HTTP boundary: failures are reported as { ok: false, error } so the upstream
// security/orchestration layer can decide how to react.
export type BridgeSuccess<TData> = { ok: true; data: TData };
export type BridgeFailure = {
  ok: false;
  error: { code: string; message: string };
};
export type BridgeResult<TData> = BridgeSuccess<TData> | BridgeFailure;

@Injectable()
export class AgentBridgeService {
  private readonly logger = new Logger(AgentBridgeService.name);

  constructor(
    private readonly toolRegistryService: ToolRegistryService,
    private readonly globalWorkspaceOrmManager: GlobalWorkspaceOrmManager,
    private readonly userWorkspaceService: UserWorkspaceService,
  ) {}

  async executeTool(
    tool: string,
    args: Record<string, unknown> | undefined,
    workspaceId: string,
    roleId: string,
    userId?: string,
    userWorkspaceId?: string,
  ): Promise<BridgeResult<unknown>> {
    try {
      // DATABASE_CRUD tools need a user context; resolve userWorkspaceId from
      // userId + workspaceId when the caller only supplies userId.
      const resolvedUserWorkspaceId =
        userWorkspaceId ??
        (isDefined(userId)
          ? (
              await this.userWorkspaceService.getUserWorkspaceForUserOrThrow({
                userId,
                workspaceId,
                relations: [],
              })
            ).id
          : undefined);

      const context: ToolContext = {
        workspaceId,
        roleId,
        userId,
        userWorkspaceId: resolvedUserWorkspaceId,
      };

      const result = await this.toolRegistryService.resolveAndExecute(
        tool,
        args,
        context,
      );

      return { ok: true, data: result };
    } catch (error) {
      return this.toFailure(error, `Failed to execute tool "${tool}"`);
    }
  }

  async getCatalog(
    workspaceId: string,
    roleId: string,
    categories?: string[],
  ): Promise<BridgeResult<{ catalog: Record<string, ToolIndexEntry[]> }>> {
    try {
      const index = await this.toolRegistryService.buildToolIndex(
        workspaceId,
        roleId,
      );

      const categorySet = categories ? new Set(categories) : undefined;

      const catalog: Record<string, ToolIndexEntry[]> = {};

      for (const entry of index) {
        if (categorySet && !categorySet.has(entry.category)) {
          continue;
        }

        const existing = catalog[entry.category] ?? [];

        existing.push(entry);
        catalog[entry.category] = existing;
      }

      return { ok: true, data: { catalog } };
    } catch (error) {
      return this.toFailure(error, 'Failed to build tool catalog');
    }
  }

  async learnTools(
    toolNames: string[],
    workspaceId: string,
    roleId: string,
  ): Promise<
    BridgeResult<{
      tools: Array<{
        name: string;
        description?: string;
        inputSchema?: object;
      }>;
    }>
  > {
    try {
      const context: ToolContext = { workspaceId, roleId };

      const tools = await this.toolRegistryService.getToolInfo(
        toolNames,
        context,
      );

      return { ok: true, data: { tools } };
    } catch (error) {
      return this.toFailure(error, 'Failed to learn tools');
    }
  }

  // currentWorkspaceMember is a GraphQL query, not a CRUD tool. We resolve it
  // directly through the global workspace ORM (system auth context) so the
  // agent can attribute records to "me" without an authenticated session.
  async getCurrentUser(
    workspaceId: string,
    userId: string,
  ): Promise<BridgeResult<WorkspaceMemberWorkspaceEntity>> {
    try {
      const authContext = buildSystemAuthContext(workspaceId);

      const workspaceMember =
        await this.globalWorkspaceOrmManager.executeInWorkspaceContext(
          async () => {
            const workspaceMemberRepository =
              await this.globalWorkspaceOrmManager.getRepository<WorkspaceMemberWorkspaceEntity>(
                workspaceId,
                'workspaceMember',
                { shouldBypassPermissionChecks: true },
              );

            return workspaceMemberRepository.findOne({ where: { userId } });
          },
          authContext,
        );

      if (!isDefined(workspaceMember)) {
        return {
          ok: false,
          error: {
            code: 'NOT_FOUND',
            message: `No workspace member found for user "${userId}"`,
          },
        };
      }

      return { ok: true, data: workspaceMember };
    } catch (error) {
      return this.toFailure(error, 'Failed to resolve current user');
    }
  }

  private toFailure(error: unknown, fallbackMessage: string): BridgeFailure {
    const message = error instanceof Error ? error.message : String(error);

    this.logger.error(`${fallbackMessage}: ${message}`);

    return {
      ok: false,
      error: { code: 'INTERNAL_ERROR', message: message || fallbackMessage },
    };
  }
}
