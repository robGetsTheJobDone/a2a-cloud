from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import issue_invocation_cp_credential, issue_token
from .config import settings
from .db import SessionLocal, init_models
from .deployments import ACTIVE_DEPLOY_STATUSES, utcnow
from .gitea import repo_exists, repo_web_url
from .models import (
    Agent,
    AgentCodeEditorOptIn,
    AgentDeployment,
    GrantAudit,
    User,
    WorkJob,
)
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown
from .work_ledger import append_event, complete_job, create_job, fail_job

TEMPLATE_UPDATE_JOB_KIND = "agent.template_update"
TEMPLATE_UPDATE_JOB_QUEUE = "template_updates"
TEMPLATE_UPDATE_IDEMPOTENCY_SCOPE = "agent_template_updates"
TEMPLATE_UPDATE_WORKER_NAME = "template_update_worker"
TEMPLATE_UPDATE_ACTIVE_RETRY_SECONDS = 30

_EXTERNAL_IMAGE_PREFIX = "external-a2a:"

log = logging.getLogger(__name__)

TemplateUpdateRunner = Callable[
    [AsyncSession, User, Agent, WorkJob, str, str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


class TemplateUpdateError(RuntimeError):
    """Raised when a template update request cannot be executed."""


class TemplateUpdateBlocked(RuntimeError):
    """Raised when a template update should retry later."""


async def enqueue_template_update_job(
    session: AsyncSession,
    agent: Agent,
    *,
    lineage: Mapping[str, Any],
) -> WorkJob:
    clean_lineage = _clean_lineage(lineage)
    policy = _update_policy(clean_lineage)
    template_ref = _template_ref(clean_lineage)
    template_version = _optional_str(clean_lineage.get("template_version")) or ""
    source_revision = _optional_str(clean_lineage.get("source_revision")) or ""
    instance_version = _optional_str(clean_lineage.get("instance_version")) or ""
    payload = {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "repo_owner": agent.gitea_owner,
        "repo_url": repo_web_url(agent.name, owner=agent.gitea_owner),
        "template_lineage": clean_lineage,
        "requested_policy": policy,
    }
    return await create_job(
        session,
        user_id=agent.owner_id,
        kind=TEMPLATE_UPDATE_JOB_KIND,
        payload=payload,
        title=f"Update {agent.name} from template",
        metadata={
            "provider": "a2a-pack",
            "trigger": "template_update",
            "agent_name": agent.name,
            "template_ref": template_ref or None,
            "template_version": template_version or None,
            "source_revision": source_revision or None,
            "update_policy": policy,
        },
        queue=TEMPLATE_UPDATE_JOB_QUEUE,
        source_type="agent_card",
        source_id=agent.name,
        subject_type="agent",
        subject_id=str(agent.id),
        worker_type="template_update",
        worker_name=TEMPLATE_UPDATE_WORKER_NAME,
        idempotency_key=(
            "agent-template-update:"
            f"{agent.id}:{template_ref}:{template_version}:{source_revision}:{instance_version}"
        ),
        idempotency_scope=TEMPLATE_UPDATE_IDEMPOTENCY_SCOPE,
        max_attempts=1,
    )


class TemplateUpdateWorker:
    def __init__(self, call_runner: TemplateUpdateRunner | None = None) -> None:
        self._call_runner = call_runner

    async def run_once(self, session: AsyncSession) -> bool:
        job = await self._next_job(session)
        if job is None:
            return False
        try:
            result = await execute_template_update(
                session,
                job,
                call_runner=self._call_runner,
            )
            await complete_job(
                session,
                job,
                result=result,
                summary=result.get("summary", "Template update completed"),
                status="complete",
                event_type="template_update_completed",
                commit=True,
            )
            return True
        except TemplateUpdateBlocked as exc:
            await self._defer_job(session, job, str(exc))
            return True
        except Exception as exc:  # noqa: BLE001
            await fail_job(
                session,
                job,
                error=str(exc),
                summary="Template update failed",
                status="error",
                event_type="template_update_failed",
                commit=True,
            )
            return True

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        job = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "queued")
                .where(WorkJob.kind == TEMPLATE_UPDATE_JOB_KIND)
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
        timeout = max(60, int(settings.template_update_timeout_seconds))
        job.status = "running"
        job.attempt = (job.attempt or 0) + 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.leased_until = now + timedelta(seconds=timeout + 300)
        await session.commit()
        await session.refresh(job)
        return job

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        now = utcnow()
        result = await session.execute(
            update(WorkJob)
            .where(WorkJob.kind == TEMPLATE_UPDATE_JOB_KIND)
            .where(WorkJob.status == "running")
            .where(WorkJob.leased_until.is_not(None))
            .where(WorkJob.leased_until <= now)
            .values(
                status="queued",
                summary="Template update lease expired; requeued",
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

    async def _defer_job(
        self,
        session: AsyncSession,
        job: WorkJob,
        reason: str,
    ) -> None:
        now = utcnow()
        retry_at = now + timedelta(seconds=TEMPLATE_UPDATE_ACTIVE_RETRY_SECONDS)
        job.status = "queued"
        job.summary = "Template update waiting for active deployment"
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
            event_type="template_update_deferred",
            payload={"reason": reason, "retry_at": retry_at.isoformat()},
            message=job.summary,
            status="queued",
            commit=False,
        )
        await session.commit()


async def execute_template_update(
    session: AsyncSession,
    job: WorkJob,
    *,
    call_runner: TemplateUpdateRunner | None = None,
) -> dict[str, Any]:
    payload = job.input_payload if isinstance(job.input_payload, dict) else {}
    agent = await _agent_from_job(session, payload)
    user = await session.get(User, agent.owner_id)
    if user is None:
        raise TemplateUpdateError("agent owner no longer exists")
    lineage = _lineage_from_job_or_agent(payload, agent)
    policy = _update_policy(lineage)
    if policy == "none":
        raise TemplateUpdateError("agent template_lineage update_policy is none")

    if policy == "notify":
        return {
            "summary": f"Template update available for {agent.name}",
            "agent_id": agent.id,
            "agent_name": agent.name,
            "template_lineage": lineage,
            "update_policy": policy,
            "mutated": False,
        }

    active = await _active_deployment(session, agent)
    if active is not None:
        raise TemplateUpdateBlocked(
            f"agent {agent.name} already has active deployment {active.deploy_id}"
        )

    target_agent, skill = _migration_target(lineage)
    if target_agent == settings.code_editor_agent_name and skill == "turn":
        await _ensure_code_editor_opt_in(session, agent, user, job)

    args = _migration_args(
        agent=agent,
        lineage=lineage,
        policy=policy,
        target_agent=target_agent,
        skill=skill,
        job=job,
    )
    await append_event(
        session,
        job,
        event_type="template_update_invoking",
        payload={
            "target_agent": target_agent,
            "skill": skill,
            "agent_name": agent.name,
            "update_policy": policy,
            "template_ref": _template_ref(lineage),
        },
        message=f"Invoking {target_agent}.{skill} for template update",
        status="running",
        commit=True,
    )
    runner = call_runner or _call_migration_agent
    result = await runner(session, user, agent, job, target_agent, skill, args)
    if not isinstance(result, dict):
        result = {"result": result}
    if result.get("ok") is False:
        raise TemplateUpdateError(_handoff_error(result))
    return {
        "summary": _summary(result, default=f"Template update invoked for {agent.name}"),
        "agent_id": agent.id,
        "agent_name": agent.name,
        "target_agent": target_agent,
        "skill": skill,
        "update_policy": policy,
        "template_lineage": lineage,
        "migration_args": _safe_args_summary(args),
        "migration_result": result,
        "mutated": True,
    }


async def _call_migration_agent(
    session: AsyncSession,
    user: User,
    agent: Agent,
    job: WorkJob,
    target_agent: str,
    skill: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    from main_agent import OrchestratorContext
    from main_agent.tools.handoff import build_handoff_tools

    # The orchestrator's own in-process identity: its file/discovery tools call
    # back into this control plane with it, and it is never written into the
    # hand-off body (``_template_update_hooks.get_cp_jwt`` mints the scoped
    # invocation credential for that). Still bounded by the one call it serves.
    token = issue_token(
        user.id, ttl_seconds=max(60, int(settings.template_update_timeout_seconds)) + 30
    )
    hooks = _template_update_hooks(session, user=user, agent=agent, job=job)
    ctx = OrchestratorContext.for_user(
        user_id=user.id,
        jwt=token,
        hooks=hooks,
        policy_controls={
            "max_agent_calls_per_run": 1,
            "only_approved_agents": True,
            "approved_agents": [target_agent],
        },
    )
    call_agent = next(
        (tool for tool in build_handoff_tools(ctx) if getattr(tool, "name", "") == "call_agent"),
        None,
    )
    if call_agent is None:
        raise TemplateUpdateError("call_agent tool unavailable")
    raw = await asyncio.wait_for(
        call_agent.ainvoke({
            "name": target_agent,
            "skill": skill,
            "args_json": json.dumps(args, separators=(",", ":"), ensure_ascii=False),
        }),
        timeout=max(60, int(settings.template_update_timeout_seconds)),
    )
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TemplateUpdateError(f"migration agent returned invalid JSON: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return raw if isinstance(raw, dict) else {"result": raw}


def _template_update_hooks(
    session: AsyncSession,
    *,
    user: User,
    agent: Agent,
    job: WorkJob,
):
    from main_agent import PlatformHooks
    from .routes.llm_creds import get_creds_for_user

    db_lock = asyncio.Lock()

    async def emit(event: dict[str, Any]) -> None:
        async with db_lock:
            try:
                await append_event(
                    session,
                    job,
                    event_type=str(event.get("type") or "template_update_event"),
                    payload=event,
                    message=_event_summary(event),
                    status=_event_status(event),
                    user_id=user.id,
                    source_type="agent_template_update",
                    source_id=agent.name,
                )
            except Exception:  # noqa: BLE001
                await session.rollback()

    async def audit_grant(
        payload: dict[str, Any],
        decision: str,
        decided_by: str,
        reason: str | None,
        parent: str | None,
    ) -> None:
        async with db_lock:
            try:
                session.add(GrantAudit(
                    grant_id=payload["grant_id"],
                    parent_grant_id=parent,
                    issuer=payload["issuer"],
                    audience=payload["audience"],
                    bucket=payload["bucket"],
                    mode=payload["mode"],
                    allow_patterns=list(payload.get("allow_patterns") or []),
                    deny_patterns=list(payload.get("deny_patterns") or []),
                    outputs_prefix=payload.get("outputs_prefix"),
                    ttl_seconds=int(
                        payload.get("expires_at", 0) - payload.get("issued_at", 0)
                    ),
                    user_id=user.id,
                    decision=decision,
                    decided_by=decided_by,
                    reason=reason,
                ))
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()

    async def get_agent_card(agent_name: str) -> dict[str, Any] | None:
        row = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one_or_none()
        return row.card if row is not None and isinstance(row.card, dict) else None

    async def get_user_llm_creds() -> dict[str, Any] | None:
        return await get_creds_for_user(user.id, session)

    async def get_cp_jwt(agent_name: str) -> dict[str, str]:
        # Never the orchestrator's own platform session: this value is written
        # into the hand-off body of a process we do not control.
        return {
            "jwt": issue_invocation_cp_credential(user.id, agent=agent_name),
            "url": settings.public_cp_url,
        }

    return PlatformHooks(
        emit=emit,
        approval_mode=False,
        audit_grant=audit_grant,
        get_agent_card=get_agent_card,
        get_user_llm_creds=get_user_llm_creds,
        get_cp_jwt=get_cp_jwt,
    )


async def _ensure_code_editor_opt_in(
    session: AsyncSession,
    agent: Agent,
    user: User,
    job: WorkJob,
) -> AgentCodeEditorOptIn:
    if not settings.code_editor_runtime_enabled:
        raise TemplateUpdateError("code editor runtime is disabled")
    if agent.image.startswith(_EXTERNAL_IMAGE_PREFIX):
        raise TemplateUpdateError("template updates require a managed source repo")
    try:
        exists = repo_exists(agent.name, owner=agent.gitea_owner)
    except Exception as exc:  # noqa: BLE001
        raise TemplateUpdateError(f"could not reach agent source repo: {exc}") from exc
    if not exists:
        raise TemplateUpdateError("template updates require an existing managed source repo")
    opt_in = (
        await session.execute(
            select(AgentCodeEditorOptIn).where(AgentCodeEditorOptIn.agent_id == agent.id)
        )
    ).scalar_one_or_none()
    if opt_in is None:
        opt_in = AgentCodeEditorOptIn(
            agent_id=agent.id,
            user_id=user.id,
            status="enabled",
            shared_agent_name=settings.code_editor_agent_name,
        )
        session.add(opt_in)
    opt_in.status = "enabled"
    opt_in.shared_agent_name = settings.code_editor_agent_name
    opt_in.workspace_key = _code_editor_workspace_key(agent)
    opt_in.last_error = None
    metadata = dict(opt_in.metadata_json or {})
    metadata["template_update"] = {
        "job_id": job.job_id,
        "enabled_at": utcnow().isoformat(),
    }
    opt_in.metadata_json = metadata
    await session.commit()
    await session.refresh(opt_in)
    return opt_in


async def _agent_from_job(session: AsyncSession, payload: Mapping[str, Any]) -> Agent:
    agent_id = int(payload.get("agent_id") or 0)
    agent_name = _optional_str(payload.get("agent_name"))
    agent = await session.get(Agent, agent_id) if agent_id else None
    if agent is None and agent_name:
        agent = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one_or_none()
    if agent is None:
        raise TemplateUpdateError("agent no longer exists")
    if agent_name and agent.name != agent_name:
        raise TemplateUpdateError("template update job agent_name no longer matches agent")
    return agent


def _lineage_from_job_or_agent(
    payload: Mapping[str, Any],
    agent: Agent,
) -> dict[str, Any]:
    lineage = payload.get("template_lineage")
    if not isinstance(lineage, Mapping):
        card = agent.card if isinstance(agent.card, dict) else {}
        lineage = card.get("template_lineage") if isinstance(card, dict) else None
    if not isinstance(lineage, Mapping):
        raise TemplateUpdateError("agent card does not declare template_lineage")
    return _clean_lineage(lineage)


def _clean_lineage(lineage: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in lineage.items() if value is not None}


def _update_policy(lineage: Mapping[str, Any]) -> str:
    return str(lineage.get("update_policy") or "none").strip().lower() or "none"


def _template_ref(lineage: Mapping[str, Any]) -> str:
    return str(lineage.get("template_ref") or lineage.get("source_agent") or "").strip()


def _migration_target(lineage: Mapping[str, Any]) -> tuple[str, str]:
    target_agent = _optional_str(lineage.get("migration_agent"))
    if target_agent is None:
        target_agent = _optional_str(lineage.get("source_agent"))
    skill = _optional_str(lineage.get("migration_skill")) or "migrate"
    if target_agent is None and skill == "turn":
        target_agent = settings.code_editor_agent_name
    if target_agent is None:
        raise TemplateUpdateError(
            "template_lineage must include source_agent or migration_agent"
        )
    return target_agent, skill


def _migration_args(
    *,
    agent: Agent,
    lineage: Mapping[str, Any],
    policy: str,
    target_agent: str,
    skill: str,
    job: WorkJob,
) -> dict[str, Any]:
    if target_agent == settings.code_editor_agent_name and skill == "turn":
        return {
            "agent_name": agent.name,
            "ref": "main",
            "session_name": f"template-update-{agent.name}-{job.job_id[:8]}",
            "max_turns": int(settings.template_update_max_turns),
            "permission_mode": "default",
            "dry_run": False,
            "push_on_failure": False,
            "prompt": _code_editor_prompt(agent=agent, lineage=lineage, policy=policy),
        }
    return {
        "agent_name": agent.name,
        "ref": "main",
        "repo_owner": agent.gitea_owner,
        "repo_url": repo_web_url(agent.name, owner=agent.gitea_owner),
        "update_policy": policy,
        "template_lineage": dict(lineage),
    }


def _code_editor_prompt(
    *,
    agent: Agent,
    lineage: Mapping[str, Any],
    policy: str,
) -> str:
    fields = {
        "template_ref": lineage.get("template_ref"),
        "template_version": lineage.get("template_version"),
        "template_digest": lineage.get("template_digest"),
        "source_agent": lineage.get("source_agent"),
        "source_agent_version": lineage.get("source_agent_version"),
        "source_revision": lineage.get("source_revision"),
        "instance_version": lineage.get("instance_version"),
        "update_policy": policy,
    }
    lineage_lines = "\n".join(
        f"- {key}: {value}" for key, value in fields.items() if value
    )
    return (
        f"Complete the template lineage update for managed A2A agent {agent.name}.\n\n"
        f"Template lineage:\n{lineage_lines or '- unspecified'}\n\n"
        "Use the target repo on main. Apply the template/runtime/source changes "
        "needed to move this instance forward while preserving user-specific "
        "behavior, public API compatibility, auth, domains, consumer setup, and "
        "secrets handling. Do not persist or print secrets. If the repo already "
        "contains the update, do not make an empty commit; report no_changes with "
        "evidence. Otherwise make real edits, run focused tests/lint/build commands "
        "that exist, commit, and push to main. Return a concise summary with changed "
        "files, tests run, and final head SHA if available."
    )


async def _active_deployment(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    latest_live = (
        await session.execute(
            select(AgentDeployment)
            .where(AgentDeployment.agent_id == agent.id)
            .where(AgentDeployment.status == "live")
            .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    stmt = (
        select(AgentDeployment)
        .where(AgentDeployment.agent_id == agent.id)
        .where(AgentDeployment.status.in_(ACTIVE_DEPLOY_STATUSES))
    )
    if latest_live is not None:
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


def _code_editor_workspace_key(agent: Agent) -> str:
    owner = agent.gitea_owner or "default"
    return f"{owner}/{agent.name}"


def _safe_args_summary(args: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in args.items()
        if key not in {"prompt", "consumer_secrets", "llm_creds"}
    }


def _event_status(event: Mapping[str, Any]) -> str | None:
    if event.get("ok") is False:
        return "error"
    if event.get("type") in {"handoff_complete", "dag_complete"}:
        return "complete"
    return "running"


def _event_summary(event: Mapping[str, Any]) -> str:
    for key in ("summary", "message", "error"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1000]
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        for key in ("message", "summary", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:1000]
    return str(event.get("type") or "template_update_event")


def _summary(result: Mapping[str, Any], *, default: str) -> str:
    for key in ("summary", "content", "error"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1000]
    nested = result.get("result")
    if isinstance(nested, Mapping):
        return _summary(nested, default=default)
    return default


def _handoff_error(result: Mapping[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    nested = result.get("result")
    if isinstance(nested, Mapping):
        nested_error = nested.get("error")
        if isinstance(nested_error, str) and nested_error.strip():
            return nested_error.strip()
    return "migration agent failed"


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


async def run_forever() -> None:
    await _init_models_with_retry()
    worker = TemplateUpdateWorker()
    from .self_healing import SelfHealingWorker

    self_healing_worker = SelfHealingWorker()
    interval = max(
        1.0,
        min(
            float(settings.template_update_worker_interval_seconds),
            float(settings.self_healing_worker_interval_seconds),
        ),
    )
    shutdown = install_shutdown_event("template-update-worker")
    while not shutdown.is_set():
        try:
            async with SessionLocal() as session:
                processed = (
                    await worker.run_once(session)
                    if settings.template_update_worker_enabled
                    else False
                )
            if not processed and settings.self_healing_enabled:
                async with SessionLocal() as session:
                    processed = await self_healing_worker.run_once(session)
            if not processed:
                await sleep_or_shutdown(shutdown, interval)
        except Exception:  # noqa: BLE001
            log.exception("Template update worker loop failed")
            await sleep_or_shutdown(shutdown, interval)


async def _init_models_with_retry() -> None:
    interval = max(1.0, float(settings.template_update_worker_interval_seconds))
    while True:
        try:
            await init_models()
            return
        except Exception:  # noqa: BLE001
            log.exception("Template update worker model init failed; retrying")
            await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
