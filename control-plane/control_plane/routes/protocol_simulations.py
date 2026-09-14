"""Owner-scoped protocol simulation process endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user, current_user_or_agent_invoke
from ..custom_kernel_simulations import CustomSimulationError, run_custom_kernel_simulation
from ..custom_kernel_suites import (
    list_custom_kernel_suite_templates,
    render_custom_kernel_suite_template,
    run_custom_kernel_suite,
)
from ..custom_kernel_templates import list_custom_kernel_templates, render_custom_kernel_template
from ..db import get_session
from ..graph_kernel_scenarios import (
    ScenarioRunError,
    graph_kernel_protocol_class,
    list_graph_kernel_scenarios,
    run_graph_kernel_scenarios,
)
from ..models import Agent, User, WorkJob
from ..protocol_registry import list_persistent_protocol_registry
from ..kernel_traces import KernelTraceRecordedPayload
from ..protocol_simulation import (
    PROTOCOL_SIMULATION_KIND,
    PROTOCOL_SIMULATION_TEMPLATE_REF,
    evaluate_active_runtime_readiness,
    protocol_ref_payload,
    protocol_simulation_metadata,
    summarize_scenario_trace_payload,
)
from ..work_ledger import append_event, create_job, fail_job, list_job_events, serialize_event, serialize_job

router = APIRouter(prefix="/v1/agents", tags=["protocol-simulations"])

_TERMINAL_STATUSES = {"complete", "completed", "error", "failed", "cancelled", "canceled", "killed"}
_EPISODE_EVENT_TYPES = {"episode_started", "attempt_scored"}
_FORBIDDEN_TRUTHY_KEYS = {
    "active_apply_enabled",
    "apply_rewrite",
    "can_mutate",
    "direct_apply",
    "direct_apply_requested",
    "mutation_applied",
    "policy_override",
}
_FORBIDDEN_NONEMPTY_KEYS = {
    "budget_writes",
    "credential_writes",
    "direct_apply_surfaces",
    "file_ops",
    "manifest_writes",
    "marketplace_writes",
    "memory_writes",
    "policy_writes",
    "route_writes",
    "source_writes",
    "write_grants",
}


class ProtocolSimulationIn(BaseModel):
    protocol_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    protocol_version: int = Field(default=1, ge=1, le=10_000)
    display_name: str | None = Field(default=None, max_length=160)
    template_ref: str = Field(default=PROTOCOL_SIMULATION_TEMPLATE_REF, min_length=1, max_length=160)
    simulation_goal: str = Field(min_length=1, max_length=2000)
    roles: dict[str, str] = Field(default_factory=dict)
    invariants: list[str] = Field(default_factory=list, max_length=50)
    signals_consumed: list[str] = Field(default_factory=list, max_length=50)
    signals_emitted: list[str] = Field(default_factory=list, max_length=50)
    run_budget_cents: int = Field(default=300, ge=0)
    budget_ceiling_cents: int = Field(default=500, gt=0)
    ttl_seconds: int = Field(default=900, gt=0, le=86_400)
    max_episodes: int = Field(default=5, gt=0, le=100)


class ProtocolSimulationStopIn(BaseModel):
    reason: str = Field(default="owner requested stop", min_length=1, max_length=500)


class ProtocolSimulationEventIn(BaseModel):
    event_type: Literal[
        "simulation_started",
        "episode_started",
        "signal_emitted",
        "invariant_checked",
        "attempt_scored",
        "proposal_emitted",
        "simulation_completed",
        "simulation_failed",
    ]
    message: str | None = Field(default=None, max_length=1000)
    episode: int | None = Field(default=None, ge=1, le=100_000)
    signal_type: str | None = Field(default=None, max_length=128)
    invariant: str | None = Field(default=None, max_length=500)
    score: float | None = Field(default=None, ge=0)
    proposal_ref: str | None = Field(default=None, max_length=200)
    cost_cents: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ProtocolScenarioRunIn(BaseModel):
    scenario_ids: list[str] = Field(default_factory=list, max_length=25)
    cost_cents: int = Field(default=0, ge=0)


class ProtocolRuntimeReadinessIn(BaseModel):
    requested: bool = True
    simulation_passed: bool = False
    policy_reviewed: bool = False
    owner_approved: bool = False
    operator_enabled: bool = False
    registry_enabled: bool = False
    no_critical_findings: bool = False
    redaction_reviewed: bool = False


class CustomKernelSimulationRunIn(BaseModel):
    spec: dict[str, Any] = Field(default_factory=dict)
    template_id: str | None = Field(default=None, min_length=1, max_length=128)
    cost_cents: int = Field(default=0, ge=0, le=100_000)


class CustomKernelSuiteRunIn(BaseModel):
    suite: dict[str, Any] = Field(default_factory=dict)
    template_id: str | None = Field(default=None, min_length=1, max_length=128)
    cost_cents: int = Field(default=0, ge=0, le=100_000)


class ProtocolSimulationOut(BaseModel):
    job: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)


async def _assert_caller_owns_agent(session: AsyncSession, user: User, name: str) -> Agent:
    agent = (await session.execute(select(Agent).where(Agent.name == name))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, f"agent {name!r} not found")
    if agent.owner_id != user.id:
        raise HTTPException(403, "not the owner of this agent")
    return agent


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


def _reject_active_mutation_payload(payload: dict[str, Any]) -> None:
    for key, value in _walk_payload(payload):
        normalized = key.lower()
        if normalized in _FORBIDDEN_TRUTHY_KEYS and bool(value):
            raise HTTPException(400, f"protocol simulations cannot apply mutations: {key!r}")
        if normalized in _FORBIDDEN_NONEMPTY_KEYS and bool(value):
            raise HTTPException(400, f"protocol simulations cannot carry mutation grants: {key!r}")


def _metadata_int(job: WorkJob, key: str, default: int = 0) -> int:
    try:
        return int((job.metadata_json or {}).get(key) or default)
    except (TypeError, ValueError):
        return default


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _simulation_events(session: AsyncSession, job: WorkJob, *, user_id: int) -> list[dict[str, Any]]:
    rows = await list_job_events(session, job.job_id, user_id=user_id)
    return [serialize_event(row) for row in rows]


async def _simulation_out(session: AsyncSession, job: WorkJob, *, user_id: int) -> ProtocolSimulationOut:
    return ProtocolSimulationOut(job=serialize_job(job), events=await _simulation_events(session, job, user_id=user_id))


async def _get_owned_simulation(session: AsyncSession, user: User, agent_name: str, job_id: str) -> WorkJob:
    await _assert_caller_owns_agent(session, user, agent_name)
    row = (
        await session.execute(
            select(WorkJob).where(
                WorkJob.job_id == job_id,
                WorkJob.user_id == user.id,
                WorkJob.kind == PROTOCOL_SIMULATION_KIND,
                WorkJob.subject_type == "agent",
                WorkJob.subject_id == agent_name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "protocol simulation not found")
    return row


async def _enforce_simulation_bounds(
    session: AsyncSession,
    job: WorkJob,
    body: ProtocolSimulationEventIn,
    *,
    user_id: int,
) -> list[dict[str, Any]]:
    events = await _simulation_events(session, job, user_id=user_id)
    ttl_seconds = _metadata_int(job, "ttl_seconds")
    if ttl_seconds and (datetime.now(timezone.utc) - _as_utc(job.created_at)).total_seconds() > ttl_seconds:
        await fail_job(
            session,
            job,
            error="protocol simulation TTL expired",
            result={"reason": "ttl_expired", "ttl_seconds": ttl_seconds, "active_apply_enabled": False},
            summary="protocol simulation TTL expired",
            user_id=user_id,
            status="killed",
            event_type="simulation_ttl_expired",
            commit=True,
        )
        raise HTTPException(409, "protocol simulation TTL expired")

    max_episodes = _metadata_int(job, "max_episodes")
    if body.event_type in _EPISODE_EVENT_TYPES:
        completed = sum(1 for event in events if event.get("event_type") in _EPISODE_EVENT_TYPES)
        if max_episodes and completed >= max_episodes:
            await fail_job(
                session,
                job,
                error="protocol simulation max episodes exceeded",
                result={"reason": "max_episodes_exceeded", "max_episodes": max_episodes, "active_apply_enabled": False},
                summary="protocol simulation max episodes exceeded",
                user_id=user_id,
                status="killed",
                event_type="simulation_episode_limit_exceeded",
                commit=True,
            )
            raise HTTPException(409, "protocol simulation max episodes exceeded")

    run_budget = _metadata_int(job, "run_budget_cents")
    budget_ceiling = _metadata_int(job, "budget_ceiling_cents")
    spent = sum(int((event.get("payload") or {}).get("cost_cents") or 0) for event in events)
    if body.cost_cents and (
        (run_budget and spent + body.cost_cents > run_budget)
        or (budget_ceiling and spent + body.cost_cents > budget_ceiling)
    ):
        await fail_job(
            session,
            job,
            error="protocol simulation budget exceeded",
            result={
                "reason": "budget_exceeded",
                "spent_cents": spent,
                "event_cost_cents": body.cost_cents,
                "run_budget_cents": run_budget,
                "budget_ceiling_cents": budget_ceiling,
                "active_apply_enabled": False,
            },
            summary="protocol simulation budget exceeded",
            user_id=user_id,
            status="killed",
            event_type="simulation_budget_exceeded",
            commit=True,
        )
        raise HTTPException(409, "protocol simulation budget exceeded")
    return events


def _stage_for_event(event_type: str) -> str:
    return {
        "simulation_started": "running",
        "episode_started": "episode",
        "signal_emitted": "signals",
        "invariant_checked": "invariants",
        "attempt_scored": "scoring",
        "proposal_emitted": "proposal",
        "simulation_completed": "completed",
        "simulation_failed": "failed",
    }.get(event_type, "event")


@router.post("/{name}/protocol-simulations", response_model=ProtocolSimulationOut, status_code=201)
async def create_protocol_simulation(
    name: str,
    body: ProtocolSimulationIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    if body.run_budget_cents > body.budget_ceiling_cents:
        raise HTTPException(400, "run budget exceeds budget ceiling")
    agent = await _assert_caller_owns_agent(session, user, name)
    protocol_ref = protocol_ref_payload(
        protocol_id=body.protocol_id,
        version=body.protocol_version,
        display_name=body.display_name,
        template_refs=[body.template_ref],
    )
    job_id = f"psim-{uuid4().hex[:16]}"
    payload = {
        "target_agent": agent.name,
        "protocol_ref": protocol_ref,
        "template_ref": body.template_ref,
        "simulation_goal": body.simulation_goal,
        "roles": body.roles,
        "invariants": body.invariants,
        "signals_consumed": body.signals_consumed,
        "signals_emitted": body.signals_emitted,
        "limits": {
            "run_budget_cents": body.run_budget_cents,
            "budget_ceiling_cents": body.budget_ceiling_cents,
            "ttl_seconds": body.ttl_seconds,
            "max_episodes": body.max_episodes,
        },
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    job = await create_job(
        session,
        user_id=user.id,
        kind=PROTOCOL_SIMULATION_KIND,
        payload=payload,
        title=f"Protocol simulation: {protocol_ref['display_name']} / {agent.name}",
        metadata=protocol_simulation_metadata(
            protocol_ref=protocol_ref,
            template_ref=body.template_ref,
            budget_ceiling_cents=body.budget_ceiling_cents,
            run_budget_cents=body.run_budget_cents,
            ttl_seconds=body.ttl_seconds,
            max_episodes=body.max_episodes,
        ),
        job_id=job_id,
        status="queued",
        queue="protocol-simulations",
        priority=10,
        correlation_id=job_id,
        source_type="protocol_pack",
        source_id=str(protocol_ref["id"]),
        subject_type="agent",
        subject_id=agent.name,
        worker_type="simulator",
        worker_name=body.template_ref,
        idempotency_scope="protocol_simulation",
        record_event=False,
        commit=False,
    )
    await append_event(
        session,
        job,
        event_type="simulation_created",
        payload={
            "target_agent": agent.name,
            "protocol_ref": protocol_ref,
            "template_ref": body.template_ref,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        message=f"protocol simulation created for {agent.name}",
        status="queued",
        stage="created",
        user_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        source_type="protocol_pack",
        source_id=str(protocol_ref["id"]),
        commit=False,
    )
    await session.commit()
    await session.refresh(job)
    return await _simulation_out(session, job, user_id=user.id)


@router.get("/{name}/protocol-simulations", response_model=list[ProtocolSimulationOut])
async def list_protocol_simulations(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[ProtocolSimulationOut]:
    await _assert_caller_owns_agent(session, user, name)
    rows = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user.id,
                WorkJob.kind == PROTOCOL_SIMULATION_KIND,
                WorkJob.subject_type == "agent",
                WorkJob.subject_id == name,
            )
            .order_by(desc(WorkJob.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return [await _simulation_out(session, row, user_id=user.id) for row in rows]


@router.get("/{name}/protocol-simulations/scenarios", response_model=list[dict[str, Any]])
async def list_protocol_simulation_scenarios(
    name: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _assert_caller_owns_agent(session, user, name)
    return list_graph_kernel_scenarios()


@router.get("/{name}/protocol-simulations/protocol-registry", response_model=list[dict[str, Any]])
async def list_protocol_pack_registry(
    name: str,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _assert_caller_owns_agent(session, user, name)
    return await list_persistent_protocol_registry(session)


@router.post("/{name}/protocol-simulations/runtime-readiness", response_model=dict[str, Any])
async def check_protocol_runtime_readiness(
    name: str,
    body: ProtocolRuntimeReadinessIn,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _assert_caller_owns_agent(session, user, name)
    return evaluate_active_runtime_readiness(**body.model_dump()).to_payload()


@router.get("/{name}/protocol-simulations/custom-templates", response_model=list[dict[str, Any]])
async def list_custom_protocol_simulation_templates(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _assert_caller_owns_agent(session, user, name)
    return list_custom_kernel_templates()


@router.get("/{name}/protocol-simulations/custom-suite-templates", response_model=list[dict[str, Any]])
async def list_custom_protocol_simulation_suite_templates(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await _assert_caller_owns_agent(session, user, name)
    return list_custom_kernel_suite_templates()


@router.post("/{name}/protocol-simulations/custom-runs", response_model=ProtocolSimulationOut, status_code=201)
async def run_custom_protocol_simulation(
    name: str,
    body: CustomKernelSimulationRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    agent = await _assert_caller_owns_agent(session, user, name)
    spec = dict(body.spec or {})
    try:
        if body.template_id:
            if spec:
                raise CustomSimulationError("custom simulation requests must provide either template_id or spec, not both")
            spec = render_custom_kernel_template(body.template_id)
        result = run_custom_kernel_simulation(spec)
    except CustomSimulationError as exc:
        raise HTTPException(400, str(exc)) from exc
    _reject_active_mutation_payload({"spec": spec, "trace": result.trace})
    template_ref = str(spec.get("template_ref") or "custom_kernel@v1")
    template_id = body.template_id or str(spec.get("template_ref") or "")
    protocol_ref = protocol_ref_payload(
        protocol_id="custom_kernel",
        version=1,
        display_name="Custom kernel simulation",
        template_refs=[template_ref],
    )
    job_id = f"psim-{uuid4().hex[:16]}"
    payload = {
        "target_agent": agent.name,
        "protocol_ref": protocol_ref,
        "template_ref": template_ref,
        "template_id": template_id or None,
        "simulation_goal": str(spec.get("goal") or spec.get("title") or "Custom kernel simulation"),
        "custom_spec": spec,
        "limits": {
            "cost_cents": body.cost_cents,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    job = await create_job(
        session,
        user_id=user.id,
        kind=PROTOCOL_SIMULATION_KIND,
        payload=payload,
        title=f"Custom kernel simulation / {agent.name}",
        metadata=protocol_simulation_metadata(
            protocol_ref=protocol_ref,
            template_ref=template_ref,
            budget_ceiling_cents=body.cost_cents,
            run_budget_cents=body.cost_cents,
            ttl_seconds=900,
            max_episodes=1,
        ),
        job_id=job_id,
        status="complete" if result.passed else "failed",
        queue="protocol-simulations",
        priority=10,
        correlation_id=job_id,
        source_type="protocol_pack",
        source_id="custom_kernel",
        subject_type="agent",
        subject_id=agent.name,
        worker_type="simulator",
        worker_name=template_ref,
        idempotency_scope="protocol_simulation",
        record_event=False,
        commit=False,
    )
    now = datetime.now(timezone.utc)
    job.started_at = now
    job.completed_at = now
    job.summary = "custom kernel simulation passed" if result.passed else "custom kernel simulation failed"
    if not result.passed:
        job.error = "custom kernel simulation invariant failed"
        job.error_payload = {
            "active_apply_enabled": False,
            "violations": result.trace.get("violations") or [],
            "alerts": result.trace.get("alerts") or [],
        }
    await append_event(
        session,
        job,
        event_type="simulation_created",
        payload={
            "target_agent": agent.name,
            "protocol_ref": protocol_ref,
            "template_ref": template_ref,
            "template_id": template_id or None,
            "custom": True,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        message=f"custom kernel simulation created for {agent.name}",
        status="running",
        stage="created",
        user_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        source_type="protocol_pack",
        source_id="custom_kernel",
        commit=False,
    )
    await append_event(
        session,
        job,
        event_type="scenario_trace_recorded",
        payload=KernelTraceRecordedPayload(
            target_agent=agent.name,
            protocol_ref=protocol_ref,
            template_ref=template_ref,
            template_id=template_id or None,
            trace_summary=result.trace_summary,
            runtime_readiness=evaluate_active_runtime_readiness(
                requested=False,
                simulation_passed=result.passed,
                policy_reviewed=False,
                owner_approved=False,
                operator_enabled=False,
                registry_enabled=True,
                no_critical_findings=result.passed,
                redaction_reviewed=True,
            ).to_payload(),
            traces=(result.trace_contract,),
            state_summary=result.state_summary_contract,
            step_results=result.step_result_contracts,
            cost_cents=body.cost_cents,
        ).to_payload(),
        message="custom kernel simulation trace recorded",
        status=job.status,
        stage="scenario_trace",
        severity="info" if result.passed else "critical",
        user_id=user.id,
        actor_type="simulator",
        actor_id=template_ref,
        source_type="protocol_pack",
        source_id="custom_kernel",
        commit=True,
    )
    await session.refresh(job)
    return await _simulation_out(session, job, user_id=user.id)


@router.post("/{name}/protocol-simulations/custom-suites", response_model=ProtocolSimulationOut, status_code=201)
async def run_custom_protocol_simulation_suite(
    name: str,
    body: CustomKernelSuiteRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    agent = await _assert_caller_owns_agent(session, user, name)
    suite = dict(body.suite or {})
    try:
        if body.template_id:
            if suite:
                raise CustomSimulationError("custom suite requests must provide either template_id or suite, not both")
            suite = render_custom_kernel_suite_template(body.template_id)
        result = run_custom_kernel_suite(suite)
    except CustomSimulationError as exc:
        raise HTTPException(400, str(exc)) from exc
    _reject_active_mutation_payload(
        {
            "suite": suite,
            "episodes": result.episodes,
            "scoreboard": result.scoreboard,
        }
    )
    template_ref = str(suite.get("template_ref") or "custom_kernel_suite@v1")
    template_id = body.template_id or str(suite.get("template_ref") or "")
    protocol_ref = protocol_ref_payload(
        protocol_id="custom_kernel_suite",
        version=1,
        display_name="Custom kernel suite",
        template_refs=[template_ref],
    )
    job_id = f"psim-{uuid4().hex[:16]}"
    payload = {
        "target_agent": agent.name,
        "protocol_ref": protocol_ref,
        "template_ref": template_ref,
        "template_id": template_id or None,
        "simulation_goal": result.title,
        "suite": suite,
        "limits": {
            "cost_cents": body.cost_cents,
            "episode_count": len(result.episodes),
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    job = await create_job(
        session,
        user_id=user.id,
        kind=PROTOCOL_SIMULATION_KIND,
        payload=payload,
        title=f"Custom kernel suite / {agent.name}",
        metadata=protocol_simulation_metadata(
            protocol_ref=protocol_ref,
            template_ref=template_ref,
            budget_ceiling_cents=body.cost_cents,
            run_budget_cents=body.cost_cents,
            ttl_seconds=900,
            max_episodes=len(result.episodes),
        ),
        job_id=job_id,
        status="complete" if result.passed else "failed",
        queue="protocol-simulations",
        priority=10,
        correlation_id=job_id,
        source_type="protocol_pack",
        source_id="custom_kernel_suite",
        subject_type="agent",
        subject_id=agent.name,
        worker_type="simulator",
        worker_name=template_ref,
        idempotency_scope="protocol_simulation",
        record_event=False,
        commit=False,
    )
    now = datetime.now(timezone.utc)
    job.started_at = now
    job.completed_at = now
    job.summary = "custom kernel suite passed" if result.passed else "custom kernel suite failed"
    if not result.passed:
        job.error = "custom kernel suite invariant failed"
        job.error_payload = {
            "active_apply_enabled": False,
            "violations": result.violations,
            "alerts": result.alerts,
            "invariant_failures": result.scoreboard.get("invariant_failures") or [],
        }
    await append_event(
        session,
        job,
        event_type="simulation_created",
        payload={
            "target_agent": agent.name,
            "protocol_ref": protocol_ref,
            "template_ref": template_ref,
            "template_id": template_id or None,
            "custom_suite": True,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        message=f"custom kernel suite created for {agent.name}",
        status="running",
        stage="created",
        user_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        source_type="protocol_pack",
        source_id="custom_kernel_suite",
        commit=False,
    )
    await append_event(
        session,
        job,
        event_type="arena_suite_recorded",
        payload={
            "target_agent": agent.name,
            "protocol_ref": protocol_ref,
            "template_ref": template_ref,
            "template_id": template_id or None,
            "suite_id": result.suite_id,
            "title": result.title,
            "passed": result.passed,
            "episode_count": len(result.episodes),
            "trace_summary": result.trace_summary,
            "runtime_readiness": evaluate_active_runtime_readiness(
                requested=False,
                simulation_passed=result.passed,
                policy_reviewed=False,
                owner_approved=False,
                operator_enabled=False,
                registry_enabled=True,
                no_critical_findings=result.passed,
                redaction_reviewed=True,
            ).to_payload(),
            "episodes": result.episodes,
            "scoreboard": result.scoreboard,
            "alerts": result.alerts,
            "violations": result.violations,
            "cost_cents": body.cost_cents,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        message="custom kernel suite scoreboard recorded",
        status=job.status,
        stage="arena_suite",
        severity="info" if result.passed else "critical",
        user_id=user.id,
        actor_type="simulator",
        actor_id=template_ref,
        source_type="protocol_pack",
        source_id="custom_kernel_suite",
        commit=True,
    )
    await session.refresh(job)
    return await _simulation_out(session, job, user_id=user.id)


@router.get("/{name}/protocol-simulations/{job_id}", response_model=ProtocolSimulationOut)
async def get_protocol_simulation(
    name: str,
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    row = await _get_owned_simulation(session, user, name, job_id)
    return await _simulation_out(session, row, user_id=user.id)


@router.post("/{name}/protocol-simulations/{job_id}/events", response_model=ProtocolSimulationOut)
async def record_protocol_simulation_event(
    name: str,
    job_id: str,
    body: ProtocolSimulationEventIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    row = await _get_owned_simulation(session, user, name, job_id)
    if row.status in _TERMINAL_STATUSES:
        raise HTTPException(409, "protocol simulation is already terminal")
    _reject_active_mutation_payload(
        {
            "payload": body.payload,
            "proposal_ref": body.proposal_ref,
            "signal_type": body.signal_type,
        }
    )
    await _enforce_simulation_bounds(session, row, body, user_id=user.id)
    protocol_ref = (row.metadata_json or {}).get("protocol_ref") or {}
    payload = {
        **body.payload,
        "episode": body.episode,
        "signal_type": body.signal_type,
        "invariant": body.invariant,
        "score": body.score,
        "proposal_ref": body.proposal_ref,
        "cost_cents": body.cost_cents,
        "target_agent": name,
        "protocol_ref": protocol_ref,
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    severity = "critical" if body.event_type == "simulation_failed" else "info"
    status = {
        "simulation_completed": "complete",
        "simulation_failed": "failed",
    }.get(body.event_type, "running")
    row.status = status
    row.started_at = row.started_at or datetime.now(timezone.utc)
    row.completed_at = datetime.now(timezone.utc) if status in {"complete", "failed"} else row.completed_at
    row.updated_at = datetime.now(timezone.utc)
    if body.event_type == "simulation_completed":
        row.summary = body.message or "protocol simulation completed"
        row.output_payload = {**(row.output_payload or {}), "active_apply_enabled": False}
    if body.event_type == "simulation_failed":
        row.error = body.message or "protocol simulation failed"
        row.error_payload = {"active_apply_enabled": False, **body.payload}
    await append_event(
        session,
        row,
        event_type=body.event_type,
        payload=payload,
        message=body.message or body.event_type.replace("_", " "),
        status=status,
        stage=_stage_for_event(body.event_type),
        severity=severity,
        user_id=user.id,
        actor_type="simulator",
        actor_id=str(protocol_ref.get("id") or row.worker_name or "protocol"),
        source_type="protocol_pack",
        source_id=str(protocol_ref.get("id") or row.source_id or ""),
        commit=True,
    )
    await session.refresh(row)
    return await _simulation_out(session, row, user_id=user.id)


@router.post("/{name}/protocol-simulations/{job_id}/scenario-runs", response_model=ProtocolSimulationOut)
async def record_protocol_simulation_scenario_run(
    name: str,
    job_id: str,
    body: ProtocolScenarioRunIn,
    user: User = Depends(current_user_or_agent_invoke),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    row = await _get_owned_simulation(session, user, name, job_id)
    if row.status in _TERMINAL_STATUSES:
        raise HTTPException(409, "protocol simulation is already terminal")
    await _enforce_simulation_bounds(
        session,
        row,
        ProtocolSimulationEventIn(event_type="invariant_checked", cost_cents=body.cost_cents),
        user_id=user.id,
    )
    try:
        traces = run_graph_kernel_scenarios(body.scenario_ids or None)
    except ScenarioRunError as exc:
        raise HTTPException(400, str(exc)) from exc

    trace_payloads = [trace.to_payload() for trace in traces]
    _reject_active_mutation_payload({"traces": trace_payloads})
    passed = all(trace.passed for trace in traces)
    trace_summary = summarize_scenario_trace_payload(
        {"traces": trace_payloads, "passed": passed}
    )
    protocol_ref = (row.metadata_json or {}).get("protocol_ref") or {}
    payload = {
        "target_agent": name,
        "protocol_ref": protocol_ref,
        "protocol_class": graph_kernel_protocol_class().to_payload(),
        "scenario_ids": [trace.scenario_id for trace in traces],
        "scenario_count": len(traces),
        "passed": passed,
        "trace_summary": trace_summary,
        "runtime_readiness": evaluate_active_runtime_readiness(
            requested=False,
            simulation_passed=passed,
            policy_reviewed=False,
            owner_approved=False,
            operator_enabled=False,
            registry_enabled=True,
            no_critical_findings=passed,
            redaction_reviewed=True,
        ).to_payload(),
        "traces": trace_payloads,
        "cost_cents": body.cost_cents,
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    row.status = "running" if passed else "failed"
    row.started_at = row.started_at or datetime.now(timezone.utc)
    row.completed_at = datetime.now(timezone.utc) if not passed else row.completed_at
    row.updated_at = datetime.now(timezone.utc)
    if not passed:
        row.error = "graph-kernel scenario invariant failed"
        row.error_payload = {"active_apply_enabled": False, "scenario_ids": payload["scenario_ids"]}
    await append_event(
        session,
        row,
        event_type="scenario_trace_recorded",
        payload=payload,
        message="graph-kernel scenario trace recorded",
        status=row.status,
        stage="scenario_trace",
        severity="info" if passed else "critical",
        user_id=user.id,
        actor_type="simulator",
        actor_id="graph_kernel_scenarios",
        source_type="protocol_pack",
        source_id=str(protocol_ref.get("id") or row.source_id or ""),
        commit=True,
    )
    await session.refresh(row)
    return await _simulation_out(session, row, user_id=user.id)


@router.post("/{name}/protocol-simulations/{job_id}/stop", response_model=ProtocolSimulationOut)
async def stop_protocol_simulation(
    name: str,
    job_id: str,
    body: ProtocolSimulationStopIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ProtocolSimulationOut:
    row = await _get_owned_simulation(session, user, name, job_id)
    if row.status not in _TERMINAL_STATUSES:
        await fail_job(
            session,
            row,
            error=body.reason,
            result={"kill_switch": True, "active_apply_enabled": False},
            summary="protocol simulation stopped by owner",
            user_id=user.id,
            status="killed",
            event_type="simulation_killed",
            commit=True,
        )
        await session.refresh(row)
    return await _simulation_out(session, row, user_id=user.id)
