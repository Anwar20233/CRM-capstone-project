import { Injectable } from '@nestjs/common';

import { isDefined } from 'twenty-shared/utils';

import { type ReverseMap } from 'src/engine/core-modules/text-masking/types/reverse-map-entry.type';
import {
  type MaskedEntity,
  type MaskedEntityType,
  type MaskTextOutput,
} from 'src/engine/metadata-modules/ai/text-masking/dtos/mask-text.output';
import { AiServiceClientService } from 'src/engine/metadata-modules/ai/text-masking/services/ai-service-client.service';
import { CrmRecordMatcherService } from 'src/engine/metadata-modules/ai/text-masking/services/crm-record-matcher.service';
import { EntityMaskAliasService } from 'src/engine/metadata-modules/ai/text-masking/services/entity-mask-alias.service';
import { MaskingSessionService } from 'src/engine/metadata-modules/ai/text-masking/services/masking-session.service';
import { type NerEntity } from 'src/engine/metadata-modules/ai/text-masking/types/ner-entity.type';
import { applyReplacements } from 'src/engine/metadata-modules/ai/text-masking/utils/apply-replacements.util';
import {
  obfuscateMoneyText,
  parseMoneyValue,
} from 'src/engine/metadata-modules/ai/text-masking/utils/parse-and-obfuscate-money.util';
import { GlobalWorkspaceOrmManager } from 'src/engine/twenty-orm/global-workspace-datasource/global-workspace-orm.manager';
import { buildSystemAuthContext } from 'src/engine/twenty-orm/utils/build-system-auth-context.util';

type MatchObjectName = 'person' | 'company' | 'opportunity';

// NER labels that we attempt to resolve to a CRM record, and the object they map to.
const RECORD_LABEL_TO_OBJECT: Record<string, MatchObjectName> = {
  person: 'person',
  'email address': 'person',
  company: 'company',
  competitor: 'company',
  deal: 'opportunity',
};

@Injectable()
export class TextMaskingService {
  constructor(
    private readonly aiServiceClientService: AiServiceClientService,
    private readonly crmRecordMatcherService: CrmRecordMatcherService,
    private readonly entityMaskAliasService: EntityMaskAliasService,
    private readonly maskingSessionService: MaskingSessionService,
    private readonly globalWorkspaceOrmManager: GlobalWorkspaceOrmManager,
  ) {}

  async maskText({
    workspaceId,
    userWorkspaceId,
    text,
    sessionId,
  }: {
    workspaceId: string;
    userWorkspaceId: string | null;
    text: string;
    sessionId?: string;
  }): Promise<MaskTextOutput> {
    const nerEntities = await this.aiServiceClientService.extractEntities(text);

    const session = await this.maskingSessionService.resolveOrCreate(
      workspaceId,
      userWorkspaceId,
      sessionId,
    );

    const { entities, reverseMap } = await this.buildEntities({
      workspaceId,
      nerEntities,
      priceFactor: session.priceFactor,
    });

    const maskedText = applyReplacements(
      text,
      entities
        .filter(
          (entity) =>
            isDefined(entity.masked) &&
            isDefined(entity.start) &&
            isDefined(entity.end),
        )
        .map((entity) => ({
          start: entity.start as number,
          end: entity.end as number,
          replacement: entity.masked as string,
        })),
    );

    await this.maskingSessionService.appendToReverseMap(
      session.id,
      workspaceId,
      reverseMap,
    );

    return {
      maskedText,
      sessionId: session.id,
      entities,
    };
  }

