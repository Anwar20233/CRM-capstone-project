import { ArrayNotEmpty, IsArray, IsNotEmpty, IsString } from 'class-validator';

export class LearnToolsDto {
  @IsArray()
  @ArrayNotEmpty()
  @IsString({ each: true })
  toolNames: string[];

  @IsString()
  @IsNotEmpty()
  workspaceId: string;

  @IsString()
  @IsNotEmpty()
  roleId: string;
}
