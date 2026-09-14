"""Opt-in, bounded source self-repair for managed agents.

Runtime failures become durable work-ledger jobs only when the live Agent Card
advertises ``capabilities.self_healing.enabled``.  The repair worker delegates
the source mutation to the scoped code-editor agent, then waits for the exact
pushed commit to become a live deployment before declaring the repair healed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

try:
    from a2a_pack.runtime import SelfHealingPolicy
except ImportError:  # Control-plane deploys can lead the independently released SDK.
    class SelfHealingPolicy(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)

        enabled: bool = True
        consecutive_failures: int = Field(default=1, ge=1, le=10)
        window_seconds: int = Field(default=300, ge=30, le=3600)
        cooldown_seconds: int = Field(default=900, ge=60, le=86400)
        max_repairs_per_day: int = Field(default=3, ge=1, le=20)
        max_turns: int = Field(default=30, ge=1, le=100)
        deployment_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
        require_tests: bool = True

        @model_validator(mode="before")
        @classmethod
        def _concise_bool(cls, value: Any) -> Any:
            return {"enabled": value} if isinstance(value, bool) else value

        def public_payload(self) -> dict[str, Any]:
            return self.model_dump(mode="json")

from .config import settings
from .deployments import utcnow
from .gitea import GITEA_USER, repo_head_sha, repo_web_url, source_changed_paths_since
from .models import Agent, AgentDeployment, AgentReceipt, User, WorkEvent, WorkJob
from .source_push_deployments import enqueue_source_push_deploy_job
from .work_ledger import append_event, complete_job, create_job, fail_job

SELF_HEALING_JOB_KIND = "agent.self_healing"
SELF_HEALING_JOB_QUEUE = "self_healing"
SELF_HEALING_WORKER_NAME = "self_healing_worker"
SELF_HEALING_IDEMPOTENCY_SCOPE = "agent_self_healing"

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)
_TOKEN_RE = re.compile(
    r"(?i)(?:bearer\s+)?(?:sk-[A-Za-z0-9_-]{12,}|gh[opsu]_[A-Za-z0-9_]{12,}|"
    r"xox[a-z]-[A-Za-z0-9-]{12,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
_URL_CREDS_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)((?:['\"]?(?:authorization|api_key|apikey|credential|password|secret|token)"
    r"['\"]?)\s*[:=]\s*)['\"][^'\"]*['\"]"
)

log = logging.getLogger(__name__)

SelfHealingRunner = Callable[
    [AsyncSession, User, Agent, WorkJob, str, str, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


class SelfHealingError(RuntimeError):
    """A repair could not produce and verify a safe live source change."""


class SelfHealingBlocked(RuntimeError):
    """A repair must wait for another deployment to finish."""


def policy_from_card(card: Mapping[str, Any] | None) -> SelfHealingPolicy | None:
    """Return a validated enabled policy from an untrusted cached Agent Card."""
    if not isinstance(card, Mapping):
        return None
    capabilities = card.get("capabilities")
    if not isinstance(capabilities, Mapping) or "self_healing" not in capabilities:
        return None
    try:
        policy = SelfHealingPolicy.model_validate(capabilities.get("self_healing"))
    except (TypeError, ValidationError, ValueError):
        log.warning("ignoring invalid self_healing capability on agent card")
        return None
    return policy if policy.enabled else None


def public_policy_from_card(card: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = policy_from_card(card)
    if policy is None:
        return {"enabled": False}
    return policy.public_payload()


def actionable_runtime_error(error_type: str) -> bool:
    normalized = str(error_type or "").strip()
    if re.fullmatch(r"HTTP5\d\d", normalized, re.IGNORECASE):
        return True
    return normalized.lower() in {
        "agentruntimeerror",
        "internalerror",
        "skillinvocationerror",
        "runtimeerror",
    }


async def maybe_enqueue_runtime_failure(
    session: AsyncSession,
    *,
    agent_id: int,
    receipt_id: str,
    skill_name: str,
    error_type: str,
    error_preview: str,
    force_actionable: bool = False,
) -> WorkJob | None:
    """Create one deduplicated repair job if manifest policy permits it."""
    if not settings.self_healing_enabled:
        return None
    agent = await session.get(Agent, agent_id)
    if agent is None:
        return None
    policy = policy_from_card(agent.card if isinstance(agent.card, dict) else None)
    if policy is None or (not force_actionable and not actionable_runtime_error(error_type)):
        return None

    now = utcnow()
    window_start = now - timedelta(seconds=policy.window_seconds)
    recent_statuses = (
        await session.execute(
            select(AgentReceipt.status)
            .where(AgentReceipt.agent_id == agent.id)
            .where(AgentReceipt.ended_at >= window_start)
            .order_by(desc(AgentReceipt.ended_at), desc(AgentReceipt.receipt_id))
            .limit(policy.consecutive_failures)
        )
    ).scalars().all()
    failure_count = 0
    for status in recent_statuses:
        if status != "error":
            break
        failure_count += 1
    if failure_count < policy.consecutive_failures:
        return None

    active = (
        await session.execute(
            select(WorkJob)
            .where(WorkJob.kind == SELF_HEALING_JOB_KIND)
            .where(WorkJob.subject_type == "agent")
            .where(WorkJob.subject_id == str(agent.id))
            .where(WorkJob.status.in_(("queued", "running")))
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return None

    day_start = now - timedelta(days=1)
    recent_jobs = (
        await session.execute(
            select(WorkJob)
            .where(WorkJob.kind == SELF_HEALING_JOB_KIND)
            .where(WorkJob.subject_type == "agent")
            .where(WorkJob.subject_id == str(agent.id))
            .where(WorkJob.created_at >= day_start)
            .order_by(desc(WorkJob.created_at))
        )
    ).scalars().all()
    if len(recent_jobs) >= policy.max_repairs_per_day:
        return None
    if recent_jobs:
        latest_at = recent_jobs[0].created_at
        if latest_at.tzinfo is None:
            latest_at = latest_at.replace(tzinfo=timezone.utc)
        if latest_at > now - timedelta(seconds=policy.cooldown_seconds):
            return None

    safe_error = _safe_text(error_preview, limit=1600)
    fingerprint = hashlib.sha256(
        f"{agent.id}:{skill_name}:{error_type}:{safe_error}".encode("utf-8")
    ).hexdigest()[:24]
    payload = {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "repo_owner": agent.gitea_owner,
        "repo_url": repo_web_url(agent.name, owner=agent.gitea_owner),
        "trigger_receipt_id": receipt_id,
        "failure": {
            "skill_name": str(skill_name or "")[:128],
            "error_type": str(error_type or "RuntimeError")[:128],
            "error_preview": safe_error,
            "fingerprint": fingerprint,
            "observed_at": now.isoformat(),
            "window_failure_count": failure_count,
        },
        "policy": policy.public_payload(),
    }
    bucket = int(now.timestamp()) // policy.cooldown_seconds
    try:
        return await create_job(
            session,
            user_id=agent.owner_id,
            kind=SELF_HEALING_JOB_KIND,
            payload=payload,
            title=f"Self-heal {agent.name}",
            metadata={
                "provider": "a2a-pack",
                "trigger": "runtime_error",
                "agent_name": agent.name,
                "error_fingerprint": fingerprint,
                "trigger_receipt_id": receipt_id,
            },
            queue=SELF_HEALING_JOB_QUEUE,
            source_type="agent_receipt",
            source_id=receipt_id,
            subject_type="agent",
            subject_id=str(agent.id),
            worker_type="self_healing",
            worker_name=SELF_HEALING_WORKER_NAME,
            idempotency_key=f"self-heal:{agent.id}:{skill_name}:{error_type}:{bucket}",
            idempotency_scope=SELF_HEALING_IDEMPOTENCY_SCOPE,
            max_attempts=1,
        )
    except Exception as exc:  # A concurrent ingress replica may win the bucket.
        if exc.__class__.__name__ in {"IdempotencyConflict", "IntegrityError"}:
            await session.rollback()
            return None
        raise


class SelfHealingWorker:
    def __init__(self, call_runner: SelfHealingRunner | None = None) -> None:
        self._call_runner = call_runner

    async def run_once(self, session: AsyncSession) -> bool:
        if await self._reconcile_verified_failed_job(session):
            return True
        job = await self._next_job(session)
        if job is None:
            return False
        try:
            result = await execute_self_healing(
                session,
                job,
                call_runner=self._call_runner,
            )
            await complete_job(
                session,
                job,
                result=result,
                summary=result.get("summary", "Self-healing repair is live"),
                status="complete",
                event_type="self_healing_completed",
                commit=True,
            )
            return True
        except SelfHealingBlocked as exc:
            await self._defer_job(session, job, str(exc))
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("self-healing job %s failed", job.job_id)
            await fail_job(
                session,
                job,
                error=_safe_text(str(exc), limit=2000),
                summary="Self-healing repair failed",
                status="error",
                event_type="self_healing_failed",
                commit=True,
            )
            return True

    async def _reconcile_verified_failed_job(self, session: AsyncSession) -> bool:
        jobs = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "error")
                .where(WorkJob.kind == SELF_HEALING_JOB_KIND)
                .order_by(WorkJob.updated_at.desc(), WorkJob.id.desc())
                .limit(20)
            )
        ).scalars().all()
        for job in jobs:
            patch_event = (
                await session.execute(
                    select(WorkEvent)
                    .where(WorkEvent.job_id == job.job_id)
                    .where(WorkEvent.event_type == "self_healing_patch_pushed")
                    .order_by(WorkEvent.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if patch_event is None:
                continue
            patch = patch_event.payload if isinstance(patch_event.payload, dict) else {}
            after_head = _sha(patch.get("source_head_after"))
            payload = job.input_payload if isinstance(job.input_payload, dict) else {}
            agent_id = int(payload.get("agent_id") or 0)
            if after_head is None or not agent_id:
                continue
            deployment = (
                await session.execute(
                    select(AgentDeployment)
                    .where(AgentDeployment.agent_id == agent_id)
                    .where(AgentDeployment.head_sha == after_head)
                    .where(AgentDeployment.status == "live")
                    .where(
                        AgentDeployment.created_at
                        >= patch_event.created_at - timedelta(seconds=5)
                    )
                    .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if deployment is None:
                continue

            deployment_result = _deployment_result(deployment)
            result = {
                "summary": (
                    f"Self-healed {payload.get('agent_name') or deployment.agent_name} "
                    f"at {after_head[:12]}"
                ),
                "agent_id": agent_id,
                "agent_name": payload.get("agent_name") or deployment.agent_name,
                "trigger_receipt_id": payload.get("trigger_receipt_id"),
                "failure": payload.get("failure") or {},
                "source_head_before": patch.get("source_head_before"),
                "source_head_after": after_head,
                "changed_files": patch.get("changed_files") or [],
                "test_evidence": patch.get("test_evidence") or {},
                "deployment": deployment_result,
                "healed": True,
                "reconciled_after_worker_error": True,
            }
            await append_event(
                session,
                job,
                event_type="self_healing_deployment_verified",
                payload={
                    **deployment_result,
                    "reconciled_after_worker_error": True,
                },
                message=(
                    f"Recovered: exact repair commit is live in "
                    f"{deployment_result['deploy_id']}"
                ),
                status="complete",
                commit=False,
            )
            await complete_job(
                session,
                job,
                result=result,
                summary=result["summary"],
                status="complete",
                event_type="self_healing_completed",
                commit=True,
            )
            return True
        return False

    async def _next_job(self, session: AsyncSession) -> WorkJob | None:
        await self._release_expired_leases(session)
        job = (
            await session.execute(
                select(WorkJob)
                .where(WorkJob.status == "queued")
                .where(WorkJob.kind == SELF_HEALING_JOB_KIND)
                .where(or_(WorkJob.queued_at.is_(None), WorkJob.queued_at <= utcnow()))
                .order_by(WorkJob.priority.desc(), WorkJob.created_at.asc(), WorkJob.id.asc())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        payload = job.input_payload if isinstance(job.input_payload, dict) else {}
        policy = SelfHealingPolicy.model_validate(payload.get("policy") or {})
        now = utcnow()
        job.status = "running"
        job.attempt = (job.attempt or 0) + 1
        job.started_at = job.started_at or now
        job.heartbeat_at = now
        job.leased_until = now + timedelta(
            seconds=max(900, settings.template_update_timeout_seconds + policy.deployment_timeout_seconds + 600)
        )
        await session.commit()
        await session.refresh(job)
        return job

    async def _release_expired_leases(self, session: AsyncSession) -> None:
        now = utcnow()
        result = await session.execute(
            update(WorkJob)
            .where(WorkJob.kind == SELF_HEALING_JOB_KIND)
            .where(WorkJob.status == "running")
            .where(WorkJob.leased_until.is_not(None))
            .where(WorkJob.leased_until <= now)
            .values(
                status="queued",
                summary="Self-healing lease expired; requeued",
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

    async def _defer_job(self, session: AsyncSession, job: WorkJob, reason: str) -> None:
        now = utcnow()
        retry_at = now + timedelta(seconds=30)
        job.status = "queued"
        job.summary = "Self-healing is waiting for the active deployment"
        job.queued_at = retry_at
        job.heartbeat_at = None
        job.leased_until = None
        job.updated_at = now
        await append_event(
            session,
            job,
            event_type="self_healing_deferred",
            payload={"reason": _safe_text(reason), "retry_at": retry_at.isoformat()},
            message=job.summary,
            status="queued",
            commit=False,
        )
        await session.commit()


async def execute_self_healing(
    session: AsyncSession,
    job: WorkJob,
    *,
    call_runner: SelfHealingRunner | None = None,
) -> dict[str, Any]:
    from .template_updates import (
        _active_deployment,
        _call_migration_agent,
        _ensure_code_editor_opt_in,
    )

    payload = job.input_payload if isinstance(job.input_payload, dict) else {}
    agent = await _agent_from_payload(session, payload)
    agent_id = agent.id
    agent_name = agent.name
    owner = await session.get(User, agent.owner_id)
    if owner is None:
        raise SelfHealingError("agent owner no longer exists")
    policy = SelfHealingPolicy.model_validate(payload.get("policy") or {})
    active = await _active_deployment(session, agent)
    if active is not None:
        raise SelfHealingBlocked(
            f"agent {agent.name} already has active deployment {active.deploy_id}"
        )
    if agent.image.startswith("external-a2a:"):
        raise SelfHealingError("self-healing requires a managed source repository")

    before_head = await asyncio.to_thread(
        repo_head_sha,
        agent.name,
        owner=agent.gitea_owner,
    )
    await _ensure_code_editor_opt_in(session, agent, owner, job)
    failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
    await append_event(
        session,
        job,
        event_type="self_healing_diagnosing",
        payload={"failure": failure, "source_head_before": before_head},
        message=f"Diagnosing {failure.get('error_type') or 'runtime error'}",
        status="running",
        commit=True,
    )
    args = {
        "agent_name": agent.name,
        "ref": "main",
        "session_name": f"self-heal-{agent.name}-{job.job_id[:8]}",
        "max_turns": policy.max_turns,
        "permission_mode": "default",
        "output_format": "json",
        "dry_run": False,
        "push_on_failure": False,
        "timeout_seconds": min(3600, settings.template_update_timeout_seconds),
        "prompt": _repair_prompt(agent, failure, policy),
    }
    await append_event(
        session,
        job,
        event_type="self_healing_patching",
        payload={
            "editor": settings.code_editor_agent_name,
            "source_head_before": before_head,
            "max_turns": policy.max_turns,
            "require_tests": policy.require_tests,
        },
        message="Sending sanitized failure evidence to the scoped code editor",
        status="running",
        commit=True,
    )
    runner = call_runner or _call_migration_agent
    raw_result = await runner(
        session,
        owner,
        agent,
        job,
        settings.code_editor_agent_name,
        "turn",
        args,
    )
    editor = _editor_payload(raw_result)
    if editor.get("ok") is False or raw_result.get("ok") is False:
        raise SelfHealingError(_result_error(editor or raw_result))
    push = editor.get("push") if isinstance(editor.get("push"), Mapping) else {}
    after_head = _sha(push.get("head_sha"))
    if after_head is None:
        after_head = await asyncio.to_thread(
            repo_head_sha,
            agent.name,
            owner=agent.gitea_owner,
        )
    if not after_head or after_head == before_head:
        raise SelfHealingError("code editor produced no new source commit")
    changes = editor.get("changes") if isinstance(editor.get("changes"), Mapping) else {}
    changed_files = [
        str(item)[:500]
        for item in list(changes.get("files") or [])[:200]
        if str(item).strip()
    ]
    if before_head:
        try:
            diff_files = await asyncio.to_thread(
                source_changed_paths_since,
                agent.name,
                before_head,
                owner=agent.gitea_owner,
            )
        except Exception:  # noqa: BLE001 - audit enrichment must not lose the repair.
            log.warning(
                "could not inspect changed paths for self-healing job %s",
                job.job_id,
                exc_info=True,
            )
        else:
            changed_files = [str(item)[:500] for item in diff_files[:200]]
    test_evidence = _test_evidence(editor)
    if policy.require_tests and _tests_explicitly_failed(editor):
        raise SelfHealingError("repair tests reported a failure; source was not accepted as healed")
    await append_event(
        session,
        job,
        event_type="self_healing_patch_pushed",
        payload={
            "source_head_before": before_head,
            "source_head_after": after_head,
            "changed_files": changed_files,
            "test_evidence": test_evidence,
            "push": {
                "attempted": bool(push.get("attempted")),
                "ok": bool(push.get("ok", True)),
                "reason": _safe_text(push.get("reason"), limit=200),
            },
        },
        message=f"Repair commit {after_head[:12]} pushed; waiting for exact deployment",
        status="running",
        commit=True,
    )

    repo_owner = agent.gitea_owner or GITEA_USER
    deployment_job = await enqueue_source_push_deploy_job(
        session,
        agent,
        owner=repo_owner,
        repo=agent.name,
        source_sha=after_head,
        changed_paths=changed_files or ["<self-healing-repair>"],
        delivery_id=f"self-healing:{job.job_id}",
        ref="refs/heads/main",
        metadata={
            "trigger": "self_healing",
            "self_healing_job_id": job.job_id,
            "trigger_receipt_id": payload.get("trigger_receipt_id"),
        },
    )
    await append_event(
        session,
        job,
        event_type="self_healing_deployment_queued",
        payload={
            "deployment_job_id": deployment_job.job_id,
            "source_head_after": after_head,
            "changed_files": changed_files,
        },
        message=f"Queued exact repair commit {after_head[:12]} for deployment",
        status="running",
        commit=True,
    )

    deployment = await _wait_for_exact_deployment(
        session,
        agent=agent,
        head_sha=after_head,
        started_at=job.started_at or utcnow(),
        timeout_seconds=policy.deployment_timeout_seconds,
    )
    if deployment.status != "live":
        raise SelfHealingError(
            f"repair deployment {deployment.deploy_id} failed: "
            f"{_safe_text(deployment.error or deployment.status, limit=1200)}"
        )
    await append_event(
        session,
        job,
        event_type="self_healing_deployment_verified",
        payload=_deployment_result(deployment),
        message=f"Exact repair commit is live in {deployment.deploy_id}",
        status="complete",
        commit=True,
    )
    return {
        "summary": f"Self-healed {agent_name} at {after_head[:12]}",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "trigger_receipt_id": payload.get("trigger_receipt_id"),
        "failure": failure,
        "source_head_before": before_head,
        "source_head_after": after_head,
        "changed_files": changed_files,
        "test_evidence": test_evidence,
        "deployment": _deployment_result(deployment),
        "healed": True,
    }


async def _wait_for_exact_deployment(
    session: AsyncSession,
    *,
    agent: Agent,
    head_sha: str,
    started_at: datetime,
    timeout_seconds: int,
) -> AgentDeployment:
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    agent_id = agent.id
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    observed_id: int | None = None
    while asyncio.get_running_loop().time() < deadline:
        deployment = (
            await session.execute(
                select(AgentDeployment)
                .where(AgentDeployment.agent_id == agent_id)
                .where(AgentDeployment.head_sha == head_sha)
                .where(AgentDeployment.created_at >= started_at - timedelta(seconds=5))
                .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
                .limit(1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if deployment is not None:
            observed_id = deployment.id
            if deployment.status == "live" or deployment.status == "failed":
                return deployment
        await asyncio.sleep(5)
    detail = f"deployment row {observed_id}" if observed_id else "no deployment row"
    raise SelfHealingError(
        f"timed out waiting for exact repair commit {head_sha[:12]} ({detail})"
    )


def _deployment_result(deployment: AgentDeployment) -> dict[str, Any]:
    return {
        "deploy_id": deployment.deploy_id,
        "head_sha": deployment.head_sha,
        "status": deployment.status,
        "agent_url": deployment.agent_url,
        "verification": deployment.verification or {},
    }


async def _agent_from_payload(session: AsyncSession, payload: Mapping[str, Any]) -> Agent:
    agent_id = int(payload.get("agent_id") or 0)
    agent = await session.get(Agent, agent_id) if agent_id else None
    if agent is None:
        raise SelfHealingError("agent no longer exists")
    if str(payload.get("agent_name") or agent.name) != agent.name:
        raise SelfHealingError("self-healing job no longer matches the agent")
    current_policy = policy_from_card(agent.card if isinstance(agent.card, dict) else None)
    if current_policy is None:
        raise SelfHealingError("self-healing was disabled before the repair started")
    return agent


def _repair_prompt(
    agent: Agent,
    failure: Mapping[str, Any],
    policy: SelfHealingPolicy,
) -> str:
    evidence = json.dumps(
        {
            "skill": str(failure.get("skill_name") or "")[:128],
            "error_type": str(failure.get("error_type") or "RuntimeError")[:128],
            "error": _safe_text(failure.get("error_preview"), limit=1600),
            "fingerprint": str(failure.get("fingerprint") or "")[:64],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"Repair the production runtime failure in managed A2A agent {agent.name}.\n\n"
        f"Sanitized failure evidence: {evidence}\n\n"
        "Find the smallest source-level root-cause fix. Preserve public APIs, auth, "
        "domains, data compatibility, secrets handling, and unrelated behavior. Do "
        "not print or persist credentials and do not weaken tests, validation, auth, "
        "or security controls. Add or update a regression test that reproduces the "
        "failure, run the focused test plus the repository's available lint/typecheck "
        "or build checks, and only commit/push when they pass. Never edit generated "
        "deployment state directly. Return JSON summarizing root cause, changed files, "
        f"and commands/results. Tests required: {str(policy.require_tests).lower()}."
    )


def _editor_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    queue: list[Any] = [value]
    fallback: dict[str, Any] = dict(value)
    while queue:
        item = queue.pop(0)
        if not isinstance(item, Mapping):
            continue
        if "push" in item and "sync" in item and "changes" in item:
            return dict(item)
        for nested in item.values():
            if isinstance(nested, Mapping):
                queue.append(nested)
    return fallback


def _sha(value: Any) -> str | None:
    cleaned = str(value or "").strip().lower()
    return cleaned if _SHA_RE.fullmatch(cleaned) else None


def _test_evidence(editor: Mapping[str, Any]) -> dict[str, Any]:
    parsed = editor.get("parsed")
    if isinstance(parsed, Mapping):
        for key in ("tests", "test_results", "verification", "checks"):
            value = parsed.get(key)
            if isinstance(value, (Mapping, list, str)):
                return {"source": f"parsed.{key}", "result": _safe_json(value)}
    stdout = _safe_text(editor.get("stdout"), limit=2000)
    return {"source": "editor_stdout_tail", "result": stdout or "not reported"}


def _tests_explicitly_failed(editor: Mapping[str, Any]) -> bool:
    rendered = json.dumps(_safe_json(editor.get("parsed")), ensure_ascii=False).lower()
    return any(marker in rendered for marker in ('"tests_passed": false', '"status": "failed"', '"tests_failed": true'))


def _result_error(result: Mapping[str, Any]) -> str:
    for key in ("error", "stderr", "stdout"):
        value = _safe_text(result.get(key), limit=1600)
        if value:
            return value
    return "code editor failed to produce a repair"


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            lowered = str(key).lower()
            if any(part in lowered for part in ("token", "secret", "password", "credential", "api_key", "authorization")):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _safe_json(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, str):
        return _safe_text(value, limit=4000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(str(value), limit=1000)


def sanitize_public_payload(value: Any) -> Any:
    """Bound and redact a repair-ledger value before returning it to the app."""
    return _safe_json(value)


def _safe_text(value: Any, *, limit: int = 1000) -> str:
    text = str(value or "").strip()
    if text[:1] in {"{", "["}:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and not isinstance(parsed, str):
            text = json.dumps(_safe_json(parsed), ensure_ascii=False, separators=(",", ":"))
    text = _TOKEN_RE.sub("[redacted]", text)
    text = _URL_CREDS_RE.sub(r"\1[redacted]@", text)
    text = _SENSITIVE_VALUE_RE.sub(r"\1'[redacted]'", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
