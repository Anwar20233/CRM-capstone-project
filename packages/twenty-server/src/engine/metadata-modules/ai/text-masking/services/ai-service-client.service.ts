import { Injectable, Logger } from '@nestjs/common';

import { isDefined } from 'twenty-shared/utils';

import { SecureHttpClientService } from 'src/engine/core-modules/secure-http-client/secure-http-client.service';
import { TwentyConfigService } from 'src/engine/core-modules/twenty-config/twenty-config.service';
import {
  TextMaskingException,
  TextMaskingExceptionCode,
} from 'src/engine/metadata-modules/ai/text-masking/text-masking.exception';
import { type NerEntity } from 'src/engine/metadata-modules/ai/text-masking/types/ner-entity.type';

// Thin client for the single global Python AI service. The base URL is shared;
// the route selects the capability (NER today, agent routes in later phases).
@Injectable()
export class AiServiceClientService {
  private readonly logger = new Logger(AiServiceClientService.name);

  constructor(
    private readonly secureHttpClientService: SecureHttpClientService,
    private readonly twentyConfigService: TwentyConfigService,
  ) {}

  async extractEntities(text: string): Promise<NerEntity[]> {
    const baseUrl = this.twentyConfigService.get('AI_SERVICE_URL');

    // The AI service is a trusted internal service (loopback in dev). Use the
    // internal client — the SSRF-safe client blocks internal/loopback IPs.
    const httpClient = this.secureHttpClientService.getInternalHttpClient({
      timeout: 30_000,
    });

    try {
      const { data } = await httpClient.post<{ entities: NerEntity[] }>(
        `${baseUrl}/ner/extract`,
        { text },
      );

      return isDefined(data?.entities) ? data.entities : [];
    } catch (error) {
      this.logger.error(`NER extraction failed: ${error}`);

      throw new TextMaskingException(
        'Failed to reach the AI service for entity extraction.',
        TextMaskingExceptionCode.AI_SERVICE_UNAVAILABLE,
      );
    }
  }
}
