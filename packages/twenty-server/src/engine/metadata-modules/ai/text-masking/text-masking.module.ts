import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';

import { TypeORMModule } from 'src/database/typeorm/typeorm.module';
import { TokenModule } from 'src/engine/core-modules/auth/token/token.module';
import { SecureHttpClientModule } from 'src/engine/core-modules/secure-http-client/secure-http-client.module';
import { EntityMaskAliasEntity } from 'src/engine/core-modules/text-masking/entity-mask-alias.entity';
import { MaskingSessionEntity } from 'src/engine/core-modules/text-masking/masking-session.entity';
import { PermissionsModule } from 'src/engine/metadata-modules/permissions/permissions.module';
import { WorkspaceCacheStorageModule } from 'src/engine/workspace-cache-storage/workspace-cache-storage.module';
import { TextMaskingController } from 'src/engine/metadata-modules/ai/text-masking/controllers/text-masking.controller';
import { AiServiceClientService } from 'src/engine/metadata-modules/ai/text-masking/services/ai-service-client.service';
import { CrmRecordMatcherService } from 'src/engine/metadata-modules/ai/text-masking/services/crm-record-matcher.service';
import { EntityMaskAliasService } from 'src/engine/metadata-modules/ai/text-masking/services/entity-mask-alias.service';
import { MaskingSessionService } from 'src/engine/metadata-modules/ai/text-masking/services/masking-session.service';
import { TextMaskingService } from 'src/engine/metadata-modules/ai/text-masking/services/text-masking.service';

@Module({
  imports: [
    TokenModule,
    WorkspaceCacheStorageModule,
    PermissionsModule,
    SecureHttpClientModule,
    TypeORMModule,
    TypeOrmModule.forFeature([EntityMaskAliasEntity, MaskingSessionEntity]),
  ],
  controllers: [TextMaskingController],
  providers: [
    TextMaskingService,
    AiServiceClientService,
    CrmRecordMatcherService,
    EntityMaskAliasService,
    MaskingSessionService,
  ],
})
export class TextMaskingModule {}
