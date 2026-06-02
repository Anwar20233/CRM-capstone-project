import { Body, Controller, Post, UseFilters, UseGuards } from '@nestjs/common';

import { PermissionFlagType } from 'twenty-shared/constants';

import { RestApiExceptionFilter } from 'src/engine/api/rest/rest-api-exception.filter';
import type { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';
import { AuthUserWorkspaceId } from 'src/engine/decorators/auth/auth-user-workspace-id.decorator';
import { AuthWorkspace } from 'src/engine/decorators/auth/auth-workspace.decorator';
import { JwtAuthGuard } from 'src/engine/guards/jwt-auth.guard';
import { SettingsPermissionGuard } from 'src/engine/guards/settings-permission.guard';
import { WorkspaceAuthGuard } from 'src/engine/guards/workspace-auth.guard';
import { AiRestApiExceptionFilter } from 'src/engine/metadata-modules/ai/filters/ai-api-exception.filter';
import { MaskTextInput } from 'src/engine/metadata-modules/ai/text-masking/dtos/mask-text.input';
import { type MaskTextOutput } from 'src/engine/metadata-modules/ai/text-masking/dtos/mask-text.output';
import { TextMaskingService } from 'src/engine/metadata-modules/ai/text-masking/services/text-masking.service';

@Controller('rest/text-masking')
@UseGuards(JwtAuthGuard, WorkspaceAuthGuard)
@UseFilters(AiRestApiExceptionFilter, RestApiExceptionFilter)
export class TextMaskingController {
  constructor(private readonly textMaskingService: TextMaskingService) {}

  @Post('mask')
  @UseGuards(SettingsPermissionGuard(PermissionFlagType.AI))
  async handleMask(
    @Body() body: MaskTextInput,
    @AuthWorkspace() workspace: WorkspaceEntity,
    @AuthUserWorkspaceId() userWorkspaceId: string,
  ): Promise<MaskTextOutput> {
    return this.textMaskingService.maskText({
      workspaceId: workspace.id,
      userWorkspaceId: userWorkspaceId ?? null,
      text: body.text,
      sessionId: body.sessionId,
    });
  }
}
