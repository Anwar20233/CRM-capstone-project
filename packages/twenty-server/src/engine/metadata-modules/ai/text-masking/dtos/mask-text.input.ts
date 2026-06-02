import { IsNotEmpty, IsOptional, IsString } from 'class-validator';

export class MaskTextInput {
  @IsString()
  @IsNotEmpty()
  text: string;

  // Reuse an existing masking session so the per-session price factor (and its
  // reverse map) stay consistent across calls. Omit to start a new session.
  @IsString()
  @IsOptional()
  sessionId?: string;
}
