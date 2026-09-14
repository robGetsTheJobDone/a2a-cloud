from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .agent_review import enqueue_deploy_review
from .config import settings
from .card_cache import invalidate_agent_card
from .database_bindings import reconcile_agent_database_bindings
from .database_resources import read_agent_database_declarations_from_tarball_bytes
from .db import SessionLocal, init_models
from .deployments import (
    ACTIVE_DEPLOY_STATUSES,
    TRANSIENT_AGENT_STATUSES,
    create_deployment,
    fail_deployment,
    record_deployment_event,
    sync_deployment_verification,
    utcnow,
)
from .gitea import GITEA_USER, ensure_repo, repo_head_sha, source_tarball_from_repo
from .models import Agent, AgentDeployment, User, WorkJob
from .runtime_upgrade import _read_entrypoint_from_repo
from .scaffold import commit_and_push_runtime_from_repo
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown
from .work_ledger import append_event, complete_job, create_job, fail_job

SOURCE_PUSH_JOB_KIND = "agent.source_push_deploy"
SOURCE_PUSH_JOB_QUEUE = "deployments"
SOURCE_PUSH_IDEMPOTENCY_SCOPE = "source_push_deployments"
SOURCE_PUSH_ACTIVE_RETRY_SECONDS = 30
SOURCE_PUSH_ACTIVE_REFRESH_SECONDS = 60

log = logging.getLogger(__name__)
AVAILABILITY_ALWAYS_ON = "always_on"


class SourcePushDeployBlocked(RuntimeError):
    """Raised when a source push cannot start without overlapping a deploy."""


def source_push_idempotency_key(owner: str, repo: str, source_sha: str) -> str:
    return f"gitea-source-push:{owner}:{repo}:{source_sha}"


async def find_agent_for_source_repo(
    session: AsyncSession,
    *,
    owner: str,
    repo: str,
) -> Agent | None:
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return None

    owner_filter = Agent.gitea_owner == owner
    if owner == GITEA_USER:
        owner_filter = or_(Agent.gitea_owner == owner, Agent.gitea_owner.is_(None))

    return (
        await session.execute(
            select(Agent).where(Agent.name == repo).where(owner_filter)
        )
    ).scalar_one_or_none()


async def enqueue_source_push_deploy_job(
    session: AsyncSession,
    agent: Agent,
    *,
    owner: str,
    repo: str,
    source_sha: str,
    changed_paths: list[str],
    delivery_id: str | None,
    ref: str,
    metadata: Mapping[str, Any] | None = None,
) -> WorkJob:
    payload = {
        "owner": owner,
        "repo": repo,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "source_sha": source_sha,
        "changed_paths": changed_paths,
        "delivery_id": delivery_id,
        "ref": ref,
    }
    request_hash = _stable_request_hash(
        {
            "kind": SOURCE_PUSH_JOB_KIND,
            "owner": owner,
            "repo": repo,
            "source_sha": source_sha,
            "changed_paths": changed_paths,
            "ref": ref,
        }
    )
    job = await create_job(
        session,
        user_id=agent.owner_id,
        kind=SOURCE_PUSH_JOB_KIND,
        payload=payload,
        title=f"Deploy {agent.name} from source push",
        metadata={
            "provider": "gitea",
            "trigger": "source_push",
            "owner": owner,
            "repo": repo,
            "source_sha": source_sha,
            **dict(metadata or {}),
        },
        queue=SOURCE_PUSH_JOB_QUEUE,
        source_type="gitea_repo",
        source_id=f"{owner}/{repo}",
        subject_type="agent",
        subject_id=str(agent.id),
        worker_type="deploy",
        worker_name="source_push_deploy_worker",
        idempotency_key=source_push_idempotency_key(owner, repo, source_sha),
        idempotency_scope=SOURCE_PUSH_IDEMPOTENCY_SCOPE,
        request_hash=request_hash,
        max_attempts=3,
    )
    debounce_seconds = max(0.0, float(settings.source_push_deploy_debounce_seconds))
    if debounce_seconds > 0:
        job.queued_at = utcnow() + timedelta(seconds=debounce_seconds)
        job.summary = "Source push deployment waiting for repository changes to settle"
        await session.commit()
        await session.refresh(job)
    return job


def _stable_request_hash(value: Mapping[str, Any]) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _agent_allows_always_on_update(agent: Agent) -> bool:
    card = agent.card if isinstance(agent.card, dict) else {}
    runtime = card.get("runtime") if isinstance(card, dict) else None
    return (
        isinstance(runtime, dict)
        and str(runtime.get("availability") or "").strip().lower()
        == AVAILABILITY_ALWAYS_ON
    )


