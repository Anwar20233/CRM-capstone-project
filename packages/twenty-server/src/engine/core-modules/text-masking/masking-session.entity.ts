import {
  Column,
  CreateDateColumn,
  Entity,
  JoinColumn,
  ManyToOne,
  PrimaryGeneratedColumn,
  Relation,
} from 'typeorm';

import { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';
import { type ReverseMap } from 'src/engine/core-modules/text-masking/types/reverse-map-entry.type';

// One masking session's persisted reverse map, kept so the later un-masking
// phase can restore masked tokens back to their original CRM records.
@Entity({ name: 'maskingSession', schema: 'core' })
export class MaskingSessionEntity {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @ManyToOne(() => WorkspaceEntity, {
    onDelete: 'CASCADE',
  })
  @JoinColumn({ name: 'workspaceId' })
  workspace: Relation<WorkspaceEntity>;

  @Column({ nullable: false, type: 'uuid' })
  workspaceId: string;

  @Column({ nullable: true, type: 'uuid' })
  userWorkspaceId: string | null;

  @Column({ type: 'jsonb', nullable: false, default: {} })
  reverseMap: ReverseMap;

  @CreateDateColumn({ type: 'timestamptz' })
  createdAt: Date;

  @Column({ nullable: true, type: 'timestamptz' })
  expiresAt: Date | null;
}
