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

// One masking request's persisted reverse map. The priceFactor is the constant
// k in [0.7, 1.3] applied to every money amount in the session, so the LLM still
// grasps relative magnitude without seeing real figures. Persisted so the later
// un-masking phase can restore both tokens and exact amounts.
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

  @Column({ nullable: false, type: 'float', default: 1.0 })
  priceFactor: number;

  @Column({ type: 'jsonb', nullable: false, default: {} })
  reverseMap: ReverseMap;

  @CreateDateColumn({ type: 'timestamptz' })
  createdAt: Date;

  @Column({ nullable: true, type: 'timestamptz' })
  expiresAt: Date | null;
}
