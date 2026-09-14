from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import SessionLocal, init_models
from .deployments import utcnow
from .gitea_provisioning import provision_gitea_workspace
from .models import Organization, OrganizationGiteaWorkspace, WorkJob
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown
from .work_ledger import complete_job, fail_job

log = logging.getLogger(__name__)


class GiteaProvisioner:
    async def run_once(self, session: AsyncSession) -> bool:
        job = await self._next_job(session)
        if job is None:
            return False
        try:
            result = await self.provision_workspace(session, job)
            await complete_job(
                session,
                job,
                result=result,
                summary=result.get("summary", "Gitea provisioning complete"),
                status="complete",
                event_type="gitea_provisioned",
                commit=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            await self._mark_failed(session, job, str(exc))
            return True

    async def provision_workspace(
        self,
        session: AsyncSession,
        job: WorkJob,
    ) -> dict[str, Any]:
        workspace_id = int((job.input_payload or {}).get("workspace_id") or 0)
        workspace = await session.get(OrganizationGiteaWorkspace, workspace_id)
        if workspace is None:
            raise RuntimeError("Gitea workspace row no longer exists")
        org = await session.get(Organization, workspace.organization_id)
        if org is None:
            raise RuntimeError("organization no longer exists")
        await provision_gitea_workspace(session, workspace, org)
        return {
            "summary": f"Provisioned Gitea org {workspace.gitea_org_name}",
            "organization_id": org.id,
            "organization_slug": org.slug,
            "workspace_id": workspace.id,
            "gitea_org_name": workspace.gitea_org_name,
            "org_url": workspace.org_url,
        }

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        job = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "queued")
                .where(WorkJob.kind == "gitea.workspace.provision")
                .where(or_(WorkJob.queued_at.is_(None), WorkJob.queued_at <= utcnow()))
                .order_by(WorkJob.created_at.asc(), WorkJob.id.asc())
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
        job.leased_until = now + timedelta(minutes=15)
        await session.commit()
        await session.refresh(job)
        return job

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        now = utcnow()
        result = await session.execute(
            update(WorkJob)
            .where(WorkJob.kind == "gitea.workspace.provision")
            .where(WorkJob.status == "running")
            .where(WorkJob.leased_until.is_not(None))
            .where(WorkJob.leased_until <= now)
            .values(
                status="queued",
                summary="Gitea provisioning lease expired; requeued",
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

    async def _mark_failed(
        self,
        session: AsyncSession,
        job: WorkJob,
        reason: str,
    ) -> None:
        workspace_id = int((job.input_payload or {}).get("workspace_id") or 0)
        workspace = await session.get(OrganizationGiteaWorkspace, workspace_id)
        if workspace is not None:
            workspace.status = "blocked"
            workspace.last_error = reason
        await fail_job(
            session,
            job,
            error=reason,
            summary="Gitea provisioning failed",
            status="blocked",
            event_type="gitea_provisioning_blocked",
            commit=True,
        )


async def run_forever() -> None:
    await init_models()
    provisioner = GiteaProvisioner()
    interval = max(1.0, float(settings.gitea_provisioner_interval_seconds))
    shutdown = install_shutdown_event("gitea-provisioner")
    while not shutdown.is_set():
        async with SessionLocal() as session:
            try:
                ran = await provisioner.run_once(session)
            except Exception:  # noqa: BLE001
                log.exception("Gitea provisioner loop failed")
                ran = False
        await sleep_or_shutdown(shutdown, 0.1 if ran else interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())
