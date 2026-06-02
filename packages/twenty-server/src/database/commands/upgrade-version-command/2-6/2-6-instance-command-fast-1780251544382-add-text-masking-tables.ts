import { QueryRunner } from 'typeorm';

import { RegisteredInstanceCommand } from 'src/engine/core-modules/upgrade/decorators/registered-instance-command.decorator';
import { FastInstanceCommand } from 'src/engine/core-modules/upgrade/interfaces/fast-instance-command.interface';

@RegisteredInstanceCommand('2.6.0', 1780251544382)
export class AddTextMaskingTablesFastInstanceCommand
  implements FastInstanceCommand
{
  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      'CREATE TABLE "core"."entityMaskAlias" ("id" uuid NOT NULL DEFAULT uuid_generate_v4(), "workspaceId" uuid NOT NULL, "objectName" text NOT NULL, "recordId" uuid NOT NULL, "token" text NOT NULL, "sequenceNumber" integer NOT NULL, "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), CONSTRAINT "IDX_MASK_ALIAS_OBJECT_TOKEN_UNIQUE" UNIQUE ("workspaceId", "objectName", "token"), CONSTRAINT "IDX_MASK_ALIAS_OBJECT_RECORD_UNIQUE" UNIQUE ("workspaceId", "objectName", "recordId"), CONSTRAINT "PK_67537d1dd0bc16654661337534d" PRIMARY KEY ("id"))',
    );
    await queryRunner.query(
      'CREATE TABLE "core"."maskingSession" ("id" uuid NOT NULL DEFAULT uuid_generate_v4(), "workspaceId" uuid NOT NULL, "userWorkspaceId" uuid, "priceFactor" double precision NOT NULL DEFAULT \'1\', "reverseMap" jsonb NOT NULL DEFAULT \'{}\', "createdAt" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), "expiresAt" TIMESTAMP WITH TIME ZONE, CONSTRAINT "PK_9fd16d18058cbea68df2e6f1c20" PRIMARY KEY ("id"))',
    );
    await queryRunner.query(
      'ALTER TABLE "core"."entityMaskAlias" ADD CONSTRAINT "FK_7ab68ede576e07624fa6594b370" FOREIGN KEY ("workspaceId") REFERENCES "core"."workspace"("id") ON DELETE CASCADE ON UPDATE NO ACTION',
    );
    await queryRunner.query(
      'ALTER TABLE "core"."maskingSession" ADD CONSTRAINT "FK_1b6d5aa6645ca1b045a7d0a9c17" FOREIGN KEY ("workspaceId") REFERENCES "core"."workspace"("id") ON DELETE CASCADE ON UPDATE NO ACTION',
    );
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      'ALTER TABLE "core"."maskingSession" DROP CONSTRAINT "FK_1b6d5aa6645ca1b045a7d0a9c17"',
    );
    await queryRunner.query(
      'ALTER TABLE "core"."entityMaskAlias" DROP CONSTRAINT "FK_7ab68ede576e07624fa6594b370"',
    );
    await queryRunner.query('DROP TABLE "core"."maskingSession"');
    await queryRunner.query('DROP TABLE "core"."entityMaskAlias"');
  }
}
