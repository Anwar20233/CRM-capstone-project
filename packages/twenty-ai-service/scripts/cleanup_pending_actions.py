#!/usr/bin/env python
"""Cleanup script to mark older or duplicate pending actions as 'expired'.

This script does two things:
1. Marks all pending actions that are past their expiration time as 'expired'.
2. For each opportunity, keeps only the single most recent pending action and
   marks any older pending actions as 'expired'.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

# Allow running from the package root without installing.
sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from followup.store.repositories import Database, _dsn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("cleanup_pending_actions")

async def cleanup() -> None:
    db_url = _dsn()
    logger.info("Connecting to database: %s", db_url)
    db = await Database.connect(db_url)
    try:
        async with db.pool.acquire() as conn:
            # 1. Mark past-expiry pending actions as expired
            res1 = await conn.execute(
                """
                UPDATE followup_agent.followup_pending_actions
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at < $1
                """,
                datetime.now(timezone.utc),
            )
            count1 = int(res1.split()[-1])
            logger.info("Expired %d stale actions based on expires_at", count1)

            # 2. For each opportunity, find all pending actions, keep the latest one (by created_at),
            #    and mark any others as expired.
            res2 = await conn.execute(
                """
                UPDATE followup_agent.followup_pending_actions
                SET status = 'expired'
                WHERE status = 'pending'
                  AND id NOT IN (
                    SELECT DISTINCT ON (opportunity_id) id
                    FROM followup_agent.followup_pending_actions
                    WHERE status = 'pending'
                    ORDER BY opportunity_id, created_at DESC
                  )
                """
            )
            count2 = int(res2.split()[-1])
            logger.info("Expired %d older duplicate/repetitive pending actions", count2)
            
            logger.info("Cleanup completed successfully.")
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(cleanup())
