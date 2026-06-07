import { IsArray, IsNotEmpty, IsOptional, IsString } from 'class-validator';

export class GetCatalogDto {
  @IsString()
  @IsNotEmpty()
  workspaceId: string;

  @IsString()
  @IsNotEmpty()
  roleId: string;

  @IsOptional()
  @IsArray()
  @IsString({ each: true })
  categories?: string[];
}
