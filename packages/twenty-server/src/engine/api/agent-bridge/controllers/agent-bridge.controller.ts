import {
  Body,
  Controller,
  HttpCode,
  HttpStatus,
  Post,
  UsePipes,
  ValidationPipe,
} from '@nestjs/common';

import { ExecuteToolDto } from 'src/engine/api/agent-bridge/dtos/execute-tool.dto';
import { GetCatalogDto } from 'src/engine/api/agent-bridge/dtos/get-catalog.dto';
import { GetCurrentUserDto } from 'src/engine/api/agent-bridge/dtos/get-current-user.dto';
import { LearnToolsDto } from 'src/engine/api/agent-bridge/dtos/learn-tools.dto';
import { AgentBridgeService } from 'src/engine/api/agent-bridge/services/agent-bridge.service';

// Intentionally unguarded: the security layer lives upstream in the Python
// service. Every endpoint takes workspaceId/roleId from the caller since there
// is no auth context to resolve them from.
@Controller('agent-bridge')
@UsePipes(
  new ValidationPipe({
    transform: true,
    whitelist: true,
    forbidNonWhitelisted: true,
  }),
)
export class AgentBridgeController {
  constructor(private readonly agentBridgeService: AgentBridgeService) {}

  @Post('execute')
  @HttpCode(HttpStatus.OK)
  async execute(@Body() body: ExecuteToolDto) {
    return this.agentBridgeService.executeTool(
      body.tool,
      body.args,
      body.workspaceId,
      body.roleId,
      body.userId,
      body.userWorkspaceId,
    );
  }

  @Post('catalog')
  @HttpCode(HttpStatus.OK)
  async catalog(@Body() body: GetCatalogDto) {
    return this.agentBridgeService.getCatalog(
      body.workspaceId,
      body.roleId,
      body.categories,
    );
  }

  @Post('learn')
  @HttpCode(HttpStatus.OK)
  async learn(@Body() body: LearnToolsDto) {
    return this.agentBridgeService.learnTools(
      body.toolNames,
      body.workspaceId,
      body.roleId,
    );
  }

  @Post('current-user')
  @HttpCode(HttpStatus.OK)
  async currentUser(@Body() body: GetCurrentUserDto) {
    return this.agentBridgeService.getCurrentUser(
      body.workspaceId,
      body.userId,
    );
  }
}
