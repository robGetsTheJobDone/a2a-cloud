from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from kubernetes import client
from kubernetes.client.rest import ApiException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import gitea
from .argo import application_summary
from .card_cache import warm_agent_card
from .config import settings
from .k8s import (
    KNATIVE_API_GROUP,
    KNATIVE_API_VERSION,
    KNATIVE_SERVICES_PLURAL,
    _load_kube,
    agent_pod_logs,
)
from .log_scrub import bound_and_scrub
from .models import (
    Agent,
    AgentDatabaseBinding,
    AgentDeployment,
    AgentDeploymentEvent,
    AgentDeploymentLog,
)

log = logging.getLogger(__name__)

ACTIVE_DEPLOY_STATUSES = {"queued", "building", "deploying", "verifying"}
TRANSIENT_AGENT_STATUSES = ACTIVE_DEPLOY_STATUSES | {"pending", "provisioning"}
DEPLOY_TIMEOUT_SECONDS = 30 * 60
MISSING_ARGO_GRACE_SECONDS = 90
# The provisioner deliberately requeues failed bindings. A transient Neon or
# network failure must not make the deployment terminal before those retries
# have a reasonable opportunity to succeed.
DATABASE_FAILURE_GRACE_SECONDS = 5 * 60
# Per (stage, source) log tail budget. Bounded before scrubbing so the persisted
# string is never larger, and kept small enough to stream in a snapshot payload.
DEPLOY_LOG_MAX_BYTES = 16 * 1024
DEPLOY_LOG_TAIL_LINES = 200


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_agent_url(name: str) -> str:
    return f"https://{settings.ingress_host_template.format(name=name)}"


async def create_deployment(
    session: AsyncSession,
    agent: Agent,
    *,
    trigger: str,
    status: str,
    source_repo_url: str | None = None,
    head_sha: str | None = None,
    image: str | None = None,
    agent_url: str | None = None,
) -> AgentDeployment:
    deploy = AgentDeployment(
        deploy_id=f"dpl_{uuid.uuid4().hex[:16]}",
        agent_id=agent.id,
        user_id=agent.owner_id,
        agent_name=agent.name,
        trigger=trigger,
        status=status,
        source_repo_url=source_repo_url,
        head_sha=head_sha,
        image=image or agent.image,
        agent_url=agent_url or agent.url,
        started_at=utcnow(),
    )
    session.add(deploy)
    await session.commit()
    await session.refresh(deploy)
    return deploy


async def record_deployment_event(
    session: AsyncSession,
    deploy: AgentDeployment,
    *,
    stage: str,
    status: str,
    message: str,
    data: dict[str, Any] | None = None,
    commit: bool = True,
) -> AgentDeploymentEvent:
    event = AgentDeploymentEvent(
        deployment_id=deploy.id,
        deploy_id=deploy.deploy_id,
        agent_name=deploy.agent_name,
        stage=stage,
        status=status,
        message=message,
        data=data or {},
    )
    session.add(event)
    if commit:
        await session.commit()
        await session.refresh(event)
    return event


async def fail_deployment(
    session: AsyncSession,
    deploy: AgentDeployment | None,
    *,
    stage: str,
    error: str,
) -> None:
    if deploy is None:
        return
    deploy.status = "failed"
    deploy.error = error
    deploy.completed_at = utcnow()
    await record_deployment_event(
        session,
        deploy,
        stage=stage,
        status="failed",
        message=error,
        commit=False,
    )
    await session.commit()


