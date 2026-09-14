from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal, init_models
from .models import Agent, User
from .routes import agents as agent_routes
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarnessCleanupResult:
    deleted: int
    skipped: int
    failed: int


class HarnessCleanupWorker:
    """Remove stale Agent Studio acceptance-run agents.

    The selector is intentionally narrow: private agents owned by the dedicated
    harness user and named with the generated studio-harness prefix.
    """

    def __init__(
        self,
        *,
        owner_email: str | None = None,
        name_prefix: str | None = None,
        ttl_seconds: int | None = None,
        max_per_run: int | None = None,
    ) -> None:
        self.owner_email = (
            owner_email or settings.agent_studio_harness_cleanup_owner_email
        ).strip()
        self.name_prefix = (name_prefix or settings.agent_studio_harness_cleanup_name_prefix).strip()
        self.ttl_seconds = int(
            settings.agent_studio_harness_cleanup_ttl_seconds
            if ttl_seconds is None
            else ttl_seconds
        )
        self.max_per_run = int(
            settings.agent_studio_harness_cleanup_max_per_run
            if max_per_run is None
            else max_per_run
        )

    async def run_once(self, session: AsyncSession) -> HarnessCleanupResult:
        if self.ttl_seconds < 0 or self.max_per_run <= 0:
            return HarnessCleanupResult(deleted=0, skipped=0, failed=0)
        cutoff = datetime.now(UTC) - timedelta(seconds=self.ttl_seconds)
        rows = (
            await session.execute(
                select(Agent)
                .join(User, User.id == Agent.owner_id)
                .where(User.email == self.owner_email)
                .where(Agent.public.is_(False))
                .where(Agent.name.like(f"{self.name_prefix}%"))
                .where(Agent.created_at < cutoff)
                .order_by(Agent.created_at.asc())
                .limit(self.max_per_run)
            )
        ).scalars().all()

        deleted = skipped = failed = 0
        for agent in rows:
            outcome = await self._cleanup_one(session, agent)
            if outcome == "deleted":
                deleted += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
        if deleted or skipped or failed:
            log.info(
                "Agent Studio harness cleanup finished deleted=%s skipped=%s failed=%s",
                deleted,
                skipped,
                failed,
            )
        return HarnessCleanupResult(deleted=deleted, skipped=skipped, failed=failed)

    async def _cleanup_one(self, session: AsyncSession, agent: Agent) -> str:
        if agent.id is None:
            return "skipped"
        agent_id = int(agent.id)
        agent_name = agent.name
        repo_owner = agent.gitea_owner
        is_external = agent_routes._is_external_agent(agent)
        dependents = (
            await session.execute(
                select(Agent.id).where(Agent.source_agent_id == agent_id).limit(1)
            )
        ).scalar_one_or_none()
        if dependents is not None:
            log.warning("Skipping harness cleanup for %s: dependent agents exist", agent_name)
            return "skipped"

        cleanup_failures = (
            agent_routes._cleanup_external_agent_resources(agent_name)
            if is_external
            else agent_routes._cleanup_agent_resources(agent_name, repo_owner=repo_owner)
        )
        if cleanup_failures:
            await session.rollback()
            log.warning(
                "Harness cleanup failed for %s: %s",
                agent_name,
                "; ".join(cleanup_failures),
            )
            return "failed"

        await session.delete(agent)
        try:
            await session.commit()
        except Exception:  # noqa: BLE001
            await session.rollback()
            log.exception("Harness cleanup DB delete failed for %s", agent_name)
            return "failed"
        await agent_routes._delete_agent_from_search(agent_id, agent_name)
        log.info("Deleted stale Agent Studio harness agent %s", agent_name)
        return "deleted"


async def run_forever() -> None:
    await _init_models_with_retry()
    worker = HarnessCleanupWorker()
    interval = max(1.0, float(settings.agent_studio_harness_cleanup_interval_seconds))
    shutdown = install_shutdown_event("harness-cleanup-worker")
    while not shutdown.is_set():
        try:
            if not settings.agent_studio_harness_cleanup_enabled:
                await sleep_or_shutdown(shutdown, interval)
                continue
            async with SessionLocal() as session:
                result = await worker.run_once(session)
            if result.deleted == 0:
                await sleep_or_shutdown(shutdown, interval)
        except Exception:  # noqa: BLE001
            log.exception("Agent Studio harness cleanup worker loop failed")
            await sleep_or_shutdown(shutdown, interval)


async def _init_models_with_retry() -> None:
    interval = max(1.0, float(settings.agent_studio_harness_cleanup_interval_seconds))
    while True:
        try:
            await init_models()
            return
        except Exception:  # noqa: BLE001
            log.exception("Agent Studio harness cleanup model init failed; retrying")
            await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
