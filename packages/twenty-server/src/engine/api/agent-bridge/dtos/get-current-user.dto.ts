import { IsNotEmpty, IsString } from 'class-validator';

export class GetCurrentUserDto {
  @IsString()
  @IsNotEmpty()
  workspaceId: string;

  @IsString()
  @IsNotEmpty()
  userId: string;
}