async def sync_deployment_verification(
    session: AsyncSession,
    agent: Agent,
    deploy: AgentDeployment,
    *,
    on_agent_changed: Callable[[Agent], Awaitable[None]] | None = None,
) -> AgentDeployment:
    """Refresh a deploy run from live Argo/Kubernetes/agent-card signals.

    Gitea Actions and ArgoCD are asynchronous and do not currently call
    back into the control plane. This function makes deployment runs feel
    live by deriving progress from the platform's observable state.
    """
    if deploy.status == "failed":
        if agent.status in TRANSIENT_AGENT_STATUSES:
            agent.status = "failed"
            await session.commit()
            if on_agent_changed is not None:
                await on_agent_changed(agent)
            await session.refresh(deploy)
        return deploy

    expected = await _expected_deployment_signals(session, agent, deploy)
    verification = await verify_agent_deployment(agent, expected=expected)
    workload_live = bool(verification.get("live"))
    database = await _database_resources_verification(session, agent)
    verification["database"] = database
    verification["live"] = bool(workload_live and database["ok"])
    if expected:
        verification["expected"] = expected
    deploy.verification = verification

    live = bool(verification.get("live"))
    database_failed = bool(database["failed"])
    database_failure_terminal = bool(
        database_failed
        and _deployment_age_seconds(deploy) > DATABASE_FAILURE_GRACE_SECONDS
    )
    timed_out = _deployment_timed_out(deploy)
    missing_argo = _missing_argo_application(deploy, verification)
    agent_changed = False
    if live:
        live_url = _string_or_none(verification.get("url")) or _canonical_agent_url(agent.name)
        deploy.status = "live"
        deploy.error = None
        if deploy.agent_url != live_url:
            deploy.agent_url = live_url
        if agent.url != live_url:
            agent.url = live_url
            agent_changed = True
        deploy.completed_at = deploy.completed_at or utcnow()
        if agent.status != "running":
            agent.status = "running"
            agent_changed = True
        # Persist the card from this controlled deploy-time fetch so passive
        # registry reads can serve it without waking a scaled-to-zero agent,
        # and warm the Redis cache so this new version is served from cache.
        card_body = verification.get("agent_card", {}).get("body")
        if isinstance(card_body, dict) and card_body.get("skills"):
            card_body = _preserve_control_plane_card_metadata(agent.card, card_body)
            if agent.card != card_body:
                agent.card = card_body
                agent_changed = True
            card_version = _string_or_none(card_body.get("version"))
            if card_version and agent.version != card_version:
                agent.version = card_version
                agent_changed = True
            await warm_agent_card(agent.name, card_body)
    elif database_failure_terminal and deploy.status in ACTIVE_DEPLOY_STATUSES:
        deploy.status = "failed"
        deploy.error = "Managed database provisioning did not recover within the retry window."
        deploy.completed_at = utcnow()
        if agent.status in TRANSIENT_AGENT_STATUSES:
            agent.status = "failed"
            agent_changed = True
    elif (timed_out or missing_argo) and deploy.status in ACTIVE_DEPLOY_STATUSES:
        deploy.status = "failed"
        deploy.error = _deployment_failure_reason(
            timed_out=timed_out,
            missing_argo=missing_argo,
        )
        deploy.completed_at = utcnow()
        if agent.status in TRANSIENT_AGENT_STATUSES:
            agent.status = "failed"
            agent_changed = True
    elif deploy.status in ACTIVE_DEPLOY_STATUSES:
        deploy.status = _derived_status(verification)

    failed = deploy.status == "failed"
    await _upsert_progress_event(
        session,
        deploy,
        stage="build",
        status="passed" if workload_live else ("failed" if failed else "running"),
        message=(
            "Image is built and serving traffic."
            if workload_live
            else deploy.error
            if failed and deploy.error
            else "Waiting for the image build and reconcile to finish."
        ),
        data={"image": deploy.image or agent.image},
    )
    await _upsert_progress_event(
        session,
        deploy,
        stage="database",
        status=(
            "passed"
            if database["ok"]
            else ("failed" if database_failure_terminal else "running")
        ),
        message=_database_message(
            database,
            terminal_failure=database_failure_terminal,
        ),
        data=database,
    )
    await _upsert_progress_event(
        session,
        deploy,
        stage="argo",
        status=_status_from_bool(verification.get("argo", {}).get("ok"), failed),
        message=_argo_message(verification.get("argo", {})),
        data=verification.get("argo", {}),
    )
    await _upsert_progress_event(
        session,
        deploy,
        stage="runtime",
        status=_status_from_bool(verification.get("runtime", {}).get("ok"), failed),
        message=_runtime_message(verification.get("runtime", {})),
        data=verification.get("runtime", {}),
    )
    await _upsert_progress_event(
        session,
        deploy,
        stage="agent_card",
        status=_status_from_bool(verification.get("agent_card", {}).get("ok"), failed),
        message=_agent_card_message(verification.get("agent_card", {})),
        data=verification.get("agent_card", {}),
    )
    await _upsert_progress_event(
        session,
        deploy,
        stage="skills",
        status=_status_from_bool(verification.get("skills", {}).get("ok"), failed),
        message=_skills_message(verification.get("skills", {})),
        data=verification.get("skills", {}),
    )
    if failed and deploy.error:
        await _upsert_progress_event(
            session,
            deploy,
            stage="verify",
            status="failed",
            message=deploy.error,
            data=verification,
        )
    elif live:
        await _upsert_progress_event(
            session,
            deploy,
            stage="verify",
            status="passed",
            message="Agent is live, has a card, and exposes callable skills.",
            data=verification,
        )
    await session.commit()
    if agent_changed and on_agent_changed is not None:
        await on_agent_changed(agent)
    await session.refresh(deploy)
    return deploy


