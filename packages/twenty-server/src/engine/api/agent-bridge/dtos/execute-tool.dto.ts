import {
  IsNotEmpty,
  IsObject,
  IsOptional,
  IsString,
} from 'class-validator';

export class ExecuteToolDto {
  @IsString()
  @IsNotEmpty()
  tool: string;

  @IsOptional()
  @IsObject()
  args?: Record<string, unknown>;

  @IsString()
  @IsNotEmpty()
  workspaceId: string;

  @IsString()
  @IsNotEmpty()
  roleId: string;

  // Required for DATABASE_CRUD tools (actor attribution / row-level perms).
  // userWorkspaceId is auto-resolved from userId + workspaceId when omitted.
  @IsOptional()
  @IsString()
  @IsNotEmpty()
  userId?: string;

  @IsOptional()
  @IsString()
  @IsNotEmpty()
  userWorkspaceId?: string;
}
