#!/usr/bin/env python
"""Run the risk sweep once and print a JSON summary.

By default only processes opportunities that have never been scored or whose
CRM record was updated after their last score. Pass --all to re-score every
active (non-terminal) opportunity regardless.

Usage:
    python scripts/run_risk_sweep.py          # incremental (default)
    python scripts/run_risk_sweep.py --all    # full sweep
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import asdict

# Allow running from the package root without installing.
sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from followup.risk_agent.daily_sweep import (
    SweepSummary,
    run_daily_sweep,
    run_smart_sweep,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("risk_sweep")


def _print_summary(summary: SweepSummary) -> None:
    print(json.dumps(asdict(summary), indent=2, default=str))
    logger.info(
        "sweep done — scanned=%d scored=%d alerts=%d skipped=%d failed=%d",
        summary.scanned,
        summary.scored,
        summary.alerts_created,
        summary.skipped,
        summary.failed,
    )
    for result in summary.results:
        if result.error:
            logger.warning(
                "opportunity %s failed: %s",
                result.opportunity.opportunity_id,
                result.error,
            )


async def _run(*, full: bool) -> int:
    mode = "full" if full else "incremental"
    logger.info("starting risk sweep (mode=%s)", mode)
    summary = await run_daily_sweep() if full else await run_smart_sweep()
    _print_summary(summary)
    return 1 if summary.failed > 0 else 0


if __name__ == "__main__":
    full = "--all" in sys.argv
    sys.exit(asyncio.run(_run(full=full)))
