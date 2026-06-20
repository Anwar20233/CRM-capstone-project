"""Quick preflight for followup_batch_e2e.py — workspace, seed data, bridge."""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=False)

SAMPLE_SENDERS = (
    "john.park@airbnb.com",
    "alex.rivera@stripe.com",
    "kevin.cho@notion.com",
    "rachel.kim@datadog.com",
)


async def main() -> None:
    dsn = os.environ.get("PG_DATABASE_URL", "postgres://postgres:postgres@localhost:5432/default")
    env_workspace = os.environ.get("TWENTY_WORKSPACE_ID", "")
    bridge = os.environ.get("NODE_BRIDGE_BASE_URL", "")

    print("=== Batch preflight ===\n")
    print(f"NODE_BRIDGE_BASE_URL : {bridge or '(not set)'}")
    print(f"TWENTY_WORKSPACE_ID  : {env_workspace or '(not set)'}")

    try:
        import asyncpg

        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        print(f"\nPostgres: FAIL — {exc}")
        print("Start Postgres and set PG_DATABASE_URL in .env.")
        raise SystemExit(1)

    try:
        workspace_id = await conn.fetchval(
            'SELECT id FROM core.workspace ORDER BY "createdAt" LIMIT 1'
        )
        data_source = await conn.fetchrow(
            '''
            SELECT "workspaceId", "schema"
            FROM core."dataSource"
            WHERE "schema" IS NOT NULL
            ORDER BY "createdAt" DESC
            LIMIT 1
            '''
        )
        ws_schema = await conn.fetchval(
            "SELECT table_schema FROM information_schema.tables "
            "WHERE table_schema LIKE 'workspace\\_%' AND table_name = 'person' "
            "ORDER BY table_schema LIMIT 1"
        )

        print(f"\nPostgres: OK ({dsn})")
        print(f"core.workspace id      : {workspace_id}")
        if data_source:
            print(f"core.dataSource        : workspace={data_source['workspaceId']} schema={data_source['schema']}")
        else:
            print(
                "core.dataSource        : MISSING (risk agent uses schema discovery fallback)"
            )
        print(f"discovered ws schema   : {ws_schema or 'NONE'}")

        if env_workspace and workspace_id and str(workspace_id) != env_workspace:
            print("\nWARNING: TWENTY_WORKSPACE_ID in .env does not match core.workspace.")
            print(f"  Update .env to: TWENTY_WORKSPACE_ID={workspace_id}")

        if env_workspace and data_source:
            ds_for_env = await conn.fetchrow(
                '''
                SELECT "schema"
                FROM core."dataSource"
                WHERE "workspaceId" = $1::uuid AND "schema" IS NOT NULL
                LIMIT 1
                ''',
                env_workspace,
            )
            if not ds_for_env:
                print(
                    f"\nWARNING: No core.dataSource row for TWENTY_WORKSPACE_ID={env_workspace}."
                )
                print("  Risk assess_risk will fail with 'Workspace schema not found'.")

        if ws_schema:
            for sender in SAMPLE_SENDERS:
                row = await conn.fetchrow(
                    f'''
                    SELECT id, "emailsPrimaryEmail" AS email
                    FROM "{ws_schema}".person
                    WHERE "emailsPrimaryEmail" = $1
                      AND "deletedAt" IS NULL
                    LIMIT 1
                    ''',
                    sender,
                )
                status = "found" if row else "MISSING"
                print(f"  sender {sender:<30} {status}")

        followup_tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'followup_agent' AND table_name = 'profile_facts'"
        )
        print(f"\nfollowup_agent schema  : {'OK' if followup_tables else 'MISSING (run migrations/seed)'}")
    finally:
        await conn.close()

    print("\nIf senders are MISSING, run:  python seed_data.py")
    print("If workspace mismatch, fix TWENTY_WORKSPACE_ID in .env then re-run batch.")


if __name__ == "__main__":
    asyncio.run(main())