async def deploy_source_push(
    session: AsyncSession,
    app: Any,
    *,
    owner: str,
    repo: str,
    source_sha: str,
    changed_paths: list[str],
    delivery_id: str | None,
    ref: str = "refs/heads/main",
    trigger: str = "source_push",
) -> dict[str, Any]:
    from .routes.agents import _canonical_url, _ensure_runtime_repo, _public_repo_url

    agent = await find_agent_for_source_repo(session, owner=owner, repo=repo)
    if agent is None:
        raise RuntimeError(f"unknown managed source repo {owner}/{repo}")

    observed_head = repo_head_sha(repo, owner=owner)
    if observed_head is None:
        raise RuntimeError(f"source repo {owner}/{repo} has no main branch")
    is_source_push = trigger == "source_push"
    if observed_head != source_sha:
        return {
            "summary": (
                f"Skipped stale source deploy for {agent.name}; "
                f"main is now {observed_head[:12]}"
            ),
            "agent_id": agent.id,
            "agent_name": agent.name,
            "source_sha": source_sha,
            "observed_main_head_sha": observed_head,
            "skipped": True,
            "reason": "stale_source_push",
        }

    active = await _refreshed_active_deployment(session, agent)
    if active is not None:
        if active.trigger == trigger and active.head_sha == source_sha:
            return {
                "summary": f"Source deploy already active for {agent.name}",
                "agent_id": agent.id,
                "agent_name": agent.name,
                "deploy_id": active.deploy_id,
                "source_sha": source_sha,
                "status": active.status,
            }
        raise SourcePushDeployBlocked(
            f"agent {agent.name} already has active deployment {active.deploy_id}"
        )
    latest = await _latest_deployment(session, agent)
    if latest is not None and latest.status == "live" and latest.head_sha == source_sha:
        return {
            "summary": f"Source SHA already deployed for {agent.name}",
            "agent_id": agent.id,
            "agent_name": agent.name,
            "deploy_id": latest.deploy_id,
            "source_sha": source_sha,
            "status": latest.status,
            "already_deployed": True,
        }

    expected_url = _canonical_url(agent.name) if agent.public else agent.url
    source_public_url = _public_repo_url(agent.name, owner=agent.gitea_owner)
    expected_image = settings.agent_image(agent.name, source_sha)
    deployment: AgentDeployment | None = None
    try:
        deployment = await create_deployment(
            session,
            agent,
            trigger=trigger,
            status="queued",
            source_repo_url=source_public_url,
            head_sha=source_sha,
            image=expected_image,
            agent_url=expected_url,
        )
        await invalidate_agent_card(agent.name)
        await record_deployment_event(
            session,
            deployment,
            stage="source",
            status="passed",
            message=(
                "Received Gitea push to managed source repo."
                if is_source_push
                else "Received explicit deploy request for managed source repo."
            ),
            data={
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "source_sha": source_sha,
                "changed_paths": changed_paths,
                "delivery_id": delivery_id,
                "repo_url": source_public_url,
                "trigger": trigger,
            },
        )

        source_tarball, exported_head = await asyncio.to_thread(
            source_tarball_from_repo,
            agent.name,
            owner=agent.gitea_owner,
        )
        if exported_head != source_sha:
            raise RuntimeError(
                "managed source changed while reconciling platform resources"
            )
        database_declarations = read_agent_database_declarations_from_tarball_bytes(
            source_tarball
        )
        owner_user = await session.get(User, agent.owner_id)
        if owner_user is None:
            raise RuntimeError(f"owner for agent {agent.name} no longer exists")
        database_bindings = await reconcile_agent_database_bindings(
            session,
            agent=agent,
            user=owner_user,
            declarations=database_declarations,
        )
        if database_declarations or database_bindings.removed:
            await record_deployment_event(
                session,
                deployment,
                stage="database",
                status="queued",
                message="Agent database declarations were reconciled from managed source.",
                data={
                    "requested": database_bindings.requested,
                    "removed": database_bindings.removed,
                    "source_sha": source_sha,
                },
            )

        source_repo_url, _ = ensure_repo(
            agent.name,
            agent.description,
            owner=agent.gitea_owner,
            public=agent.public,
        )
        entrypoint = _read_entrypoint_from_repo(source_repo_url, source_sha)
        runtime_push_url, runtime_internal_url = _ensure_runtime_repo(
            agent.name,
            agent.description,
        )
        runtime_sha = commit_and_push_runtime_from_repo(
            name=agent.name,
            entrypoint=entrypoint,
            source_repo_url=source_repo_url,
            source_sha=source_sha,
            push_url=runtime_push_url,
            image_tag=source_sha,
            allow_always_on=_agent_allows_always_on_update(agent),
        )

        deployment.status = "building"
        deployment.image = expected_image
        deployment.agent_url = expected_url
        agent.status = "building"
        agent.image = deployment.image or agent.image
        if agent.public:
            agent.url = expected_url
        await session.commit()

        await record_deployment_event(
            session,
            deployment,
            stage="runtime",
            status="passed",
            message=f"Hidden runtime repo restamped for source SHA {source_sha[:12]}.",
            data={
                "runtime_head_sha": runtime_sha,
                "runtime_repo_url": runtime_internal_url,
                "source_sha": source_sha,
                "observed_main_head_sha": observed_head,
            },
        )
        await record_deployment_event(
            session,
            deployment,
            stage="build",
            status="running",
            message=(
                "Gitea Actions is building image for source SHA "
                f"{source_sha[:12]}."
            ),
            data={"image": deployment.image, "source_sha": source_sha},
        )
        await record_deployment_event(
            session,
            deployment,
            stage="argo",
            status="running",
            message="ArgoCD will reconcile after the runtime manifest bump.",
            data={"url": expected_url, "repo_url": runtime_internal_url},
        )
        review_id = enqueue_deploy_review(
            _review_app(app),
            agent_id=agent.id,
            agent_name=agent.name,
            ref=source_sha,
            owner=agent.gitea_owner,
            user_id=agent.owner_id,
            deploy_id=deployment.deploy_id,
        )
        return {
            "summary": f"Queued source deployment for {agent.name}",
            "agent_id": agent.id,
            "agent_name": agent.name,
            "deploy_id": deployment.deploy_id,
            "source_sha": source_sha,
            "runtime_sha": runtime_sha,
            "review_id": review_id,
        }
    except Exception as exc:
        await fail_deployment(
            session,
            deployment,
            stage=trigger,
            error=f"source deploy failed: {exc}",
        )
        if agent.status in TRANSIENT_AGENT_STATUSES:
            agent.status = "failed"
            await session.commit()
        raise


