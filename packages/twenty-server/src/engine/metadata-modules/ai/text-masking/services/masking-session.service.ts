import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';

import { isDefined } from 'twenty-shared/utils';
import { Repository } from 'typeorm';

import { InjectCacheStorage } from 'src/engine/core-modules/cache-storage/decorators/cache-storage.decorator';
import { CacheStorageService } from 'src/engine/core-modules/cache-storage/services/cache-storage.service';
import { CacheStorageNamespace } from 'src/engine/core-modules/cache-storage/types/cache-storage-namespace.enum';
import { MaskingSessionEntity } from 'src/engine/core-modules/text-masking/masking-session.entity';
import { type ReverseMap } from 'src/engine/core-modules/text-masking/types/reverse-map-entry.type';

// Price factor k bounds: amounts are multiplied by a constant in [0.7, 1.3]
// per session so the LLM keeps a sense of magnitude without seeing real figures.
const PRICE_FACTOR_MIN = 0.7;
const PRICE_FACTOR_RANGE = 0.6;

const PRICE_FACTOR_TTL_MS = 24 * 60 * 60 * 1000;

export type MaskingSession = {
  id: string;
  priceFactor: number;
};

@Injectable()
export class MaskingSessionService {
  constructor(
    @InjectRepository(MaskingSessionEntity)
    private readonly maskingSessionRepository: Repository<MaskingSessionEntity>,
    @InjectCacheStorage(CacheStorageNamespace.EngineTextMasking)
    private readonly cacheStorage: CacheStorageService,
  ) {}

  // Resolve an existing session (reusing its price factor) or create a new one.
  async resolveOrCreate(
    workspaceId: string,
    userWorkspaceId: string | null,
    sessionId?: string,
  ): Promise<MaskingSession> {
    if (isDefined(sessionId)) {
      const cachedFactor = await this.cacheStorage.get<number>(
        this.priceFactorKey(sessionId),
      );

      if (isDefined(cachedFactor)) {
        return { id: sessionId, priceFactor: cachedFactor };
      }

      const existing = await this.maskingSessionRepository.findOne({
        where: { id: sessionId, workspaceId },
      });

      if (existing) {
        await this.cachePriceFactor(existing.id, existing.priceFactor);

        return { id: existing.id, priceFactor: existing.priceFactor };
      }
    }

    const priceFactor = PRICE_FACTOR_MIN + Math.random() * PRICE_FACTOR_RANGE;

    const created = await this.maskingSessionRepository.save(
      this.maskingSessionRepository.create({
        workspaceId,
        userWorkspaceId,
        priceFactor,
        reverseMap: {},
      }),
    );

    await this.cachePriceFactor(created.id, priceFactor);

    return { id: created.id, priceFactor };
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

  private async cachePriceFactor(
    sessionId: string,
    priceFactor: number,
  ): Promise<void> {
    await this.cacheStorage.set(
      this.priceFactorKey(sessionId),
      priceFactor,
      PRICE_FACTOR_TTL_MS,
    );
  }

  private priceFactorKey(sessionId: string): string {
    return `price-factor:${sessionId}`;
  }
}
