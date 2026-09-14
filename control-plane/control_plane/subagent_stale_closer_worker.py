from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal, init_models
from .subagent_runs import close_stale_subagent_runs

log = logging.getLogger(__name__)


class SubagentStaleCloserWorker:
    async def run_once(self, session: AsyncSession) -> int:
        closed = await close_stale_subagent_runs(
            session,
            stale_after_seconds=settings.subagent_stale_after_seconds,
            limit=settings.subagent_stale_batch_size,
        )
        if closed:
            log.info("Closed %s stale subagent run(s)", closed)
        return closed


async def run_forever() -> None:
    await _init_models_with_retry()
    worker = SubagentStaleCloserWorker()
    interval = max(1.0, float(settings.subagent_stale_closer_interval_seconds))
    while True:
        try:
            if not settings.subagent_stale_closer_enabled:
                await asyncio.sleep(interval)
                continue
            async with SessionLocal() as session:
                closed = await worker.run_once(session)
            if closed == 0:
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Subagent stale closer worker loop failed")
            await asyncio.sleep(interval)


async def _init_models_with_retry() -> None:
    interval = max(1.0, float(settings.subagent_stale_closer_interval_seconds))
    while True:
        try:
            await init_models()
            return
        except Exception:  # noqa: BLE001
            log.exception("Subagent stale closer model init failed; retrying")
            await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
