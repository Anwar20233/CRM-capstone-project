import {
  Column,
  CreateDateColumn,
  Entity,
  JoinColumn,
  ManyToOne,
  PrimaryGeneratedColumn,
  Relation,
  Unique,
} from 'typeorm';

import { WorkspaceEntity } from 'src/engine/core-modules/workspace/workspace.entity';

// Permanent masked alias for a single CRM record. The token (e.g. CONTACT_001)
// is stable across requests/sessions: the same person always maps to the same
// token within a workspace, so masked text stays coherent over time and a later
// un-masking phase can resolve a token back to its record by lookup.
@Entity({ name: 'entityMaskAlias', schema: 'core' })
@Unique('IDX_MASK_ALIAS_OBJECT_RECORD_UNIQUE', [
  'workspaceId',
  'objectName',
  'recordId',
])
@Unique('IDX_MASK_ALIAS_OBJECT_TOKEN_UNIQUE', [
  'workspaceId',
  'objectName',
  'token',
])
export class EntityMaskAliasEntity {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @ManyToOne(() => WorkspaceEntity, {
    onDelete: 'CASCADE',
  })
  @JoinColumn({ name: 'workspaceId' })
  workspace: Relation<WorkspaceEntity>;

  @Column({ nullable: false, type: 'uuid' })
  workspaceId: string;

  // Object metadata name singular: 'person' | 'company' | 'opportunity'
  @Column({ nullable: false, type: 'text' })
  objectName: string;

  @Column({ nullable: false, type: 'uuid' })
  recordId: string;

  // e.g. 'CONTACT_001', 'ORG_001', 'DEAL_001'
  @Column({ nullable: false, type: 'text' })
  token: string;

  // Per (workspace, objectName) sequence backing the token number.
  @Column({ nullable: false, type: 'int' })
  sequenceNumber: number;

  @CreateDateColumn({ type: 'timestamptz' })
  createdAt: Date;
}
