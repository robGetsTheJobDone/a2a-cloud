"""Durable worker for Agent Studio builds.

Studio builds can run for an hour, so they must not belong to an API pod's
lifespan. This worker leases work-ledger jobs, refreshes the lease while the
coordinator streams, and lets an expired lease retry after a hard process
failure. It also adopts active runs created by the legacy in-process runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .agent_studio_runs import (
    STUDIO_GRANT_TTL_S,
    STUDIO_JOB_KIND,
    STUDIO_SKILL,
    STUDIO_UPGRADE_SKILL,
    _record_event,
    enqueue_studio_build,
    run_studio_build,
)
from .auth import issue_studio_job_token
from .config import settings
from .db import SessionLocal, init_models
from .deployments import utcnow
from .models import AgentStudioRun, AgentStudioRunEvent, WorkJob
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown
from .work_ledger import append_event, complete_job, fail_job

log = logging.getLogger(__name__)

ACTIVE_STUDIO_STATUSES = {
    "queued",
    "building",
    "evaluating",
    "reviewing",
    "improving",
    "deploying",
}

StudioRunner = Callable[..., Awaitable[None]]


class AgentStudioWorker:
    def __init__(self, runner: StudioRunner = run_studio_build) -> None:
        self._runner = runner

    async def run_once(self, session: AsyncSession) -> bool:
        await self._adopt_stale_legacy_runs(session)
        job = await self._next_job(session)
        if job is None:
            return False

        payload = job.input_payload if isinstance(job.input_payload, dict) else {}
        run_id = str(payload.get("run_id") or "").strip()
        existing_run = await self._run(session, run_id)
        if existing_run is not None and existing_run.status == "live":
            await self._complete_live_job(session, job, existing_run)
            return True
        if existing_run is not None and existing_run.status == "failed":
            await self._fail_job_and_run(
                session,
                job,
                run_id,
                existing_run.stop_reason or "Studio build already failed",
            )
            return True
        heartbeat_stop = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_lease(job.job_id, heartbeat_stop),
            name=f"studio-lease-{job.job_id[:8]}",
        )
        try:
            await self._execute_job(payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("Studio worker job %s failed", job.job_id)
            await self._fail_job_and_run(session, job, run_id, str(exc))
            return True
        finally:
            heartbeat_stop.set()
            await heartbeat

        session.expire_all()
        run = await self._run(session, run_id)
        if run is not None and run.status == "live":
            await self._complete_live_job(session, job, run)
        else:
            reason = (
                run.stop_reason
                if run is not None and run.stop_reason
                else "Studio build ended without a terminal live result"
            )
            await self._fail_job_and_run(session, job, run_id, reason)
        return True

    async def _execute_job(self, payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "").strip()
        agent_name = str(payload.get("agent_name") or "").strip()
        user_id = int(payload.get("builder_user_id") or 0)
        cp_url = str(payload.get("cp_url") or settings.public_cp_url).strip()
        skill_name = str(payload.get("skill_name") or STUDIO_SKILL).strip()
        skill_args = payload.get("skill_args")
        if not run_id or not agent_name or user_id <= 0 or not isinstance(skill_args, dict):
            raise RuntimeError("Studio job payload is incomplete")
        if skill_name not in {STUDIO_SKILL, STUDIO_UPGRADE_SKILL}:
            raise RuntimeError("Studio job payload has an unsupported skill")
        await self._runner(
            run_id=run_id,
            agent_name=agent_name,
            user_id=user_id,
            user_jwt=issue_studio_job_token(
                user_id,
                run_id=run_id,
                target_agent=agent_name,
                ttl_seconds=STUDIO_GRANT_TTL_S,
            ),
            cp_url=cp_url,
            skill_name=skill_name,
            skill_args=skill_args,
            budget_cents=int(payload.get("budget_cents") or 500),
        )

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        job = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.kind == STUDIO_JOB_KIND)
                .where(WorkJob.status == "queued")
                .where(WorkJob.attempt < WorkJob.max_attempts)
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
            seconds=max(30, int(settings.agent_studio_worker_lease_seconds))
        )
        await append_event(
            session,
            job,
            event_type="agent_studio_build_started",
            message="Agent Studio worker leased the build",
            status="running",
            commit=False,
        )
        await session.commit()
        await session.refresh(job)
        return job

    async def _heartbeat_lease(self, job_id: str, stop: asyncio.Event) -> None:
        lease_seconds = max(30, int(settings.agent_studio_worker_lease_seconds))
        interval = max(5.0, min(30.0, lease_seconds / 3))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
            now = utcnow()
            try:
                async with SessionLocal() as session:
                    await session.execute(
                        update(WorkJob)
                        .where(WorkJob.job_id == job_id)
                        .where(WorkJob.kind == STUDIO_JOB_KIND)
                        .where(WorkJob.status == "running")
                        .values(
                            heartbeat_at=now,
                            leased_until=now + timedelta(seconds=lease_seconds),
                            updated_at=now,
                        )
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001
                log.exception("Could not refresh Studio job lease %s", job_id)

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        expired = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.kind == STUDIO_JOB_KIND)
                .where(WorkJob.status == "running")
                .where(WorkJob.leased_until.is_not(None))
                .where(WorkJob.leased_until <= utcnow())
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for job in expired:
            payload = job.input_payload if isinstance(job.input_payload, dict) else {}
            run_id = str(payload.get("run_id") or "").strip()
            run = await self._run(session, run_id)
            if run is not None and run.status == "live":
                await self._complete_live_job(session, job, run, commit=False)
                continue
            if (job.attempt or 0) < (job.max_attempts or 1):
                now = utcnow()
                job.status = "queued"
                job.summary = "Studio worker lease expired; requeued"
                job.queued_at = now
                job.heartbeat_at = None
                job.leased_until = None
                job.updated_at = now
                if run is not None and run.status in ACTIVE_STUDIO_STATUSES:
                    run.status = "queued"
                    run.stop_reason = None
                    await _record_event(
                        session,
                        run,
                        phase="build",
                        actor="coordinator",
                        status="running",
                        message="The build worker was interrupted. Resuming from saved source.",
                    )
                await append_event(
                    session,
                    job,
                    event_type="agent_studio_build_requeued",
                    message=job.summary,
                    status="queued",
                    commit=False,
                )
            else:
                await self._fail_job_and_run(
                    session,
                    job,
                    run_id,
                    "The build worker was interrupted repeatedly and could not recover.",
                    commit=False,
                )
        if expired:
            await session.commit()

    async def _adopt_stale_legacy_runs(self, session: AsyncSession) -> None:
        """Create jobs for pre-worker runs whose in-process task disappeared."""
        cutoff = utcnow() - timedelta(
            seconds=max(30, int(settings.agent_studio_stale_after_seconds))
        )
        latest_event_at = (
            select(func.max(AgentStudioRunEvent.created_at))
            .where(AgentStudioRunEvent.run_id == AgentStudioRun.run_id)
            .correlate(AgentStudioRun)
            .scalar_subquery()
        )
        has_job = (
            select(WorkJob.id)
            .where(WorkJob.kind == STUDIO_JOB_KIND)
            .where(WorkJob.subject_type == "agent_studio_run")
            .where(WorkJob.subject_id == AgentStudioRun.run_id)
            .exists()
        )
        runs = (
            await session.execute(
                select(AgentStudioRun)
                .where(AgentStudioRun.status.in_(ACTIVE_STUDIO_STATUSES))
                .where(~has_job)
                .where(
                    func.coalesce(
                        latest_event_at,
                        AgentStudioRun.updated_at,
                        AgentStudioRun.created_at,
                    )
                    <= cutoff
                )
                .order_by(AgentStudioRun.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(25)
            )
        ).scalars().all()
        if not runs:
            return
        for run in runs:
            builder_user_id = run.user_id
            if builder_user_id is None:
                await self._mark_run_failed(
                    session,
                    run,
                    "The interrupted build could not recover its builder account.",
                )
                continue
            await enqueue_studio_build(
                session,
                run_id=run.run_id,
                agent_name=run.agent_name,
                user_id=builder_user_id,
                brief=run.brief or {},
                skill_name=(
                    STUDIO_UPGRADE_SKILL
                    if run.action == "edit_existing"
                    else STUDIO_SKILL
                ),
                skill_args=(
                    {
                        "name": run.agent_name,
                        "idea": str((run.brief or {}).get("goal") or ""),
                        "proposal_id": run.run_id,
                        "expected_head_sha": run.source_sha or "",
                        "evidence_json": json.dumps(
                            run.authorization_snapshot or {},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                    if run.action == "edit_existing"
                    else None
                ),
            )
            run.status = "queued"
            run.stop_reason = None
            await _record_event(
                session,
                run,
                phase="build",
                actor="coordinator",
                status="running",
                message="Recovered an interrupted build and queued it on the durable worker.",
            )
        await session.commit()

    async def _fail_job_and_run(
        self,
        session: AsyncSession,
        job: WorkJob,
        run_id: str,
        reason: str,
        *,
        commit: bool = True,
    ) -> None:
        run = await self._run(session, run_id)
        if run is not None and run.status != "live":
            await self._mark_run_failed(session, run, reason)
        await fail_job(
            session,
            job,
            error=reason,
            summary="Studio build failed",
            status="failed",
            event_type="agent_studio_build_failed",
            commit=commit,
        )

    @staticmethod
    async def _complete_live_job(
        session: AsyncSession,
        job: WorkJob,
        run: AgentStudioRun,
        *,
        commit: bool = True,
    ) -> None:
        await complete_job(
            session,
            job,
            result=run.report or {"run_id": run.run_id, "status": "live"},
            summary=f"Studio agent {run.agent_name} is live",
            status="complete",
            event_type="agent_studio_build_completed",
            commit=commit,
        )

    @staticmethod
    async def _mark_run_failed(
        session: AsyncSession, run: AgentStudioRun, reason: str
    ) -> None:
        run.status = "failed"
        run.stop_reason = reason[:2000]
        await _record_event(
            session,
            run,
            phase="build",
            actor="coordinator",
            status="failed",
            message=reason,
        )

    @staticmethod
    async def _run(session: AsyncSession, run_id: str) -> AgentStudioRun | None:
        if not run_id:
            return None
        return (
            await session.execute(
                select(AgentStudioRun).where(AgentStudioRun.run_id == run_id)
            )
        ).scalar_one_or_none()


async def run_forever() -> None:
    await init_models()
    worker = AgentStudioWorker()
    shutdown = install_shutdown_event("agent-studio-worker")
    interval = max(0.5, float(settings.agent_studio_worker_interval_seconds))
    while not shutdown.is_set():
        try:
            async with SessionLocal() as session:
                processed = (
                    await worker.run_once(session)
                    if settings.agent_studio_worker_enabled
                    else False
                )
            if not processed:
                await sleep_or_shutdown(shutdown, interval)
        except Exception:  # noqa: BLE001
            log.exception("Agent Studio worker loop failed")
            await sleep_or_shutdown(shutdown, interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
