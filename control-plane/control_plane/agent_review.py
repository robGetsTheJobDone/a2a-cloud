"""Async deploy-time review wiring.

After ``POST /v1/agents/from-tarball`` successfully commits source to the
managed repo, this module enqueues an async review by the ``agent-reviewer``
meta-agent. The deploy never waits for the review (advisory mode); the
result lands in :class:`AgentReviewRun` and a follow-up
:class:`AgentDeploymentEvent` with ``stage="review"``.

The reviewer is itself an A2A agent, invoked over HTTP through the same
``/invoke/{skill}`` SSE channel that handoffs use. The deploying user's
``cp_jwt`` is forwarded so the reviewer's ``_assert_caller_can_access_repo``
authz check works unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS, issue_invocation_cp_credential
from .db import SessionLocal
from .deployments import record_deployment_event
from .models import AgentDeployment, AgentReviewRun
from .platform_settings import is_reviewer_enabled
from main_agent.config import load_settings as load_runtime_settings
from main_agent.grants import GrantClaims, mint_grant_token

logger = logging.getLogger(__name__)


REVIEWER_AGENT_NAME = os.environ.get("A2A_CP_REVIEWER_AGENT", "agent-reviewer")
REVIEWER_AGENT_DNS = os.environ.get(
    "A2A_CP_REVIEWER_DNS", "{name}.agents.svc.cluster.local"
)
REVIEWER_TIMEOUT_S = float(os.environ.get("A2A_CP_REVIEWER_TIMEOUT_S", "180"))
REVIEWER_LLM_GRANT_TTL_S = int(os.environ.get("A2A_CP_REVIEWER_LLM_GRANT_TTL_S", "600"))
REVIEWER_SKILL = "review"
#: Outer bound of one reviewer call, and the cap on the caller identity handed
#: to it, so a copy kept by the reviewer process outlives the call.
#:
#: Explicitly *not* ``REVIEWER_TIMEOUT_S``: the value below is passed to
#: ``httpx.AsyncClient`` and then used on a streamed SSE response, where it
#: bounds the idle gap between reads, not the total duration — and the reviewer
#: emits a progress heartbeat every 20s for the life of its graph, so the
#: stream never idles and a review may legitimately run far past 180s.
#: The real ceiling is the one the platform enforces on the reviewer's own
#: Knative revision (``k8s.KNATIVE_MAX_TIMEOUT_SECONDS``, 1800s, applied to
#: every agent by ``_declared_runtime_timeout``): the request cannot outlive
#: it. That is the same bound ``AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS`` already
#: encodes, so the credential is capped there rather than at a number that
#: would expire mid-review and 401 the reviewer's own Gitea-token release.
REVIEWER_CALL_TTL_SECONDS = AGENT_INVOKE_TOKEN_MAX_TTL_SECONDS


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reviewer_url() -> str:
    host = REVIEWER_AGENT_DNS.format(name=REVIEWER_AGENT_NAME)
    return f"http://{host}/invoke/{REVIEWER_SKILL}"


def _reviewer_llm_creds(
    *,
    user_id: int,
    review_id: str,
    max_budget_usd: float | None = None,
) -> dict[str, Any] | None:
    settings = load_runtime_settings()
    models = tuple(settings.platform_llm_models) or (
        (settings.litellm_model,) if settings.litellm_model else ()
    )
    if not models:
        return None
    try:
        token, payload = mint_grant_token(
            GrantClaims(
                issuer=f"deploy-review:user-{user_id}",
                audience=REVIEWER_AGENT_NAME,
                bucket=f"user-{user_id}-files",
                mode="read_only",
                allow_patterns=(),
                llm_models=models,
                llm_max_budget_usd=(
                    max(0.01, float(max_budget_usd))
                    if max_budget_usd is not None
                    else settings.platform_llm_max_budget_usd
                ),
                llm_rpm_limit=settings.platform_llm_rpm_limit,
                llm_tpm_limit=settings.platform_llm_tpm_limit,
                ttl_seconds=REVIEWER_LLM_GRANT_TTL_S,
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not mint deploy-review LLM grant review_id=%s", review_id)
        return None
    return {
        "base_url": str(settings.litellm_url).rstrip("/") + "/v1",
        "api_key": token,
        "model": models[0],
        "temperature_mode": "omit",
        "extra_body": {},
        "metadata": {
            "a2a_user_id": user_id,
            "a2a_grant_id": payload.get("grant_id"),
            "a2a_agent_name": REVIEWER_AGENT_NAME,
            "a2a_skill_name": REVIEWER_SKILL,
            "a2a_llm_source": "deploy-review",
            "deploy_review_id": review_id,
        },
    }


async def _call_reviewer(
    *,
    agent_name: str,
    ref: str,
    owner: str | None,
    cp_jwt: str,
    cp_url: str,
    llm_creds: dict[str, Any] | None = None,
    mode: str = "audit",
) -> dict[str, Any]:
    """POST to the reviewer's invoke endpoint and parse the SSE stream.

    Returns the final result dict (the reviewer's ``review`` skill return
    value) on success. Raises :class:`RuntimeError` on HTTP failure or if
    the stream never yields a result event.
    """
    url = _reviewer_url()
    body: dict[str, Any] = {
        "arguments": {
            "agent_name": agent_name,
            "ref": ref,
            **({"owner": owner} if owner else {}),
            **({"mode": "improvements"} if mode == "improvements" else {}),
        },
        "cp_jwt": cp_jwt,
        "cp_url": cp_url,
    }
    if llm_creds is not None:
        body["llm_creds"] = llm_creds
    last_result: dict[str, Any] | None = None
    async with httpx.AsyncClient(timeout=REVIEWER_TIMEOUT_S + 30.0) as client:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers={"Accept": "text/event-stream"},
        ) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"reviewer {resp.status_code}: {detail}")
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    data_lines = [
                        ln[5:].lstrip()
                        for ln in raw.split("\n")
                        if ln.startswith("data:")
                    ]
                    if not data_lines:
                        continue
                    payload_str = "\n".join(data_lines)
                    if payload_str == "[DONE]":
                        continue
                    try:
                        ev = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "result":
                        result_payload = ev.get("result", ev)
                        if isinstance(result_payload, dict):
                            last_result = result_payload
    if last_result is None:
        raise RuntimeError("reviewer stream ended without a result event")
    return last_result


def _classify_status(report: dict[str, Any]) -> tuple[str, int, int, int]:
    """Map a ReviewReport-shaped dict to (status, critical, warning, info)."""
    if not isinstance(report, dict):
        return "errored", 0, 0, 0
    if "error" in report and not report.get("findings"):
        return "errored", 0, 0, 0
    findings = report.get("findings") or []
    critical = sum(1 for f in findings if f.get("severity") == "critical")
    warning = sum(1 for f in findings if f.get("severity") == "warning")
    info = sum(1 for f in findings if f.get("severity") == "info")
    if critical > 0:
        return "failed", critical, warning, info
    if warning > 0:
        return "warning", critical, warning, info
    return "passed", critical, warning, info


def _event_status_for_review(status: str) -> str:
    if status == "passed":
        return "passed"
    if status in ("warning", "failed"):
        return "failed"
    return "running"


async def _resolve_deployment(
    session: AsyncSession, deploy_id: str | None
) -> AgentDeployment | None:
    if not deploy_id:
        return None
    return (
        await session.execute(
            select(AgentDeployment).where(AgentDeployment.deploy_id == deploy_id)
        )
    ).scalar_one_or_none()


async def run_deploy_review(
    *,
    review_id: str,
    agent_id: int,
    agent_name: str,
    ref: str,
    owner: str | None,
    user_id: int,
    user_jwt: str,
    cp_url: str,
    deploy_id: str | None,
) -> None:
    """Execute one async review against the reviewer agent and persist results."""
    started = time.monotonic()
    async with SessionLocal() as session:
        row = AgentReviewRun(
            review_id=review_id,
            deploy_id=deploy_id,
            agent_id=agent_id,
            agent_name=agent_name,
            ref=ref,
            user_id=user_id,
            status="running",
            started_at=_utcnow(),
            findings=[],
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        deployment = await _resolve_deployment(session, deploy_id)

        enabled = await is_reviewer_enabled(session)
        if not enabled:
            row.status = "skipped"
            row.error = "reviewer disabled via platform_settings.reviewer_enabled"
            row.completed_at = _utcnow()
            row.elapsed_ms = int((time.monotonic() - started) * 1000)
            await session.commit()
            if deployment is not None:
                await record_deployment_event(
                    session,
                    deployment,
                    stage="review",
                    status="passed",
                    message="agent-reviewer skipped (disabled in admin panel)",
                    data={"review_id": review_id},
                )
            return

        if deployment is not None:
            await record_deployment_event(
                session,
                deployment,
                stage="review",
                status="running",
                message=f"agent-reviewer running ({REVIEWER_AGENT_NAME})",
                data={"review_id": review_id, "ref": ref},
            )

        try:
            llm_creds = _reviewer_llm_creds(user_id=user_id, review_id=review_id)
            report = await _call_reviewer(
                agent_name=agent_name,
                ref=ref,
                owner=owner,
                cp_jwt=user_jwt,
                cp_url=cp_url,
                llm_creds=llm_creds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("reviewer call failed for %s@%s", agent_name, ref)
            row.status = "errored"
            row.error = f"{type(exc).__name__}: {exc}"
            row.completed_at = _utcnow()
            row.elapsed_ms = int((time.monotonic() - started) * 1000)
            await session.commit()
            if deployment is not None:
                await record_deployment_event(
                    session,
                    deployment,
                    stage="review",
                    status="failed",
                    message=f"reviewer call errored: {row.error[:200]}",
                    data={"review_id": review_id},
                )
            return

        status, crit, warn, info = _classify_status(report)
        findings = report.get("findings") or []
        row.status = status
        row.findings = list(findings)
        row.critical_count = crit
        row.warning_count = warn
        row.info_count = info
        row.summary = report.get("summary")
        row.completed_at = _utcnow()
        row.elapsed_ms = int((time.monotonic() - started) * 1000)
        await session.commit()

        if deployment is not None:
            await record_deployment_event(
                session,
                deployment,
                stage="review",
                status=_event_status_for_review(status),
                message=(
                    f"agent-reviewer {status}: {crit} critical, "
                    f"{warn} warning, {info} info"
                ),
                data={
                    "review_id": review_id,
                    "critical": crit,
                    "warning": warn,
                    "info": info,
                    "summary": row.summary,
                },
            )


def enqueue_deploy_review(
    app: Any,
    *,
    agent_id: int,
    agent_name: str,
    ref: str,
    owner: str | None,
    user_id: int,
    cp_url: str | None = None,
    deploy_id: str | None = None,
) -> str | None:
    """Fire-and-forget async review. Returns the review_id, or None on JWT failure.

    The deploying user's identity is captured here; a fresh credential is
    issued so the background task is not coupled to the original request's
    token TTL. ``agent-reviewer`` is first-party code from this repo and drives
    the caller's source surface on their behalf, so
    :func:`issue_invocation_cp_credential` deliberately keeps it an ordinary
    platform credential — but only for as long as one reviewer call may run. A
    non-default ``A2A_CP_REVIEWER_AGENT`` is not a platform specialist and gets
    the scoped invoke credential instead.

    Whether the reviewer actually runs is decided at task start by the DB
    ``reviewer_enabled`` flag — see :func:`is_reviewer_enabled`. Tasks always
    enqueue so a "skipped" row appears in the audit log either way.
    """
    review_id = uuid.uuid4().hex
    try:
        user_jwt = issue_invocation_cp_credential(
            user_id,
            agent=REVIEWER_AGENT_NAME,
            ttl_seconds=REVIEWER_CALL_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not issue user JWT for review enqueue user_id=%s", user_id)
        return None
    resolved_cp_url = cp_url or os.environ.get(
        "A2A_CP_URL", "http://control-plane.control-plane.svc.cluster.local"
    )
    task = asyncio.create_task(
        run_deploy_review(
            review_id=review_id,
            agent_id=agent_id,
            agent_name=agent_name,
            ref=ref,
            owner=owner,
            user_id=user_id,
            user_jwt=user_jwt,
            cp_url=resolved_cp_url,
            deploy_id=deploy_id,
        ),
        name=f"agent-review-{agent_name}-{review_id[:8]}",
    )
    tasks: set[asyncio.Task[Any]] = getattr(app.state, "agent_review_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.agent_review_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return review_id


# Silence an unused-import warning for ``secrets`` when downstream code uses
# it indirectly (review_id generation switched to uuid4 but secrets remains a
# useful module-level helper for future variants).
_ = secrets
