from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..argo import request_application_refresh
from ..config import settings
from ..db import get_session
from ..deployments import (
    ACTIVE_DEPLOY_STATUSES,
    record_deployment_event,
)
from ..gitea import agent_name_from_runtime_repo, is_runtime_repo_name, is_user_source_path
from ..gitea_meta import META_WRITER_USER
from ..models import Agent, AgentDeployment, AgentDeploymentEvent
from ..source_push_deployments import (
    enqueue_source_push_deploy_job,
    find_agent_for_source_repo,
)

# Gitea's callback, authenticated by the webhook HMAC signature; not part of
# the customer-callable API.
router = APIRouter(
    prefix="/v1/platform/gitea/webhooks",
    tags=["gitea-webhooks"],
    include_in_schema=False,
)

_ZERO_SHA = "0" * 40
_PLATFORM_SOURCE_EDIT_COMMIT_PREFIXES = ("a2a-source-edit:", "[a2a-source-edit]")
_PLATFORM_SOURCE_EDIT_EMAILS = {"platform@a2a.local", f"noreply@{settings.platform_domain}"}


@router.post("/source-push")
async def source_push_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if not settings.gitea_source_webhooks_enabled:
        return _ignored("disabled")

    secret = settings.gitea_source_webhook_secret
    if not secret:
        raise HTTPException(503, "Gitea source webhooks are not configured")

    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get("x-gitea-signature"), secret)

    event = request.headers.get("x-gitea-event", "").strip().lower()
    if event != "push":
        return _ignored("non_push_event")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "malformed webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "malformed webhook payload")

    ref = _string(payload.get("ref"))
    if ref != "refs/heads/main":
        return _ignored("non_main_ref", ref=ref)

    source_sha = _string(payload.get("after"))
    if not source_sha or source_sha == _ZERO_SHA or payload.get("deleted") is True:
        return _ignored("branch_deleted")

    owner, repo = _repository_scope(payload)
    if not owner or not repo:
        raise HTTPException(400, "repository owner/name missing from webhook payload")

    if is_runtime_repo_name(repo):
        return _ignored("runtime_repo", owner=owner, repo=repo)

    changed_paths = _changed_paths(payload)
    user_paths = [path for path in changed_paths if is_user_source_path(path)]
    if not user_paths:
        return _ignored(
            "platform_only_changes" if changed_paths else "no_changed_paths",
            owner=owner,
            repo=repo,
        )
    if _is_platform_source_edit_push(payload):
        return _ignored("platform_source_edit_push", owner=owner, repo=repo)

    agent = await find_agent_for_source_repo(session, owner=owner, repo=repo)
    if agent is None:
        return _ignored("unknown_repo", owner=owner, repo=repo)

    delivery_id = request.headers.get("x-gitea-delivery")
    job = await enqueue_source_push_deploy_job(
        session,
        agent,
        owner=owner,
        repo=repo,
        source_sha=source_sha,
        changed_paths=user_paths,
        delivery_id=delivery_id,
        ref=ref,
        metadata={"delivery_id": delivery_id},
    )
    return JSONResponse(
        status_code=202,
        content={
            "status": "queued",
            "job_id": job.job_id,
            "agent": agent.name,
            "source_sha": source_sha,
        },
    )


@router.post("/runtime-push")
async def runtime_push_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    if not settings.gitea_runtime_webhooks_enabled:
        return _ignored("disabled")

    secret = settings.gitea_runtime_webhook_secret
    if not secret:
        raise HTTPException(503, "Gitea runtime webhooks are not configured")

    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get("x-gitea-signature"), secret)

    event = request.headers.get("x-gitea-event", "").strip().lower()
    if event != "push":
        return _ignored("non_push_event")

    payload = _payload(raw_body)
    ref = _string(payload.get("ref"))
    if ref != "refs/heads/main":
        return _ignored("non_main_ref", ref=ref)

    runtime_sha = _string(payload.get("after"))
    if not runtime_sha or runtime_sha == _ZERO_SHA or payload.get("deleted") is True:
        return _ignored("branch_deleted")

    owner, repo = _repository_scope(payload)
    if not owner or not repo:
        raise HTTPException(400, "repository owner/name missing from webhook payload")

    agent_name = agent_name_from_runtime_repo(repo)
    if not agent_name:
        return _ignored("source_repo", owner=owner, repo=repo)

    agent = (
        await session.execute(select(Agent).where(Agent.name == agent_name))
    ).scalar_one_or_none()
    if agent is None:
        return _ignored("unknown_agent", owner=owner, repo=repo, agent=agent_name)

    changed_paths = _changed_paths(payload)
    deployment = await _runtime_webhook_deployment(session, agent)
    expected_image = await _expected_runtime_image(session, agent, deployment)
    delivery_id = request.headers.get("x-gitea-delivery")
    refresh_result: dict[str, Any] | None = None
    refresh_error: str | None = None

    try:
        refresh_result = dict(request_application_refresh(agent.name, hard=True))
    except Exception as exc:  # noqa: BLE001
        refresh_error = _short_error(exc)

    if deployment is not None:
        await record_deployment_event(
            session,
            deployment,
            stage="runtime",
            status="running",
            message=(
                "Runtime repo pushed deploy manifests; waiting for image build "
                "and rollout to settle."
            ),
            data={
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "runtime_head_sha": runtime_sha,
                "changed_paths": changed_paths,
                "delivery_id": delivery_id,
                "expected_image": expected_image,
            },
        )
        await record_deployment_event(
            session,
            deployment,
            stage="argo",
            status="running",
            message=(
                "Requested ArgoCD hard refresh for the runtime repo push."
                if refresh_error is None
                else "ArgoCD refresh request failed; waiting for automated reconciliation."
            ),
            data={
                "runtime_head_sha": runtime_sha,
                "expected_revision": runtime_sha,
                "expected_image": expected_image,
                "refresh": refresh_result,
                "error": refresh_error,
            },
        )

    return JSONResponse(
        status_code=202,
        content={
            "status": "refreshed" if refresh_error is None else "refresh_failed",
            "agent": agent.name,
            "runtime_sha": runtime_sha,
            "deploy_id": deployment.deploy_id if deployment is not None else None,
            "refresh_error": refresh_error,
        },
    )