  // Builds one MaskedEntity per detected NER span (masked and unmasked) and the
  // reverse map for the entities we did mask.
  private async buildEntities({
    workspaceId,
    nerEntities,
    priceFactor,
  }: {
    workspaceId: string;
    nerEntities: NerEntity[];
    priceFactor: number;
  }): Promise<{
    entities: MaskedEntity[];
    reverseMap: ReverseMap;
  }> {
    const reverseMap: ReverseMap = {};
    const amountSequence = { current: 0 };

    return this.globalWorkspaceOrmManager.executeInWorkspaceContext(
      async () => {
        const entities: MaskedEntity[] = [];

        for (const nerEntity of nerEntities) {
          if (nerEntity.label === 'money') {
            entities.push(
              this.buildMoneyEntity(
                nerEntity,
                priceFactor,
                amountSequence,
                reverseMap,
              ),
            );
            continue;
          }

          const objectName = RECORD_LABEL_TO_OBJECT[nerEntity.label];
          const type: MaskedEntityType = objectName ?? 'other';

          // Detected but not a maskable record type (date, location, …).
          if (!isDefined(objectName)) {
            entities.push(this.buildUnmaskedEntity(nerEntity, 'other'));
            continue;
          }

          const recordId = await this.matchEntity(
            workspaceId,
            objectName,
            nerEntity,
          );

          // Detected but not in the CRM — highlighted, but not masked.
          if (!isDefined(recordId)) {
            entities.push(this.buildUnmaskedEntity(nerEntity, type));
            continue;
          }

          const token = await this.entityMaskAliasService.getOrCreateToken(
            workspaceId,
            objectName,
            recordId,
          );

          entities.push({
            label: nerEntity.label,
            type,
            originalText: nerEntity.text,
            start: nerEntity.start,
            end: nerEntity.end,
            masked: token,
            token,
            recordId,
          });

          reverseMap[token] = {
            token,
            type: objectName,
            recordId,
            originalText: nerEntity.text,
          };
        }

        return { entities, reverseMap };
      },
      buildSystemAuthContext(workspaceId),
    );
  }

  private buildUnmaskedEntity(
    nerEntity: NerEntity,
    type: MaskedEntityType,
  ): MaskedEntity {
    return {
      label: nerEntity.label,
      type,
      originalText: nerEntity.text,
      start: nerEntity.start,
      end: nerEntity.end,
      masked: null,
      token: null,
    };
  }

  private buildMoneyEntity(
    nerEntity: NerEntity,
    priceFactor: number,
    amountSequence: { current: number },
    reverseMap: ReverseMap,
  ): MaskedEntity {
    const originalValue = parseMoneyValue(nerEntity.text);

    // No parseable number (e.g. "six figures") — highlight but don't obfuscate.
    if (!isDefined(originalValue)) {
      return this.buildUnmaskedEntity(nerEntity, 'money');
    }

    amountSequence.current += 1;
    const token = `AMOUNT_${String(amountSequence.current).padStart(3, '0')}`;
    const obfuscatedText = obfuscateMoneyText(
      nerEntity.text,
      originalValue,
      priceFactor,
    );

    reverseMap[token] = {
      token,
      type: 'money',
      originalText: nerEntity.text,
      originalValue,
      obfuscatedValue: Math.round(originalValue * priceFactor),
      obfuscatedText,
    };

    return {
      label: nerEntity.label,
      type: 'money',
      originalText: nerEntity.text,
      start: nerEntity.start,
      end: nerEntity.end,
      masked: obfuscatedText,
      token,
    };
  }

  private async matchEntity(
    workspaceId: string,
    objectName: MatchObjectName,
    entity: NerEntity,
  ): Promise<string | null> {
    if (objectName === 'person') {
      // Email is the highest-confidence signal; otherwise match by name.
      if (entity.label === 'email address') {
        return this.crmRecordMatcherService.matchPersonByEmail(
          workspaceId,
          entity.text,
        );
      }

      return this.crmRecordMatcherService.matchPersonByName(
        workspaceId,
        entity.text,
      );
    }

    if (objectName === 'company') {
      return this.crmRecordMatcherService.matchCompanyByName(
        workspaceId,
        entity.text,
      );
    }

    return this.crmRecordMatcherService.matchOpportunityByName(
      workspaceId,
      entity.text,
    );
  }
}