async def _active_deployment(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    latest_live = await _latest_live_deployment(session, agent)
    stmt = (
        select(AgentDeployment)
        .where(AgentDeployment.agent_id == agent.id)
        .where(AgentDeployment.status.in_(ACTIVE_DEPLOY_STATUSES))
    )
    if latest_live is not None:
        # A later live deployment proves older active rows were superseded.
        stmt = stmt.where(
            or_(
                AgentDeployment.created_at > latest_live.created_at,
                and_(
                    AgentDeployment.created_at == latest_live.created_at,
                    AgentDeployment.id > latest_live.id,
                ),
            )
        )
    return (
        await session.execute(
            stmt.order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_deployment(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    return (
        await session.execute(
            select(AgentDeployment)
            .where(AgentDeployment.agent_id == agent.id)
            .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_live_deployment(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    return (
        await session.execute(
            select(AgentDeployment)
            .where(AgentDeployment.agent_id == agent.id)
            .where(AgentDeployment.status == "live")
            .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _refreshed_active_deployment(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    active = await _active_deployment(session, agent)
    if active is None or not _should_refresh_active_deployment(active):
        return active

    await sync_deployment_verification(session, agent, active)
    return await _active_deployment(session, agent)


def _should_refresh_active_deployment(deploy: AgentDeployment) -> bool:
    return _deployment_age_seconds(deploy) >= SOURCE_PUSH_ACTIVE_REFRESH_SECONDS


def _deployment_age_seconds(deploy: AgentDeployment) -> float:
    started = deploy.started_at or deploy.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (utcnow() - started).total_seconds()


def _review_app(app: Any) -> Any:
    if app is not None:
        return app
    return SimpleNamespace(state=SimpleNamespace())


class SourcePushDeployWorker:
    def __init__(self, app: Any | None = None) -> None:
        self.app = app

    async def run_once(self, session: AsyncSession) -> bool:
        job = await self._next_job(session)
        if job is None:
            return await self._refresh_due_active_deployment(session)
        job_pk = job.id
        try:
            result = await self.deploy_job(session, job)
            await complete_job(
                session,
                job,
                result=result,
                summary=result.get("summary", "Source push deployment queued"),
                status="complete",
                event_type="source_push_deploy_queued",
                commit=True,
            )
            return True
        except SourcePushDeployBlocked as exc:
            job = await self._reload_after_error(session, job_pk)
            if job is None:
                return True
            await self._defer_job(session, job, str(exc))
            return True
        except Exception as exc:  # noqa: BLE001
            job = await self._reload_after_error(session, job_pk)
            if job is None:
                log.exception("Source push deployment failed after job disappeared")
                return True
            await fail_job(
                session,
                job,
                error=str(exc),
                summary="Source push deployment failed",
                status="error",
                event_type="source_push_deploy_failed",
                commit=True,
            )
            return True

    async def deploy_job(
        self,
        session: AsyncSession,
        job: WorkJob,
    ) -> dict[str, Any]:
        payload = job.input_payload or {}
        return await deploy_source_push(
            session,
            self.app,
            owner=_required_str(payload, "owner"),
            repo=_required_str(payload, "repo"),
            source_sha=_required_str(payload, "source_sha"),
            changed_paths=_string_list(payload.get("changed_paths")),
            delivery_id=_optional_str(payload.get("delivery_id")),
            ref=_optional_str(payload.get("ref")) or "refs/heads/main",
        )

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        job = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "queued")
                .where(WorkJob.kind == SOURCE_PUSH_JOB_KIND)
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
        job.leased_until = now + timedelta(minutes=15)
        await session.commit()
        await session.refresh(job)
        return job

    async def _refresh_due_active_deployment(self, session: AsyncSession) -> bool:
        rows = (
            await session.execute(
                select(AgentDeployment, Agent)
                .join(Agent, Agent.id == AgentDeployment.agent_id)
                .where(AgentDeployment.trigger == "source_push")
                .where(AgentDeployment.status.in_(ACTIVE_DEPLOY_STATUSES))
                .order_by(AgentDeployment.started_at.asc(), AgentDeployment.id.asc())
                .limit(25)
            )
        ).all()
        for deploy, agent in rows:
            if not _should_refresh_active_deployment(deploy):
                continue
            await sync_deployment_verification(session, agent, deploy)
            return True
        return False

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        now = utcnow()
        result = await session.execute(
            update(WorkJob)
            .where(WorkJob.kind == SOURCE_PUSH_JOB_KIND)
            .where(WorkJob.status == "running")
            .where(WorkJob.leased_until.is_not(None))
            .where(WorkJob.leased_until <= now)
            .values(
                status="queued",
                summary="Source push deployment lease expired; requeued",
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

    async def _reload_after_error(
        self,
        session: AsyncSession,
        job_pk: int,
    ) -> WorkJob | None:
        await session.rollback()
        return await session.get(WorkJob, job_pk)

    async def _defer_job(
        self,
        session: AsyncSession,
        job: WorkJob,
        reason: str,
    ) -> None:
        now = utcnow()
        retry_at = now + timedelta(seconds=SOURCE_PUSH_ACTIVE_RETRY_SECONDS)
        job.status = "queued"
        job.summary = "Source push deployment waiting for active deployment"
        job.error = None
        job.error_payload = {}
        job.output_payload = {}
        job.queued_at = retry_at
        job.heartbeat_at = None
        job.leased_until = None
        job.completed_at = None
        job.updated_at = now
        await append_event(
            session,
            job,
            event_type="source_push_deploy_deferred",
            payload={"reason": reason, "retry_at": retry_at.isoformat()},
            message=job.summary,
            status="queued",
            commit=False,
        )
        await session.commit()


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = _optional_str(payload.get(key))
    if value is None:
        raise RuntimeError(f"source push job payload missing {key}")
    return value


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


async def run_forever() -> None:
    await init_models()
    worker = SourcePushDeployWorker()
    interval = max(1.0, float(settings.source_push_deploy_worker_interval_seconds))
    shutdown = install_shutdown_event("source-push-deploy-worker")
    while not shutdown.is_set():
        try:
            if not settings.source_push_deploy_worker_enabled:
                await sleep_or_shutdown(shutdown, interval)
                continue
            async with SessionLocal() as session:
                processed = await worker.run_once(session)
            if not processed:
                await sleep_or_shutdown(shutdown, interval)
        except Exception:  # noqa: BLE001
            log.exception("Source push deploy worker loop failed")
            await sleep_or_shutdown(shutdown, interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