def _verify_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    if not signature:
        raise HTTPException(401, "webhook signature required")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    supplied = signature.strip()
    if supplied.startswith("sha256="):
        supplied = supplied.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "webhook signature mismatch")


def _payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "malformed webhook payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "malformed webhook payload")
    return payload


async def _runtime_webhook_deployment(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    active = (
        await session.execute(
            select(AgentDeployment)
            .where(AgentDeployment.agent_id == agent.id)
            .where(AgentDeployment.status.in_(ACTIVE_DEPLOY_STATUSES))
            .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        return active
    return (
        await session.execute(
            select(AgentDeployment)
            .where(AgentDeployment.agent_id == agent.id)
            .order_by(desc(AgentDeployment.created_at), desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _expected_runtime_image(
    session: AsyncSession,
    agent: Agent,
    deployment: AgentDeployment | None,
) -> str | None:
    if deployment is not None:
        recorded = await _recorded_expected_image(session, deployment)
        if recorded:
            return recorded
    head_sha = deployment.head_sha if deployment is not None else None
    image = deployment.image if deployment is not None else agent.image
    if _is_pinned_managed_agent_image(image, agent.name):
        return image
    if head_sha and _is_managed_agent_image(image, agent.name):
        return settings.agent_image(agent.name, head_sha)
    return image or None


async def _recorded_expected_image(
    session: AsyncSession,
    deployment: AgentDeployment,
) -> str | None:
    events = (
        await session.execute(
            select(AgentDeploymentEvent)
            .where(AgentDeploymentEvent.deployment_id == deployment.id)
            .order_by(desc(AgentDeploymentEvent.id))
        )
    ).scalars().all()
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        expected = _string(data.get("expected_image"))
        if expected:
            return expected
    return None


def _is_pinned_managed_agent_image(image: str | None, agent_name: str) -> bool:
    if not image:
        return False
    prefix = settings.agent_image_prefix(agent_name)
    if not image.startswith(prefix):
        return False
    return image.removeprefix(prefix) != "latest"


def _is_managed_agent_image(image: str | None, agent_name: str) -> bool:
    if not image:
        return False
    prefix = settings.agent_image_prefix(agent_name)
    return image.startswith(prefix)


def _repository_scope(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None, None
    name = _string(repo.get("name"))
    owner_payload = repo.get("owner")
    owner: str | None = None
    if isinstance(owner_payload, dict):
        for key in ("username", "login", "name"):
            owner = _string(owner_payload.get(key))
            if owner:
                break
    if not owner:
        full_name = _string(repo.get("full_name"))
        if full_name and "/" in full_name:
            owner = full_name.split("/", 1)[0]
    return owner, name


def _changed_paths(payload: dict[str, Any]) -> list[str]:
    paths: set[str] = set()
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for key in ("added", "modified", "removed"):
            values = commit.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                path = _string(value)
                if path:
                    paths.add(path)
    return sorted(paths)


def _all_commits_platform_authored(payload: dict[str, Any]) -> bool:
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return False
    for commit in commits:
        if not isinstance(commit, dict):
            return False
        emails = _commit_emails(commit)
        if not emails or any(email.lower() not in _PLATFORM_SOURCE_EDIT_EMAILS for email in emails):
            return False
    return True


def _is_platform_source_edit_push(payload: dict[str, Any]) -> bool:
    meta_writer = META_WRITER_USER.lower()
    if (_actor_username(payload.get("pusher")) or "").lower() == meta_writer:
        return True
    if (_actor_username(payload.get("sender")) or "").lower() == meta_writer:
        return True
    if _all_commits_have_platform_source_edit_marker(payload):
        return True
    return _all_commits_platform_authored(payload)


def _all_commits_have_platform_source_edit_marker(payload: dict[str, Any]) -> bool:
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return False
    for commit in commits:
        if not isinstance(commit, dict):
            return False
        message = (_string(commit.get("message")) or "").lower()
        if not any(message.startswith(prefix) for prefix in _PLATFORM_SOURCE_EDIT_COMMIT_PREFIXES):
            return False
    return True


def _actor_username(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("username", "login", "name"):
        username = _string(value.get(key))
        if username:
            return username
    return None


def _commit_emails(commit: dict[str, Any]) -> list[str]:
    emails: list[str] = []
    for key in ("author", "committer"):
        value = commit.get(key)
        if isinstance(value, dict):
            email = _string(value.get("email"))
            if email:
                emails.append(email)
    return emails


def _string(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _ignored(reason: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=202, content={"status": "ignored", "reason": reason, **extra})


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:240]
