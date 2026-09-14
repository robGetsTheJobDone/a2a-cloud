from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal, init_models
from .deployments import utcnow
from .job_wake import AgentApiJobWake
from .models import WorkJob
from .routes.agents import _AGENT_API_RUN_KIND, _execute_agent_api_run_job
from .worker_shutdown import install_shutdown_event

log = logging.getLogger(__name__)


class AgentApiRunWorker:
    async def run_once(self, session: AsyncSession) -> bool:
        job = await self._next_job(session)
        if job is None:
            return False
        await _execute_agent_api_run_job(session=session, job_id=str(job.job_id))
        return True

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        job = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "queued")
                .where(WorkJob.kind == _AGENT_API_RUN_KIND)
                .where(or_(WorkJob.queued_at.is_(None), WorkJob.queued_at <= utcnow()))
                .order_by(
                    WorkJob.priority.desc(),
                    WorkJob.created_at.asc(),
                    WorkJob.id.asc(),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        now = utcnow()
        job.status = "running"
        job.attempt = (job.attempt or 0) + 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.leased_until = now + timedelta(
            seconds=self._lease_seconds(job)
        )
        await session.commit()
        await session.refresh(job)
        return job

    def _lease_seconds(self, job: WorkJob) -> int:
        seconds = max(60, int(settings.agent_api_worker_lease_seconds))
        payload = job.input_payload if isinstance(job.input_payload, dict) else {}
        arguments = (
            payload.get("arguments")
            if isinstance(payload.get("arguments"), dict)
            else {}
        )
        requested_timeout = arguments.get("timeout_seconds")
        if isinstance(requested_timeout, bool):
            return seconds
        try:
            requested_seconds = int(float(requested_timeout))
        except (TypeError, ValueError):
            return seconds
        return max(seconds, requested_seconds + 300)

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        now = utcnow()
        result = await session.execute(
            update(WorkJob)
            .where(WorkJob.kind == _AGENT_API_RUN_KIND)
            .where(WorkJob.status == "running")
            .where(WorkJob.leased_until.is_not(None))
            .where(WorkJob.leased_until <= now)
            .values(
                status="queued",
                summary="Agent API run lease expired; requeued",
                queued_at=now,
                heartbeat_at=None,
                leased_until=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if result.rowcount:
            session.expire_all()


async def run_forever() -> None:
    await init_models()
    worker = AgentApiRunWorker()
    interval = max(1.0, float(settings.agent_api_worker_interval_seconds))
    shutdown = install_shutdown_event("agent-api-worker")
    # Redis wake channel: enqueuers nudge so pickup is immediate; the poll
    # interval is only the fallback when Redis is down or a nudge is missed.
    wake = AgentApiJobWake()
    await wake.connect()
    while not shutdown.is_set():
        try:
            if not settings.agent_api_worker_enabled:
                await wake.wait(interval)
                continue
            async with SessionLocal() as session:
                processed = await worker.run_once(session)
            if not processed:
                await wake.wait(interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Agent API worker loop failed")
            await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