async def collect_deployment_logs(
    session: AsyncSession,
    deploy: AgentDeployment,
    verification: dict[str, Any],
) -> None:
    """Proxy raw build/runtime logs into per-stage :class:`AgentDeploymentLog`.

    Called lazily from the deployment-logs endpoint (never from bulk list or the
    2s stream poll) so the Gitea/pod fetches only happen when a user actually
    opens the log drawer. Best-effort and defensive: any failure to reach Gitea,
    Argo, or the pod log endpoint must never break the request. Fetching stops
    once the deploy is live (scaled-to-zero pods have no live logs and the last
    active-state tail is already persisted).
    """
    if deploy.status == "live":
        return

    # Runtime / pod readiness logs (highest-value for debugging crashloops).
    try:
        pods = agent_pod_logs(deploy.agent_name, tail_lines=DEPLOY_LOG_TAIL_LINES)
        text = _format_pod_logs(pods)
        if text:
            await _upsert_deployment_log(session, deploy, stage="runtime", source="pod", text=text)
    except Exception as exc:  # noqa: BLE001
        log.debug("pod log fetch failed for %s: %s", deploy.agent_name, exc)

    # Gitea Actions / image build logs.
    try:
        build = gitea.action_build_status(
            deploy.agent_name, head_sha=deploy.head_sha
        )
        text = _format_action_logs(build)
        if text:
            await _upsert_deployment_log(
                session, deploy, stage="build", source="gitea_actions", text=text
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("gitea actions log fetch failed for %s: %s", deploy.agent_name, exc)

    # Argo sync detail is already in the verification payload — surface it as a log.
    try:
        text = _format_argo_logs(verification.get("argo", {}))
        if text:
            await _upsert_deployment_log(session, deploy, stage="argo", source="argo", text=text)
    except Exception as exc:  # noqa: BLE001
        log.debug("argo log format failed for %s: %s", deploy.agent_name, exc)


def _format_pod_logs(pods: dict[str, Any]) -> str:
    if not pods.get("ok"):
        err = pods.get("error")
        return f"[pod logs unavailable: {err}]" if err else ""
    blocks: list[str] = []
    for entry in pods.get("pods", []):
        header = f"=== pod {entry['pod']} / {entry['container']} (phase={entry.get('phase')}, restarts={entry.get('restarts')}) ==="
        blocks.append(header)
        prev = entry.get("previous_log")
        if prev:
            blocks.append("--- previous container (crash) ---")
            blocks.append(prev.rstrip())
        current = entry.get("log")
        if current:
            blocks.append(current.rstrip())
    return "\n".join(b for b in blocks if b).strip()


def _format_action_logs(build: dict[str, Any]) -> str:
    if build.get("log"):
        return str(build["log"])
    lines: list[str] = []
    run = build.get("run")
    if isinstance(run, dict):
        lines.append(
            "Build run "
            f"{run.get('workflow') or 'workflow'} "
            f"#{run.get('run_number') or '?'}: "
            f"status={run.get('status')} conclusion={run.get('conclusion')}"
        )
    elif build.get("reason"):
        lines.append(str(build["reason"]))
    web = build.get("web_url")
    if web:
        lines.append(f"View full build logs: {web}")
    return "\n".join(lines).strip()


def _format_argo_logs(argo: dict[str, Any]) -> str:
    if not isinstance(argo, dict) or not argo:
        return ""
    lines = [
        f"exists={argo.get('exists')}",
        f"health={argo.get('health')}",
        f"sync={argo.get('sync')}",
        f"revision={argo.get('revision')}",
    ]
    for key in ("message", "operation_message", "error"):
        val = argo.get(key)
        if val:
            lines.append(f"{key}: {val}")
    return "\n".join(str(line) for line in lines if line and "None" not in str(line)).strip()


async def _upsert_deployment_log(
    session: AsyncSession,
    deploy: AgentDeployment,
    *,
    stage: str,
    source: str,
    text: str,
) -> None:
    content, truncated = bound_and_scrub(text, max_bytes=DEPLOY_LOG_MAX_BYTES)
    if not content:
        return
    byte_len = len(content.encode("utf-8", "replace"))
    existing = (
        await session.execute(
            select(AgentDeploymentLog).where(
                AgentDeploymentLog.deployment_id == deploy.id,
                AgentDeploymentLog.stage == stage,
                AgentDeploymentLog.source == source,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.content == content:
            return
        existing.content = content
        existing.truncated = truncated
        existing.byte_len = byte_len
        return
    session.add(
        AgentDeploymentLog(
            deployment_id=deploy.id,
            deploy_id=deploy.deploy_id,
            agent_name=deploy.agent_name,
            stage=stage,
            source=source,
            content=content,
            truncated=truncated,
            byte_len=byte_len,
        )
    )


async def verify_agent_deployment(
    agent: Agent,
    *,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected = expected or {}
    expected_revision = _string_or_none(expected.get("runtime_revision"))
    expected_image = _string_or_none(expected.get("image"))
    argo = _safe_argo(agent.name, expected_revision=expected_revision)
    runtime = _safe_runtime(agent.name, expected_image=expected_image)
    if argo.get("revision_matches") is False and runtime.get("image_matches") is True:
        argo = {
            **argo,
            "ok": True,
            "equivalent_image_match": True,
        }
    runtime_image_ready = runtime.get("image_matches") is not False
    # The live card/skills probe cold-starts a scale-to-zero agent. Defer it
    # until the runtime confirms the expected image is reconciled and the
    # Knative Service reports Ready. ArgoCD remains useful operator telemetry,
    # but Knative apps can stay OutOfSync after a healthy rollout, so it is not
    # a live-readiness gate.
    runtime_ready = bool(runtime.get("ok") and runtime_image_ready)
    if runtime_ready:
        live_http = await _safe_agent_http(agent.name)
    else:
        skipped = {"ok": False, "skipped": True, "reason": "runtime_not_ready"}
        live_http = {"health": dict(skipped), "card": dict(skipped)}
    card = live_http["card"]
    skills = _skills_summary(card.get("body") if isinstance(card, dict) else None)
    return {
        "live": bool(
            runtime.get("ok")
            and runtime_image_ready
            and card.get("ok")
            and skills.get("ok")
        ),
        "url": agent.url or None,
        "argo": argo,
        "runtime": runtime,
        "health": live_http["health"],
        "agent_card": card,
        "skills": skills,
    }


async def deployment_events(
    session: AsyncSession,
    deploy: AgentDeployment,
) -> list[AgentDeploymentEvent]:
    return (
        await session.execute(
            select(AgentDeploymentEvent)
            .where(AgentDeploymentEvent.deployment_id == deploy.id)
            .order_by(AgentDeploymentEvent.id)
        )
    ).scalars().all()


async def latest_deployment_for_agent(
    session: AsyncSession,
    agent: Agent,
) -> AgentDeployment | None:
    return (
        await session.execute(
            select(AgentDeployment)
            .where(
                AgentDeployment.agent_name == agent.name,
                AgentDeployment.user_id == agent.owner_id,
            )
            .order_by(desc(AgentDeployment.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _database_resources_verification(
    session: AsyncSession,
    agent: Agent,
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(AgentDatabaseBinding).where(
                AgentDatabaseBinding.agent_id == agent.id,
                AgentDatabaseBinding.status != "removed",
            )
        )
    ).scalars().all()
    counts = {"ready": 0, "pending": 0, "provisioning": 0, "failed": 0}
    for row in rows:
        status = str(row.status or "pending")
        if status in counts:
            counts[status] += 1
        else:
            counts["pending"] += 1
    return {
        "ok": not rows or counts["ready"] == len(rows),
        "required": bool(rows),
        "total": len(rows),
        **counts,
    }


def _database_message(
    database: dict[str, Any],
    *,
    terminal_failure: bool = False,
) -> str:
    if not database.get("required"):
        return "Agent declares no managed databases."
    if database.get("ok"):
        return f"All {database.get('ready', 0)} managed database binding(s) are ready."
    if database.get("failed") and terminal_failure:
        return f"{database.get('failed')} managed database binding(s) failed provisioning."
    if database.get("failed"):
        return "Managed database provisioning is retrying after a failed attempt."
    return "Waiting for managed database provisioning and migrations to finish."


async def _expected_deployment_signals(
    session: AsyncSession,
    agent: Agent,
    deploy: AgentDeployment,
) -> dict[str, Any]:
    events = await deployment_events(session, deploy)
    runtime_revision: str | None = None
    expected_image: str | None = None
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        runtime_revision = (
            _string_or_none(data.get("expected_revision"))
            or _string_or_none(data.get("runtime_head_sha"))
            or runtime_revision
        )
        expected_image = _string_or_none(data.get("expected_image")) or expected_image

    if expected_image is None:
        expected_image = _expected_image_for_deploy(agent, deploy)

    expected: dict[str, Any] = {}
    if runtime_revision:
        expected["runtime_revision"] = runtime_revision
    if expected_image:
        expected["image"] = expected_image
    if deploy.head_sha:
        expected["source_sha"] = deploy.head_sha
    return expected


def _expected_image_for_deploy(agent: Agent, deploy: AgentDeployment) -> str | None:
    image = deploy.image or agent.image
    if _is_pinned_managed_agent_image(image, agent.name):
        return image
    if deploy.head_sha and _is_managed_agent_image(image, agent.name):
        return f"registry.a2acloud.io/agents/{agent.name}:{deploy.head_sha}"
    return image or None


def _is_pinned_managed_agent_image(image: str | None, agent_name: str) -> bool:
    if not image:
        return False
    prefix = f"registry.a2acloud.io/agents/{agent_name}:"
    if not image.startswith(prefix):
        return False
    return image.removeprefix(prefix) != "latest"


def _is_managed_agent_image(image: str | None, agent_name: str) -> bool:
    if not image:
        return False
    return image.startswith(f"registry.a2acloud.io/agents/{agent_name}:")


async def _upsert_progress_event(
    session: AsyncSession,
    deploy: AgentDeployment,
    *,
    stage: str,
    status: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    previous = (
        await session.execute(
            select(AgentDeploymentEvent)
            .where(
                AgentDeploymentEvent.deployment_id == deploy.id,
                AgentDeploymentEvent.stage == stage,
            )
            .order_by(desc(AgentDeploymentEvent.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    payload = data or {}
    if (
        previous is not None
        and previous.status == status
        and previous.message == message
    ):
        previous.data = payload
        return
    session.add(
        AgentDeploymentEvent(
            deployment_id=deploy.id,
            deploy_id=deploy.deploy_id,
            agent_name=deploy.agent_name,
            stage=stage,
            status=status,
            message=message,
            data=payload,
        )
    )


def _deployment_timed_out(deploy: AgentDeployment) -> bool:
    return _deployment_age_seconds(deploy) > DEPLOY_TIMEOUT_SECONDS


def _missing_argo_application(
    deploy: AgentDeployment,
    verification: dict[str, Any],
) -> bool:
    if deploy.status not in ACTIVE_DEPLOY_STATUSES:
        return False
    if verification.get("argo", {}).get("exists") is not False:
        return False
    return _deployment_age_seconds(deploy) > MISSING_ARGO_GRACE_SECONDS


def _deployment_age_seconds(deploy: AgentDeployment) -> float:
    started = deploy.started_at or deploy.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (utcnow() - started).total_seconds()


def _deployment_failure_reason(*, timed_out: bool, missing_argo: bool) -> str:
    if missing_argo:
        return (
            "ArgoCD application is missing; deployment infrastructure was not "
            "created or was deleted."
        )
    if timed_out:
        return "Timed out waiting for the agent to become live."
    return "Deployment verification failed."


def _preserve_control_plane_card_metadata(
    existing_card: Any,
    live_card: dict[str, Any],
) -> dict[str, Any]:
    """Keep deploy-time control-plane metadata absent from runtime cards."""
    if not isinstance(existing_card, dict):
        return live_card
    merged = dict(live_card)
    if "consumer_setup" not in merged and "consumer_setup" in existing_card:
        merged["consumer_setup"] = existing_card["consumer_setup"]
    return merged


def _derived_status(verification: dict[str, Any]) -> str:
    if verification.get("agent_card", {}).get("ok"):
        return "verifying"
    if verification.get("runtime", {}).get("ok") or verification.get("argo", {}).get("ok"):
        return "deploying"
    return "building"


def _status_from_bool(ok: Any, timed_out: bool) -> str:
    if ok is True:
        return "passed"
    if timed_out:
        return "failed"
    return "running"


def _safe_argo(
    agent_name: str,
    *,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    try:
        summary = application_summary(agent_name)
        revision = _string_or_none(summary.get("revision"))
        revision_matches = (
            None if expected_revision is None else revision == expected_revision
        )
        return {
            **summary,
            "expected_revision": expected_revision,
            "revision_matches": revision_matches,
            "ok": bool(
                summary.get("exists")
                and summary.get("health") in ("Healthy", "Progressing", None)
                and revision_matches is not False
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exists": None, "error": _short_error(exc)}


def _safe_runtime(
    agent_name: str,
    *,
    expected_image: str | None = None,
) -> dict[str, Any]:
    try:
        _load_kube()
        apps = client.AppsV1Api()
        dep = apps.read_namespaced_deployment(agent_name, settings.agents_namespace)
        status = dep.status
        desired = status.replicas or 0
        available = status.available_replicas or 0
        updated = status.updated_replicas or 0
        ready = status.ready_replicas or 0
        images, agent_image_env = _deployment_images(dep)
        image_matches = _image_matches(expected_image, images, agent_image_env)
        return {
            "ok": available >= 1 and ready >= 1 and image_matches is not False,
            "kind": "deployment",
            "desired": desired,
            "available": available,
            "updated": updated,
            "ready": ready,
            "images": images,
            "agent_image_env": agent_image_env,
            "expected_image": expected_image,
            "image_matches": image_matches,
        }
    except ApiException as exc:
        if exc.status != 404:
            return {"ok": False, "kind": "deployment", "error": _short_error(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": "deployment", "error": _short_error(exc)}

    try:
        _load_kube()
        custom = client.CustomObjectsApi()
        ksvc = custom.get_namespaced_custom_object(
            KNATIVE_API_GROUP,
            KNATIVE_API_VERSION,
            settings.agents_namespace,
            KNATIVE_SERVICES_PLURAL,
            agent_name,
        )
        status = ksvc.get("status") if isinstance(ksvc, dict) else {}
        conditions = status.get("conditions") if isinstance(status, dict) else []
        ready = _condition_status(conditions, "Ready")
        images, agent_image_env = _knative_images(ksvc)
        image_matches = _image_matches(expected_image, images, agent_image_env)
        return {
            "ok": ready == "True" and image_matches is not False,
            "kind": "knative_service",
            "ready": ready,
            "url": status.get("url") if isinstance(status, dict) else None,
            "latest_created_revision": status.get("latestCreatedRevisionName")
            if isinstance(status, dict)
            else None,
            "latest_ready_revision": status.get("latestReadyRevisionName")
            if isinstance(status, dict)
            else None,
            "images": images,
            "agent_image_env": agent_image_env,
            "expected_image": expected_image,
            "image_matches": image_matches,
        }
    except ApiException as exc:
        if exc.status == 404:
            return {"ok": False, "kind": "deployment", "error": "deployment not found"}
        return {"ok": False, "kind": "knative_service", "error": _short_error(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _short_error(exc)}


def _condition_status(conditions: Any, condition_type: str) -> str | None:
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if (
            isinstance(condition, dict)
            and condition.get("type") == condition_type
        ):
            return condition.get("status")
    return None


def _deployment_images(dep: Any) -> tuple[list[str], str | None]:
    template = getattr(getattr(dep, "spec", None), "template", None)
    pod_spec = getattr(template, "spec", None)
    containers = getattr(pod_spec, "containers", None)
    return _container_images(containers)


def _knative_images(ksvc: Any) -> tuple[list[str], str | None]:
    if not isinstance(ksvc, dict):
        return [], None
    spec = ksvc.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
    return _container_images(containers)


def _container_images(containers: Any) -> tuple[list[str], str | None]:
    images: list[str] = []
    agent_image_env: str | None = None
    if not isinstance(containers, list):
        return images, agent_image_env
    for container in containers:
        image = _container_value(container, "image")
        if image:
            images.append(image)
        env = _container_value(container, "env")
        if not isinstance(env, list):
            continue
        for item in env:
            name = _container_value(item, "name")
            value = _container_value(item, "value")
            if name == "A2A_AGENT_IMAGE" and isinstance(value, str) and value:
                agent_image_env = value
    return images, agent_image_env


def _container_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _image_matches(
    expected_image: str | None,
    images: list[str],
    agent_image_env: str | None,
) -> bool | None:
    if not expected_image:
        return None
    def same_image(actual: str | None) -> bool:
        if not actual:
            return False
        # Kubernetes resolves a tagged image to ``tag@sha256:digest``. That is
        # the same immutable artifact, not a mismatch against the requested tag.
        return actual == expected_image or actual.split("@sha256:", 1)[0] == expected_image

    return any(same_image(image) for image in images) or same_image(agent_image_env)


async def _safe_agent_http(agent_name: str) -> dict[str, dict[str, Any]]:
    base = f"http://{agent_name}.agents.svc.cluster.local"
    health: dict[str, Any] = {"ok": False}
    card: dict[str, Any] = {"ok": False}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            health_resp = await client.get(f"{base}/healthz")
            health = {
                "ok": health_resp.status_code < 400,
                "status_code": health_resp.status_code,
            }
            card_resp = await client.get(f"{base}/.well-known/agent-card")
            body: Any = None
            if card_resp.status_code < 400:
                try:
                    body = card_resp.json()
                except ValueError:
                    body = None
            card = {
                "ok": card_resp.status_code < 400 and isinstance(body, dict),
                "status_code": card_resp.status_code,
                "body": body if isinstance(body, dict) else None,
            }
    except Exception as exc:  # noqa: BLE001
        err = _short_error(exc)
        health = {"ok": False, "error": err}
        card = {"ok": False, "error": err}
    return {"health": health, "card": card}


def _skills_summary(card: Any) -> dict[str, Any]:
    skills = card.get("skills") if isinstance(card, dict) else None
    if not isinstance(skills, list):
        return {"ok": False, "count": 0, "names": []}
    names = [
        str(skill.get("name"))
        for skill in skills
        if isinstance(skill, dict) and skill.get("name")
    ]
    return {"ok": len(names) > 0, "count": len(names), "names": names[:20]}


def _argo_message(argo: dict[str, Any]) -> str:
    if argo.get("equivalent_image_match"):
        return "ArgoCD revision is stale, but the runtime is serving the expected image."
    if argo.get("ok"):
        sync = argo.get("sync") or "watching"
        health = argo.get("health") or "unknown"
        return f"ArgoCD is {sync}; app health is {health}."
    if argo.get("revision_matches") is False:
        expected = argo.get("expected_revision")
        revision = argo.get("revision") or "unknown"
        return (
            "ArgoCD is still on runtime revision "
            f"{str(revision)[:12]}; waiting for {str(expected)[:12]}."
        )
    if argo.get("error"):
        return f"ArgoCD status unavailable: {argo['error']}"
    if argo.get("exists") is False:
        return "ArgoCD application has not been created yet."
    return "Waiting for ArgoCD to reconcile the deploy manifests."


def _runtime_message(runtime: dict[str, Any]) -> str:
    if runtime.get("image_matches") is False:
        expected = runtime.get("expected_image")
        images = runtime.get("images") if isinstance(runtime.get("images"), list) else []
        live = runtime.get("agent_image_env") or (images[0] if images else "unknown image")
        return f"Runtime is still serving {live}; waiting for {expected}."
    if runtime.get("ok"):
        if runtime.get("kind") == "knative_service":
            rev = runtime.get("latest_ready_revision") or "latest revision"
            return f"Knative Service is ready ({rev})."
        return f"Runtime pod is ready ({runtime.get('ready', 1)} ready)."
    if runtime.get("error"):
        return f"Runtime status unavailable: {runtime['error']}"
    if runtime.get("kind") == "knative_service":
        ready = runtime.get("ready") or "Unknown"
        return f"Waiting for Knative Service to become ready (Ready={ready})."
    return "Waiting for runtime to become ready."


def _agent_card_message(card: dict[str, Any]) -> str:
    if card.get("ok"):
        return "Agent card is reachable from the cluster."
    if card.get("skipped"):
        return "Waiting for the runtime to reconcile before validating the agent card."
    if card.get("status_code"):
        return f"Agent card returned HTTP {card['status_code']}."
    if card.get("error"):
        return f"Agent card is not reachable yet: {card['error']}"
    return "Waiting for the agent card endpoint."


def _skills_message(skills: dict[str, Any]) -> str:
    if skills.get("ok"):
        count = skills.get("count", 0)
        return f"{count} callable skill{'s' if count != 1 else ''} detected."
    return "Waiting for the agent to expose callable skills."


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:240]


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None
