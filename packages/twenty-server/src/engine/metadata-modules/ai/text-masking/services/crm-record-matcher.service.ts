import { Injectable } from '@nestjs/common';

import { isNonEmptyString } from '@sniptt/guards';
import { type ObjectLiteral } from 'typeorm';

import { GlobalWorkspaceOrmManager } from 'src/engine/twenty-orm/global-workspace-datasource/global-workspace-orm.manager';
import { type WorkspaceRepository } from 'src/engine/twenty-orm/repository/workspace.repository';
import { addPersonEmailFiltersToQueryBuilder } from 'src/modules/match-participant/utils/add-person-email-filters-to-query-builder';
import { findPersonByPrimaryOrAdditionalEmail } from 'src/modules/match-participant/utils/find-person-by-primary-or-additional-email';
import { type CompanyWorkspaceEntity } from 'src/modules/company/standard-objects/company.workspace-entity';
import { type OpportunityWorkspaceEntity } from 'src/modules/opportunity/standard-objects/opportunity.workspace-entity';
import { type PersonWorkspaceEntity } from 'src/modules/person/standard-objects/person.workspace-entity';

const BYPASS_PERMISSIONS = { shouldBypassPermissionChecks: true } as const;

const normalizeName = (value: string): string =>
  value.trim().replace(/\s+/g, ' ').toLowerCase();

// Resolves NER-extracted spans to existing CRM records. Reuses Twenty's email
// (person) matcher; adds normalized name matching since Twenty has no
// name-based person/company/opportunity lookup. Must run inside an active
// workspace context (executeInWorkspaceContext).
@Injectable()
export class CrmRecordMatcherService {
  constructor(
    private readonly globalWorkspaceOrmManager: GlobalWorkspaceOrmManager,
  ) {}

  // Highest-confidence person signal — exact (case-insensitive) email match.
  async matchPersonByEmail(
    workspaceId: string,
    email: string,
  ): Promise<string | null> {
    const personRepository =
      await this.globalWorkspaceOrmManager.getRepository<PersonWorkspaceEntity>(
        workspaceId,
        'person',
        BYPASS_PERMISSIONS,
      );

    const people = await addPersonEmailFiltersToQueryBuilder({
      queryBuilder: personRepository.createQueryBuilder('person'),
      emails: [email],
    })
      .orderBy('person.createdAt', 'ASC')
      .getMany();

    return findPersonByPrimaryOrAdditionalEmail({ people, email })?.id ?? null;
  }

  // Normalized full-name match, tolerant of order and first/last-only mentions.
  async matchPersonByName(
    workspaceId: string,
    name: string,
  ): Promise<string | null> {
    const normalized = normalizeName(name);

    if (!isNonEmptyString(normalized)) {
      return null;
    }

    const personRepository =
      await this.globalWorkspaceOrmManager.getRepository<PersonWorkspaceEntity>(
        workspaceId,
        'person',
        BYPASS_PERMISSIONS,
      );

    const match = await personRepository
      .createQueryBuilder('person')
      .where(
        `LOWER(TRIM(COALESCE("person"."nameFirstName", '') || ' ' || COALESCE("person"."nameLastName", ''))) = :name`,
        { name: normalized },
      )
      .orWhere(
        `LOWER(TRIM(COALESCE("person"."nameLastName", '') || ' ' || COALESCE("person"."nameFirstName", ''))) = :name`,
        { name: normalized },
      )
      .orWhere(`LOWER("person"."nameFirstName") = :name`, { name: normalized })
      .orWhere(`LOWER("person"."nameLastName") = :name`, { name: normalized })
      .orderBy('person.createdAt', 'ASC')
      .getOne();

    return match?.id ?? null;
  }

  async matchCompanyByName(
    workspaceId: string,
    name: string,
  ): Promise<string | null> {
    const companyRepository =
      await this.globalWorkspaceOrmManager.getRepository<CompanyWorkspaceEntity>(
        workspaceId,
        'company',
        BYPASS_PERMISSIONS,
      );

    return this.matchByNameColumn(companyRepository, 'company', name);
  }

  async matchOpportunityByName(
    workspaceId: string,
    name: string,
  ): Promise<string | null> {
    const opportunityRepository =
      await this.globalWorkspaceOrmManager.getRepository<OpportunityWorkspaceEntity>(
        workspaceId,
        'opportunity',
        BYPASS_PERMISSIONS,
      );

    return this.matchByNameColumn(opportunityRepository, 'opportunity', name);
  }

  // Exact normalized match first, then an unaccent ILIKE contains fallback.
  private async matchByNameColumn<T extends ObjectLiteral & { id: string }>(
    repository: WorkspaceRepository<T>,
    alias: string,
    name: string,
  ): Promise<string | null> {
    const normalized = normalizeName(name);

    if (!isNonEmptyString(normalized)) {
      return null;
    }

    const exact = await repository
      .createQueryBuilder(alias)
      .where(`LOWER("${alias}"."name") = :name`, { name: normalized })
      .orderBy(`${alias}.createdAt`, 'ASC')
      .getOne();

    if (exact) {
      return exact.id;
    }

    const fuzzy = await repository
      .createQueryBuilder(alias)
      .where(
        `public.unaccent_immutable(LOWER("${alias}"."name")) ILIKE public.unaccent_immutable(:pattern)`,
        { pattern: `%${normalized}%` },
      )
      .orderBy(`${alias}.createdAt`, 'ASC')
      .getOne();

    return fuzzy?.id ?? null;
  }
}
