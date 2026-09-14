"""Owner-scoped adversarial reviewer-loop process endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent_review import _classify_status
from ..auth import current_user
from ..db import get_session
from ..models import Agent, AgentReviewRun, User, WorkJob
from ..work_ledger import (
    append_event,
    create_job,
    fail_job,
    list_job_events,
    serialize_event,
    serialize_job,
)

router = APIRouter(prefix="/v1/agents", tags=["adversarial-review-loops"])

REVIEW_LOOP_KIND = "adversarial_review_loop"
REVIEW_LOOP_TEMPLATE_REF = "adversarial_review_loop@v1"
DEFAULT_REVIEWER_AGENT = "agent-reviewer"
_TERMINAL_STATUSES = {"complete", "completed", "error", "failed", "cancelled", "canceled", "killed"}
_FINDING_EVENT_TYPES = {"finding_emitted"}
_FORBIDDEN_TRUTHY_KEYS = {
    "active_apply_enabled",
    "can_mutate",
    "direct_apply",
    "direct_apply_requested",
    "evaluator_can_apply_changes",
    "mutation_applied",
    "reviewer_can_mutate",
}
_FORBIDDEN_NONEMPTY_KEYS = {
    "budget_writes",
    "direct_apply_surfaces",
    "file_ops",
    "manifest_writes",
    "memory_writes",
    "policy_writes",
    "route_writes",
    "source_writes",
    "write_grants",
}


class ReviewLoopIn(BaseModel):
    reviewer_agent_name: str = Field(default=DEFAULT_REVIEWER_AGENT, min_length=1, max_length=128)
    ref: str | None = Field(default=None, max_length=128)
    head_sha: str | None = Field(default=None, max_length=128)
    loop_budget_cents: int = Field(default=300, ge=0)
    budget_ceiling_cents: int = Field(default=500, gt=0)
    ttl_seconds: int = Field(default=900, gt=0, le=86_400)
    max_iterations: int = Field(default=3, gt=0, le=50)
    canary_plan: str | None = Field(default=None, max_length=500)
    rollback_plan: str | None = Field(
        default="stop review loop and discard pending fix proposals",
        max_length=500,
    )


class ReviewLoopStopIn(BaseModel):
    reason: str = Field(default="owner requested stop", min_length=1, max_length=500)


class ReviewLoopFindingIn(BaseModel):
    severity: Literal["critical", "warning", "info"] = "info"
    title: str = Field(min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=1000)
    category: str | None = Field(default=None, max_length=100)
    file: str | None = Field(default=None, max_length=500)
    line: int | None = Field(default=None, ge=1)
    recommendation: str | None = Field(default=None, max_length=1000)
    finding_hash: str | None = Field(default=None, max_length=128)


class ReviewLoopEventIn(BaseModel):
    event_type: Literal[
        "reviewer_started",
        "finding_emitted",
        "fix_proposed",
        "loop_completed",
        "loop_failed",
    ]
    message: str | None = Field(default=None, max_length=1000)
    iteration: int | None = Field(default=None, ge=1, le=10_000)
    review_id: str | None = Field(default=None, max_length=64)
    findings: list[ReviewLoopFindingIn] = Field(default_factory=list)
    finding_ref: str | None = Field(default=None, max_length=200)
    proposal_id: str | None = Field(default=None, max_length=200)
    self_improvement_proposal_ref: str | None = Field(default=None, max_length=200)
    cost_cents: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ReviewLoopOut(BaseModel):
    job: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)


async def _assert_caller_owns_agent(
    session: AsyncSession,
    user: User,
    name: str,
) -> Agent:
    agent = (
        await session.execute(select(Agent).where(Agent.name == name))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, f"agent {name!r} not found")
    if agent.owner_id != user.id:
        raise HTTPException(403, "not the owner of this agent")
    return agent


def _loop_payload(agent: Agent, body: ReviewLoopIn) -> dict[str, Any]:
    target_ref = body.head_sha or body.ref or str(agent.card.get("version") or "current")
    return {
        "target_agent": agent.name,
        "target_refs": [f"agent:{agent.name}", f"ref:{target_ref}"],
        "reviewer_agent": body.reviewer_agent_name,
        "ref": body.ref,
        "head_sha": body.head_sha,
        "findings_only_required": True,
        "direct_apply_forbidden": True,
        "fixes_enter_self_improvement_required": True,
        "promotion_freeze_on_critical": True,
        "loop_limits": {
            "loop_budget_cents": body.loop_budget_cents,
            "budget_ceiling_cents": body.budget_ceiling_cents,
            "ttl_seconds": body.ttl_seconds,
            "max_iterations": body.max_iterations,
        },
        "canary_plan": body.canary_plan,
        "rollback_plan": body.rollback_plan,
    }


def _loop_metadata(body: ReviewLoopIn) -> dict[str, Any]:
    return {
        "template_ref": REVIEW_LOOP_TEMPLATE_REF,
        "source_document": "docs/design/capability-graph/emergent-readiness-matrix.md",
        "source_section": "E4 Adversarial Reviewer Loop",
        "reviewer_agent_name": body.reviewer_agent_name,
        "budget_ceiling_cents": body.budget_ceiling_cents,
        "loop_budget_cents": body.loop_budget_cents,
        "ttl_seconds": body.ttl_seconds,
        "max_iterations": body.max_iterations,
        "kill_switch_available": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _walk_payload(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((str(key), item))
            found.extend(_walk_payload(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_payload(item))
    return found


def _reject_direct_apply_payload(payload: dict[str, Any]) -> None:
    for key, value in _walk_payload(payload):
        normalized = key.lower()
        if normalized in _FORBIDDEN_TRUTHY_KEYS and bool(value):
            raise HTTPException(
                400,
                f"review loop events are findings-only; {key!r} cannot be truthy",
            )
        if normalized in _FORBIDDEN_NONEMPTY_KEYS and bool(value):
            raise HTTPException(
                400,
                f"review loop events cannot carry direct mutation grants: {key!r}",
            )


def _metadata_int(job: WorkJob, key: str, default: int = 0) -> int:
    try:
        return int((job.metadata_json or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


async def _enforce_loop_bounds(
    session: AsyncSession,
    job: WorkJob,
    body: ReviewLoopEventIn,
    *,
    user_id: int,
) -> list[dict[str, Any]]:
    events = await _loop_events(session, job, user_id=user_id)
    now = datetime.now(timezone.utc)
    ttl_seconds = _metadata_int(job, "ttl_seconds")
    if ttl_seconds and (now - _as_utc(job.created_at)).total_seconds() > ttl_seconds:
        await fail_job(
            session,
            job,
            error="review loop TTL expired",
            result={
                "reason": "ttl_expired",
                "ttl_seconds": ttl_seconds,
                "active_apply_enabled": False,
            },
            summary="review loop TTL expired",
            user_id=user_id,
            status="killed",
            event_type="loop_ttl_expired",
            commit=True,
        )
        raise HTTPException(409, "review loop TTL expired")

    max_iterations = _metadata_int(job, "max_iterations")
    if body.event_type in _FINDING_EVENT_TYPES:
        completed_iterations = sum(
            1 for event in events if event.get("event_type") in _FINDING_EVENT_TYPES
        )
        if max_iterations and completed_iterations >= max_iterations:
            await fail_job(
                session,
                job,
                error="review loop max iterations exceeded",
                result={
                    "reason": "max_iterations_exceeded",
                    "max_iterations": max_iterations,
                    "active_apply_enabled": False,
                },
                summary="review loop max iterations exceeded",
                user_id=user_id,
                status="killed",
                event_type="loop_iteration_limit_exceeded",
                commit=True,
            )
            raise HTTPException(409, "review loop max iterations exceeded")

    loop_budget = _metadata_int(job, "loop_budget_cents")
    budget_ceiling = _metadata_int(job, "budget_ceiling_cents")
    spent = sum(int((event.get("payload") or {}).get("cost_cents") or 0) for event in events)
    if body.cost_cents and (
        (loop_budget and spent + body.cost_cents > loop_budget)
        or (budget_ceiling and spent + body.cost_cents > budget_ceiling)
    ):
        await fail_job(
            session,
            job,
            error="review loop budget exceeded",
            result={
                "reason": "budget_exceeded",
                "spent_cents": spent,
                "event_cost_cents": body.cost_cents,
                "loop_budget_cents": loop_budget,
                "budget_ceiling_cents": budget_ceiling,
                "active_apply_enabled": False,
            },
            summary="review loop budget exceeded",
            user_id=user_id,
            status="killed",
            event_type="loop_budget_exceeded",
            commit=True,
        )
        raise HTTPException(409, "review loop budget exceeded")
    return events


def _finding_dicts(body: ReviewLoopEventIn) -> list[dict[str, Any]]:
    return [finding.model_dump(exclude_none=True) for finding in body.findings]


async def _upsert_review_run(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    job: WorkJob,
    body: ReviewLoopEventIn,
) -> tuple[str, str, int, int, int]:
    findings = _finding_dicts(body)
    review_id = body.review_id or f"{job.job_id}-review-{len(findings) or 1}"
    report = {"findings": findings}
    status, critical, warning, info = _classify_status(report)
    summary = body.message or (
        f"{critical} critical, {warning} warning, {info} info findings"
    )
    row = (
        await session.execute(
            select(AgentReviewRun).where(AgentReviewRun.review_id == review_id)
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgentReviewRun(
            review_id=review_id,
            agent_id=agent.id,
            agent_name=agent.name,
            ref=str((job.input_payload or {}).get("head_sha") or (job.input_payload or {}).get("ref") or "current"),
            user_id=user.id,
            status=status,
            summary=summary,
            findings=findings,
            critical_count=critical,
            warning_count=warning,
            info_count=info,
            started_at=_as_utc(job.started_at or job.created_at),
            completed_at=datetime.now(timezone.utc),
        )
        session.add(row)
    else:
        row.agent_id = agent.id
        row.agent_name = agent.name
        row.user_id = user.id
        row.status = status
        row.summary = summary
        row.findings = findings
        row.critical_count = critical
        row.warning_count = warning
        row.info_count = info
        row.completed_at = datetime.now(timezone.utc)
    await session.flush()
    return review_id, status, critical, warning, info


async def _append_loop_event(
    session: AsyncSession,
    job: WorkJob,
    *,
    event_type: str,
    payload: dict[str, Any],
    message: str,
    status: str,
    stage: str,
    severity: str = "info",
    user: User,
    source_id: str | None,
    commit: bool = False,
) -> None:
    await append_event(
        session,
        job,
        event_type=event_type,
        payload={**payload, "active_apply_enabled": False},
        message=message,
        status=status,
        stage=stage,
        severity=severity,
        user_id=user.id,
        actor_type="agent" if source_id else "user",
        actor_id=source_id or str(user.id),
        source_type="agent" if source_id else None,
        source_id=source_id,
        commit=commit,
    )


async def _loop_events(
    session: AsyncSession,
    job: WorkJob,
    *,
    user_id: int,
) -> list[dict[str, Any]]:
    rows = await list_job_events(session, job.job_id, user_id=user_id)
    return [serialize_event(row) for row in rows]


async def _loop_out(
    session: AsyncSession,
    job: WorkJob,
    *,
    user_id: int,
) -> ReviewLoopOut:
    return ReviewLoopOut(
        job=serialize_job(job),
        events=await _loop_events(session, job, user_id=user_id),
    )


async def _get_owned_loop(
    session: AsyncSession,
    user: User,
    agent_name: str,
    job_id: str,
) -> WorkJob:
    await _assert_caller_owns_agent(session, user, agent_name)
    row = (
        await session.execute(
            select(WorkJob).where(
                WorkJob.job_id == job_id,
                WorkJob.user_id == user.id,
                WorkJob.kind == REVIEW_LOOP_KIND,
                WorkJob.subject_type == "agent",
                WorkJob.subject_id == agent_name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "review loop not found")
    return row


@router.post("/{name}/review-loops", response_model=ReviewLoopOut, status_code=201)
async def create_review_loop(
    name: str,
    body: ReviewLoopIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewLoopOut:
    if body.loop_budget_cents > body.budget_ceiling_cents:
        raise HTTPException(400, "loop budget exceeds budget ceiling")
    agent = await _assert_caller_owns_agent(session, user, name)
    job_id = f"arl-{uuid4().hex[:16]}"
    payload = _loop_payload(agent, body)
    job = await create_job(
        session,
        user_id=user.id,
        kind=REVIEW_LOOP_KIND,
        payload=payload,
        title=f"Adversarial review loop: {agent.name}",
        metadata=_loop_metadata(body),
        job_id=job_id,
        status="queued",
        queue="reviewer-loops",
        priority=10,
        correlation_id=job_id,
        source_type="agent",
        source_id=body.reviewer_agent_name,
        subject_type="agent",
        subject_id=agent.name,
        worker_type="agent",
        worker_name=body.reviewer_agent_name,
        idempotency_scope="adversarial_review_loop",
        record_event=False,
        commit=False,
    )
    await append_event(
        session,
        job,
        event_type="loop_created",
        payload={
            "template_ref": REVIEW_LOOP_TEMPLATE_REF,
            "target_agent": agent.name,
            "reviewer_agent": body.reviewer_agent_name,
            "source_document": "emergent-readiness-matrix.md:E4",
            "findings_only": True,
            "active_apply_enabled": False,
        },
        message=f"adversarial reviewer loop created for {agent.name}",
        status="queued",
        stage="created",
        user_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        source_type="agent",
        source_id=body.reviewer_agent_name,
        commit=False,
    )
    await session.commit()
    await session.refresh(job)
    return await _loop_out(session, job, user_id=user.id)


@router.post("/{name}/review-loops/{job_id}/events", response_model=ReviewLoopOut)
async def record_review_loop_event(
    name: str,
    job_id: str,
    body: ReviewLoopEventIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewLoopOut:
    row = await _get_owned_loop(session, user, name, job_id)
    if row.status in _TERMINAL_STATUSES:
        raise HTTPException(409, "review loop is already terminal")
    agent = await _assert_caller_owns_agent(session, user, name)
    _reject_direct_apply_payload(
        {
            "payload": body.payload,
            "findings": _finding_dicts(body),
            "proposal_id": body.proposal_id,
            "self_improvement_proposal_ref": body.self_improvement_proposal_ref,
        }
    )
    await _enforce_loop_bounds(session, row, body, user_id=user.id)

    reviewer_name = str((row.metadata_json or {}).get("reviewer_agent_name") or row.worker_name or "")
    base_payload = {
        **body.payload,
        "iteration": body.iteration,
        "cost_cents": body.cost_cents,
        "target_agent": agent.name,
        "reviewer_agent": reviewer_name,
        "findings_only": True,
        "proposal_only": True,
    }

    if body.event_type == "reviewer_started":
        row.status = "blocked" if row.status == "blocked" else "running"
        row.started_at = row.started_at or datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        await _append_loop_event(
            session,
            row,
            event_type="reviewer_started",
            payload=base_payload,
            message=body.message or "adversarial reviewer started",
            status="running",
            stage="review",
            user=user,
            source_id=reviewer_name,
        )
    elif body.event_type == "finding_emitted":
        if not body.findings:
            raise HTTPException(400, "finding_emitted requires at least one finding")
        review_id, review_status, critical, warning, info = await _upsert_review_run(
            session,
            agent=agent,
            user=user,
            job=row,
            body=body,
        )
        row.status = "blocked" if critical else "running"
        row.summary = body.message or f"review loop findings: {review_status}"
        row.output_payload = {
            **(row.output_payload or {}),
            "latest_review_id": review_id,
            "latest_review_status": review_status,
            "critical_finding_count": critical,
            "warning_finding_count": warning,
            "info_finding_count": info,
            "active_apply_enabled": False,
        }
        row.updated_at = datetime.now(timezone.utc)
        await _append_loop_event(
            session,
            row,
            event_type="finding_emitted",
            payload={
                **base_payload,
                "review_id": review_id,
                "review_status": review_status,
                "findings": _finding_dicts(body),
            },
            message=body.message or f"reviewer emitted {len(body.findings)} findings",
            status="failed" if critical else review_status,
            stage="findings",
            severity="critical" if critical else ("warning" if warning else "info"),
            user=user,
            source_id=reviewer_name,
        )
        if critical:
            await _append_loop_event(
                session,
                row,
                event_type="promotion_frozen",
                payload={
                    "target_agent": agent.name,
                    "review_id": review_id,
                    "critical_finding_count": critical,
                    "reason": "critical reviewer finding",
                    "promotion_frozen": True,
                    "proposal_only": True,
                },
                message="promotion frozen due to critical reviewer finding",
                status="blocked",
                stage="promotion_gate",
                severity="critical",
                user=user,
                source_id=reviewer_name,
            )
    elif body.event_type == "fix_proposed":
        proposal_ref = body.self_improvement_proposal_ref or body.proposal_id
        if not proposal_ref:
            raise HTTPException(
                400,
                "fix_proposed requires self_improvement_proposal_ref or proposal_id",
            )
        row.status = "running"
        row.output_payload = {
            **(row.output_payload or {}),
            "proposed_fix_refs": sorted(
                set((row.output_payload or {}).get("proposed_fix_refs") or []) | {proposal_ref}
            ),
            "active_apply_enabled": False,
        }
        row.updated_at = datetime.now(timezone.utc)
        await _append_loop_event(
            session,
            row,
            event_type="fix_proposed",
            payload={
                **base_payload,
                "finding_ref": body.finding_ref,
                "self_improvement_proposal_ref": proposal_ref,
                "fixes_enter_self_improvement": True,
            },
            message=body.message or "reviewer proposed a fix through self-improvement",
            status="queued",
            stage="proposal",
            user=user,
            source_id=reviewer_name,
        )
    elif body.event_type == "loop_completed":
        row.status = "complete"
        row.summary = body.message or "review loop completed"
        row.output_payload = {
            **(row.output_payload or {}),
            **base_payload,
            "active_apply_enabled": False,
        }
        row.completed_at = datetime.now(timezone.utc)
        row.updated_at = row.completed_at
        await _append_loop_event(
            session,
            row,
            event_type="loop_completed",
            payload=base_payload,
            message=row.summary,
            status="complete",
            stage="complete",
            user=user,
            source_id=reviewer_name,
        )
    else:
        row = await fail_job(
            session,
            row,
            error=body.message or "review loop failed",
            result={**base_payload, "active_apply_enabled": False},
            summary=body.message or "review loop failed",
            user_id=user.id,
            status="failed",
            event_type="loop_failed",
            commit=False,
        )

    await session.commit()
    await session.refresh(row)
    return await _loop_out(session, row, user_id=user.id)


@router.get("/{name}/review-loops", response_model=list[ReviewLoopOut])
async def list_review_loops(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ReviewLoopOut]:
    await _assert_caller_owns_agent(session, user, name)
    rows = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user.id,
                WorkJob.kind == REVIEW_LOOP_KIND,
                WorkJob.subject_type == "agent",
                WorkJob.subject_id == name,
            )
            .order_by(desc(WorkJob.created_at), desc(WorkJob.id))
            .limit(limit)
        )
    ).scalars().all()
    return [await _loop_out(session, row, user_id=user.id) for row in rows]


@router.get("/{name}/review-loops/{job_id}", response_model=ReviewLoopOut)
async def get_review_loop(
    name: str,
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewLoopOut:
    row = await _get_owned_loop(session, user, name, job_id)
    return await _loop_out(session, row, user_id=user.id)


@router.post("/{name}/review-loops/{job_id}/stop", response_model=ReviewLoopOut)
async def stop_review_loop(
    name: str,
    job_id: str,
    body: ReviewLoopStopIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReviewLoopOut:
    row = await _get_owned_loop(session, user, name, job_id)
    if row.status not in _TERMINAL_STATUSES:
        row = await fail_job(
            session,
            row,
            error=body.reason,
            result={
                "reason": body.reason,
                "kill_switch": True,
                "active_apply_enabled": False,
            },
            summary=body.reason,
            user_id=user.id,
            status="killed",
            event_type="loop_killed",
            commit=True,
        )
    return await _loop_out(session, row, user_id=user.id)
