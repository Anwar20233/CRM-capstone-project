import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';

import { Repository } from 'typeorm';

import { EntityMaskAliasEntity } from 'src/engine/core-modules/text-masking/entity-mask-alias.entity';

// objectName (object metadata name singular) → masked-token prefix.
const TOKEN_PREFIX_BY_OBJECT_NAME: Record<string, string> = {
  person: 'CONTACT',
  company: 'ORG',
  opportunity: 'DEAL',
};

@Injectable()
export class EntityMaskAliasService {
  constructor(
    @InjectRepository(EntityMaskAliasEntity)
    private readonly entityMaskAliasRepository: Repository<EntityMaskAliasEntity>,
  ) {}

  // Returns the permanent token for a record, assigning the next sequence number
  // for (workspace, objectName) on first encounter. The unique constraint guards
  // against concurrent inserts; on conflict we re-read the now-existing row.
  async getOrCreateToken(
    workspaceId: string,
    objectName: string,
    recordId: string,
  ): Promise<string> {
    const existing = await this.entityMaskAliasRepository.findOne({
      where: { workspaceId, objectName, recordId },
    });

    if (existing) {
      return existing.token;
    }

    const prefix = TOKEN_PREFIX_BY_OBJECT_NAME[objectName] ?? 'ENTITY';

    try {
      return await this.entityMaskAliasRepository.manager.transaction(
        async (manager) => {
          // Serialize sequence assignment per (workspace, objectName). Postgres
          // forbids FOR UPDATE with aggregates, so use a transaction-scoped
          // advisory lock (released on commit/rollback) instead.
          await manager.query(
            `SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))`,
            [workspaceId, objectName],
          );

          const [{ next }] = await manager.query(
            `SELECT COALESCE(MAX("sequenceNumber"), 0) + 1 AS next
             FROM "core"."entityMaskAlias"
             WHERE "workspaceId" = $1 AND "objectName" = $2`,
            [workspaceId, objectName],
          );

          const sequenceNumber = Number(next);
          const token = `${prefix}_${String(sequenceNumber).padStart(3, '0')}`;

          const alias = manager.create(EntityMaskAliasEntity, {
            workspaceId,
            objectName,
            recordId,
            token,
            sequenceNumber,
          });

          await manager.save(alias);

          return token;
        },
      );
    } catch {
      // Lost a race — the row now exists; re-read it.
      const raced = await this.entityMaskAliasRepository.findOne({
        where: { workspaceId, objectName, recordId },
      });

      if (raced) {
        return raced.token;
      }

      throw new Error(
        `Failed to assign mask alias for ${objectName}:${recordId}`,
      );
    }
  }
}
