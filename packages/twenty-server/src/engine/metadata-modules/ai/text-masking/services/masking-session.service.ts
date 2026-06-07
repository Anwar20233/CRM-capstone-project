import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';

import { isDefined } from 'twenty-shared/utils';
import { Repository } from 'typeorm';

import { MaskingSessionEntity } from 'src/engine/core-modules/text-masking/masking-session.entity';
import { type ReverseMap } from 'src/engine/core-modules/text-masking/types/reverse-map-entry.type';

export type MaskingSession = {
  id: string;
};

@Injectable()
export class MaskingSessionService {
  constructor(
    @InjectRepository(MaskingSessionEntity)
    private readonly maskingSessionRepository: Repository<MaskingSessionEntity>,
  ) {}

  // Resolve an existing session or create a new one. The session persists the
  // reverse map used by the later un-masking phase.
  async resolveOrCreate(
    workspaceId: string,
    userWorkspaceId: string | null,
    sessionId?: string,
  ): Promise<MaskingSession> {
    if (isDefined(sessionId)) {
      const existing = await this.maskingSessionRepository.findOne({
        where: { id: sessionId, workspaceId },
      });

      if (existing) {
        return { id: existing.id };
      }
    }

    const created = await this.maskingSessionRepository.save(
      this.maskingSessionRepository.create({
        workspaceId,
        userWorkspaceId,
        reverseMap: {},
      }),
    );

    return { id: created.id };
  }

  // Merge new token mappings into the persisted reverse map for un-masking later.
  async appendToReverseMap(
    sessionId: string,
    workspaceId: string,
    entries: ReverseMap,
  ): Promise<void> {
    const session = await this.maskingSessionRepository.findOne({
      where: { id: sessionId, workspaceId },
    });

    if (!session) {
      return;
    }

    await this.maskingSessionRepository.update(
      { id: sessionId },
      { reverseMap: { ...session.reverseMap, ...entries } },
    );
  }
}
