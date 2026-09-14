"""User-created kernel simulation endpoints.

These routes expose the bounded custom graph-kernel runner without requiring
the simulation to be attached to a deployed agent. They remain simulation-only
and proposal-only; no result can mutate live routes, grants, memory, source,
policy, marketplace state, or manifests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..custom_kernel_simulations import CustomSimulationError, run_custom_kernel_simulation
from ..custom_kernel_templates import render_custom_kernel_template
from ..db import get_session
from ..kernel_contracts import KernelEvidenceEvent
from ..kernel_steps import KernelStepSpec
from ..kernel_traces import KernelReplayResult, KernelTraceRecordedPayload
from ..live_kernel_simulations import (
    LiveKernelSimulationError,
    live_invocation_summary,
    run_hybrid_live_invocations,
)
from ..models import ChatThread, User, WorkJob
from ..protocol_simulation import (
    PROTOCOL_SIMULATION_KIND,
    evaluate_active_runtime_readiness,
    protocol_ref_payload,
    protocol_simulation_metadata,
)
from ..thread_events import append_thread_event
from ..work_ledger import append_event, create_job, list_job_events, serialize_event, serialize_job

router = APIRouter(prefix="/v1/me/kernel-simulations", tags=["kernel-simulations"])

SimulationKind = Literal[
    "market",
    "tournament",
    "school",
    "adversarial_arena",
    "resource_competition",
    "capability_lifecycle",
    "partnership",
]


class KernelActorSpec(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    label: str | None = Field(default=None, max_length=160)
    role: str | None = Field(default=None, max_length=80)


class KernelPortSpec(BaseModel):
    node_id: str = Field(min_length=1, max_length=80)
    id: str = Field(min_length=1, max_length=80)
    direction: Literal["input", "output", "bidirectional"] = "bidirectional"
    schema_ref: str = Field(default="kernel.signal.v1", min_length=1, max_length=160)
    port_type: str | None = Field(default=None, max_length=80)


class KernelCapabilitySpec(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    owner: str = Field(min_length=1, max_length=80)
    actions: list[str] = Field(default_factory=list, max_length=20)
    resources: list[str] = Field(default_factory=list, max_length=40)
    budget: int = Field(default=10, ge=0, le=10_000)
    delegation_depth: int = Field(default=1, ge=0, le=20)


class KernelPolicySpec(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    level: Literal["platform", "org", "owner", "process"] = "owner"
    effect: Literal[
        "allow",
        "deny",
        "freeze",
        "revoke",
        "require_approval",
        "require_narrower_scope",
    ] = "allow"
    actions: list[str] = Field(default_factory=list, max_length=20)
    resources: list[str] = Field(default_factory=list, max_length=40)


class KernelEndpointSpec(BaseModel):
    node_id: str = Field(min_length=1, max_length=80)
    port_id: str = Field(min_length=1, max_length=80)


class KernelEdgeSpec(BaseModel):
    id: str | None = Field(default=None, max_length=80)
    edge_id: str | None = Field(default=None, max_length=80)
    from_: KernelEndpointSpec = Field(alias="from")
    to: KernelEndpointSpec
    edge_type: str = Field(default="call", min_length=1, max_length=80)
    capability_id: str | None = Field(default=None, max_length=80)
    process_id: str | None = Field(default=None, max_length=80)
    provenance_ref: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def _has_identity(self) -> "KernelEdgeSpec":
        if not (self.id or self.edge_id):
            raise ValueError("edge id or edge_id is required")
        return self


class KernelSimulationSpec(BaseModel):
    simulation_type: SimulationKind
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=1000)
    scenario_id: str | None = Field(default=None, max_length=120)
    actors: list[KernelActorSpec] = Field(default_factory=list, max_length=25)
    ports: list[KernelPortSpec] = Field(default_factory=list, max_length=120)
    edges: list[KernelEdgeSpec] = Field(default_factory=list, max_length=120)
    capabilities: list[KernelCapabilitySpec] = Field(default_factory=list, max_length=60)
    policies: list[KernelPolicySpec] = Field(default_factory=list, max_length=80)
    steps: list[KernelStepSpec] = Field(default_factory=list, max_length=120)
    invariants: list[str | dict[str, Any]] = Field(default_factory=list, max_length=25)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_runtime_content(self) -> "KernelSimulationSpec":
        if not self.actors and not self.steps:
            raise ValueError("simulation spec must include actors or steps")
        return self


class KernelSimulationRunIn(BaseModel):
    spec: KernelSimulationSpec | None = None
    template_id: str | None = Field(default=None, min_length=1, max_length=128)
    cost_cents: int = Field(default=0, ge=0, le=100_000)
    thread_id: str | None = Field(default=None, min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    execution_mode: Literal["bounded", "hybrid"] = "bounded"
    max_live_calls: int = Field(default=3, ge=1, le=8)

    @model_validator(mode="after")
    def _one_source(self) -> "KernelSimulationRunIn":
        if bool(self.spec) == bool(self.template_id):
            raise ValueError("provide exactly one of spec or template_id")
        return self


class KernelSimulationReplayOut(BaseModel):
    job: dict[str, Any]
    replay_passed: bool
    original_summary: dict[str, Any]
    replay_summary: dict[str, Any]


class KernelSimulationOut(BaseModel):
    job: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)


def _template(template_id: str, simulation_type: SimulationKind, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "template_id": template_id,
        "name": name,
        "simulation_type": simulation_type,
        "description": spec.get("goal") or spec.get("title") or name,
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
        "spec": spec,
    }


def _school_spec() -> dict[str, Any]:
    return {
        "simulation_type": "school",
        "title": "Cohort capability growth drill",
        "goal": "Model a mentor delegating bounded practice authority to learners and scoring readiness without granting live authority.",
        "actors": [{"id": "mentor"}, {"id": "learner-a"}, {"id": "learner-b"}],
        "capabilities": [
            {
                "id": "cap-mentor",
                "owner": "mentor",
                "actions": ["train", "review", "call"],
                "resources": ["cohort:*", "skill:*"],
                "budget": 20,
                "delegation_depth": 2,
            }
        ],
        "steps": [
            {
                "type": "delegate",
                "parent_capability_id": "cap-mentor",
                "child_capability_id": "cap-learner-a",
                "child_owner": "learner-a",
                "requested": {
                    "actions": ["call"],
                    "resources": ["skill:practice"],
                    "budget": 5,
                    "delegation_depth": 1,
                },
            },
            {"type": "emit_signal", "node_id": "learner-a", "signal_type": "practice_complete", "payload": {"score": 0.92}},
            {
                "type": "record_outcome",
                "outcome_id": "outcome-learner-a",
                "participant_id": "learner-a",
                "metrics": {"practice_score": 0.92, "cost": 2},
                "evidence_refs": ["signal:practice_complete"],
            },
            {
                "type": "score_participant",
                "score_id": "score-learner-a",
                "participant_id": "learner-a",
                "outcome_id": "outcome-learner-a",
                "score": 92,
            },
            {"type": "revoke_capability", "capability_id": "cap-learner-a"},
        ],
        "invariants": [
            "replay_deterministic",
            "no_active_apply",
            "no_violations",
            {"id": "participant_score", "score_id": "score-learner-a", "score": 92},
        ],
    }


def _partnership_spec() -> dict[str, Any]:
    return {
        "simulation_type": "partnership",
        "title": "Bounded cooperation partnership drill",
        "goal": "Model two agents forming a scoped cooperation edge, using it once, and expiring the process-local authority.",
        "actors": [{"id": "alpha"}, {"id": "beta"}],
        "ports": [
            {"node_id": "alpha", "id": "route:task", "direction": "output", "schema_ref": "task.v1"},
            {"node_id": "beta", "id": "invoke:task", "direction": "input", "schema_ref": "task.v1"},
        ],
        "capabilities": [
            {
                "id": "cap-alpha",
                "owner": "alpha",
                "actions": ["call"],
                "resources": ["beta:invoke:*", "skill:cooperate"],
                "budget": 8,
                "delegation_depth": 1,
            }
        ],
        "policies": [
            {
                "id": "owner-allow-cooperate",
                "level": "owner",
                "effect": "allow",
                "actions": ["call"],
                "resources": ["beta:invoke:*", "skill:cooperate"],
            }
        ],
        "steps": [
            {
                "type": "start_process",
                "process_id": "partnership-1",
                "owner": "alpha",
                "capability_id": "cap-alpha",
                "ttl": 5,
                "budget": 5,
            },
            {
                "type": "propose_edge",
                "edge_id": "edge-alpha-beta",
                "from": {"node_id": "alpha", "port_id": "route:task"},
                "to": {"node_id": "beta", "port_id": "invoke:task"},
                "edge_type": "call",
                "capability_id": "cap-alpha",
                "process_id": "partnership-1",
            },
            {"type": "activate_edge", "edge_id": "edge-alpha-beta", "decision_id": "pd-partnership"},
            {"type": "use_edge", "edge_id": "edge-alpha-beta", "cost": 1},
            {"type": "stop_process", "process_id": "partnership-1", "reason": "simulation_completed"},
        ],
        "invariants": [
            "replay_deterministic",
            "no_active_apply",
            "no_violations",
            {"id": "edge_expired", "edge_id": "edge-alpha-beta"},
            {"id": "no_active_process_edges", "process_id": "partnership-1"},
        ],
    }


def _starter_templates() -> list[dict[str, Any]]:
    specs = {
        "market@v1": ("market", "Market allocation", render_custom_kernel_template("competitive_allocation@v1")),
        "resource_competition@v1": ("resource_competition", "Resource competition", render_custom_kernel_template("competitive_allocation@v1")),
        "tournament@v1": ("tournament", "Tournament scoring", render_custom_kernel_template("arena_outcome@v1")),
        "adversarial_arena@v1": ("adversarial_arena", "Adversarial review arena", render_custom_kernel_template("adversarial_reviewer@v1")),
        "capability_lifecycle@v1": ("capability_lifecycle", "Capability growth and retirement", render_custom_kernel_template("deletion_preview@v1")),
        "school@v1": ("school", "School/cohort capability growth", _school_spec()),
        "partnership@v1": ("partnership", "Cooperation partnership", _partnership_spec()),
    }
    out: list[dict[str, Any]] = []
    for template_id, (kind, name, spec) in specs.items():
        next_spec = dict(spec)
        next_spec["simulation_type"] = kind
        next_spec.setdefault("goal", next_spec.get("title") or name)
        next_spec.setdefault("metadata", {})
        next_spec["metadata"] = {
            **(next_spec.get("metadata") if isinstance(next_spec.get("metadata"), dict) else {}),
            "starter_template_id": template_id,
        }
        out.append(_template(template_id, kind, name, next_spec))  # type: ignore[arg-type]
    return out


def _spec_from_body(body: KernelSimulationRunIn) -> tuple[str | None, KernelSimulationSpec]:
    if body.template_id:
        for template in _starter_templates():
            if template["template_id"] == body.template_id:
                return body.template_id, KernelSimulationSpec.model_validate(template["spec"])
        raise HTTPException(400, f"unknown kernel simulation template: {body.template_id!r}")
    assert body.spec is not None
    return None, body.spec


async def _events(session: AsyncSession, job: WorkJob, *, user_id: int) -> list[dict[str, Any]]:
    rows = await list_job_events(session, job.job_id, user_id=user_id)
    return [serialize_event(row) for row in rows]


async def _out(session: AsyncSession, job: WorkJob, *, user_id: int) -> KernelSimulationOut:
    return KernelSimulationOut(job=serialize_job(job), events=await _events(session, job, user_id=user_id))


async def _get_owned_job(session: AsyncSession, user: User, job_id: str) -> WorkJob:
    row = (
        await session.execute(
            select(WorkJob).where(
                WorkJob.job_id == job_id,
                WorkJob.user_id == user.id,
                WorkJob.kind == PROTOCOL_SIMULATION_KIND,
                WorkJob.subject_type == "user",
                WorkJob.subject_id == str(user.id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "kernel simulation not found")
    return row


async def _assert_thread(session: AsyncSession, user: User, thread_id: str | None) -> ChatThread | None:
    if not thread_id:
        return None
    row = (
        await session.execute(
            select(ChatThread).where(ChatThread.id == thread_id, ChatThread.user_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "thread not found")
    return row


def _protocol_ref(spec: KernelSimulationSpec) -> dict[str, Any]:
    return protocol_ref_payload(
        protocol_id=f"user_{spec.simulation_type}",
        version=1,
        display_name=spec.title,
        template_refs=[str((spec.metadata or {}).get("starter_template_id") or f"{spec.simulation_type}@user")],
    )


def _trace_payload(
    *,
    user: User,
    spec: KernelSimulationSpec,
    result: Any,
    protocol_ref: dict[str, Any],
    template_id: str | None,
    cost_cents: int,
    execution_mode: str,
    live_invocations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    live_passed = True
    if live_invocations is not None:
        live_passed = bool(live_invocations.get("passed"))
    passed = result.passed and live_passed
    payload = KernelTraceRecordedPayload(
        target_user=user.id,
        simulation_type=spec.simulation_type,
        protocol_ref=protocol_ref,
        template_ref=template_id or str((spec.metadata or {}).get("starter_template_id") or "user_kernel@v1"),
        trace_summary=result.trace_summary,
        runtime_readiness=evaluate_active_runtime_readiness(
            requested=False,
            simulation_passed=passed,
            policy_reviewed=False,
            owner_approved=False,
            operator_enabled=False,
            registry_enabled=True,
            no_critical_findings=passed,
            redaction_reviewed=True,
        ).to_payload(),
        traces=(result.trace_contract,),
        state_summary=result.state_summary_contract,
        step_results=result.step_result_contracts,
        cost_cents=cost_cents,
    ).to_payload()
    payload["execution_mode"] = execution_mode
    payload["passed"] = passed
    payload["simulation_only"] = execution_mode == "bounded"
    payload["hybrid_live_agents"] = execution_mode == "hybrid"
    payload["active_apply_enabled"] = False
    if live_invocations is not None:
        live_summary = live_invocation_summary({"live_invocations": live_invocations})
        payload["live_invocations"] = live_invocations
        payload["trace_summary"] = {
            **(payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {}),
            **live_summary,
        }
    return payload


async def _project_thread_event(
    session: AsyncSession,
    *,
    user: User,
    thread: ChatThread | None,
    payload: dict[str, Any],
) -> None:
    if thread is None:
        return
    await append_thread_event(
        session,
        thread_id=thread.id,
        user_id=user.id,
        event=KernelEvidenceEvent(
            evidence_kind="scenario_trace",
            event_type="scenario_trace_recorded",
            title="kernel simulation trace recorded",
            status="complete" if payload.get("passed") else "failed",
            severity="info" if payload.get("passed") else "critical",
            message="user-created kernel simulation trace recorded",
            source={"kind": "user_kernel_simulation", "user_id": user.id},
            payload=payload,
        ).to_payload(),
    )
    for decision in payload.get("policy_decisions") or []:
        if not isinstance(decision, dict):
            continue
        severity = "critical" if decision.get("decision") in {"deny", "blocked"} else "info"
        await append_thread_event(
            session,
            thread_id=thread.id,
            user_id=user.id,
            event=KernelEvidenceEvent(
                evidence_kind="policy_decision",
                event_type="policy_decision_recorded",
                title=f"Policy decision: {decision.get('decision') or 'unknown'}",
                status="complete",
                severity=severity,
                message=str(decision.get("reason") or ""),
                source={"kind": "user_kernel_simulation", "user_id": user.id},
                payload=(
                    decision.get("redacted_decision")
                    if isinstance(decision.get("redacted_decision"), dict)
                    else decision
                ),
            ).to_payload(),
        )
    live = payload.get("live_invocations") if isinstance(payload.get("live_invocations"), dict) else None
    if live is not None:
        await append_thread_event(
            session,
            thread_id=thread.id,
            user_id=user.id,
            event=KernelEvidenceEvent(
                evidence_kind="live_agent_invocations",
                event_type="live_agent_invocations_recorded",
                title="Live agent invocations recorded",
                status="complete" if live.get("passed") else "failed",
                severity="info" if live.get("passed") else "critical",
                message="hybrid kernel simulation invoked selected agents and recorded their outputs",
                source={"kind": "user_kernel_simulation", "user_id": user.id},
                payload=live,
            ).to_payload(),
        )


@router.get("/templates", response_model=list[dict[str, Any]])
async def list_user_kernel_simulation_templates(
    user: User = Depends(current_user),
) -> list[dict[str, Any]]:
    del user
    return _starter_templates()


@router.post("/runs", response_model=KernelSimulationOut, status_code=201)
async def run_user_kernel_simulation(
    body: KernelSimulationRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KernelSimulationOut:
    thread = await _assert_thread(session, user, body.thread_id)
    template_id, spec = _spec_from_body(body)
    spec_payload = spec.model_dump(by_alias=True, exclude_none=True)
    try:
        result = run_custom_kernel_simulation(spec_payload)
    except CustomSimulationError as exc:
        raise HTTPException(400, str(exc)) from exc
    live_invocations: dict[str, Any] | None = None
    if body.execution_mode == "hybrid":
        if not result.passed:
            raise HTTPException(400, "hybrid execution requires the bounded kernel proof to pass first")
        try:
            live_result = await run_hybrid_live_invocations(
                spec=spec_payload,
                user=user,
                session=session,
                max_live_calls=body.max_live_calls,
            )
        except LiveKernelSimulationError as exc:
            raise HTTPException(400, str(exc)) from exc
        live_invocations = live_result.to_payload()

    protocol_ref = _protocol_ref(spec)
    trace_payload = _trace_payload(
        user=user,
        spec=spec,
        result=result,
        protocol_ref=protocol_ref,
        template_id=template_id,
        cost_cents=body.cost_cents,
        execution_mode=body.execution_mode,
        live_invocations=live_invocations,
    )
    run_passed = bool(trace_payload.get("passed"))
    job_id = f"uksim-{uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    payload = {
        "spec": spec_payload,
        "template_id": template_id,
        "thread_id": thread.id if thread else None,
        "execution_mode": body.execution_mode,
        "max_live_calls": body.max_live_calls,
        "simulation_only": body.execution_mode == "bounded",
        "hybrid_live_agents": body.execution_mode == "hybrid",
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    job = await create_job(
        session,
        user_id=user.id,
        kind=PROTOCOL_SIMULATION_KIND,
        payload=payload,
        title=f"User kernel simulation: {spec.title}",
        metadata={
            **protocol_simulation_metadata(
                protocol_ref=protocol_ref,
                template_ref=template_id or f"{spec.simulation_type}@user",
                budget_ceiling_cents=body.cost_cents,
                run_budget_cents=body.cost_cents,
                ttl_seconds=900,
                max_episodes=1,
            ),
            "user_created": True,
            "simulation_type": spec.simulation_type,
            "execution_mode": body.execution_mode,
        },
        job_id=job_id,
        status="complete" if run_passed else "failed",
        queue="kernel-simulations",
        priority=10,
        correlation_id=job_id,
        source_type="user_kernel_simulation",
        source_id=spec.simulation_type,
        subject_type="user",
        subject_id=str(user.id),
        worker_type="simulator",
        worker_name=template_id or "user_kernel_spec",
        idempotency_key=body.idempotency_key,
        idempotency_scope="user_kernel_simulation",
        record_event=False,
        commit=False,
    )
    if job.job_id != job_id:
        return await _out(session, job, user_id=user.id)
    job.started_at = now
    job.completed_at = now
    job.summary = "user kernel simulation passed" if run_passed else "user kernel simulation failed"
    job.output_payload = trace_payload
    if not run_passed:
        job.error = (
            "user kernel live invocation failed"
            if result.passed and live_invocations is not None and not live_invocations.get("passed")
            else "user kernel simulation invariant failed"
        )
        job.error_payload = {
            "active_apply_enabled": False,
            "violations": result.trace.get("violations") or [],
            "alerts": result.trace.get("alerts") or [],
            "live_invocations": live_invocations,
        }
    await append_event(
        session,
        job,
        event_type="simulation_created",
        payload={
            "protocol_ref": protocol_ref,
            "simulation_type": spec.simulation_type,
            "template_id": template_id,
            "execution_mode": body.execution_mode,
            "simulation_only": body.execution_mode == "bounded",
            "hybrid_live_agents": body.execution_mode == "hybrid",
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        message=f"user kernel simulation created: {spec.title}",
        status="running",
        stage="created",
        user_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        source_type="user_kernel_simulation",
        source_id=spec.simulation_type,
        commit=False,
    )
    if live_invocations is not None:
        await append_event(
            session,
            job,
            event_type="live_agent_invocations_recorded",
            payload=live_invocations,
            message="hybrid kernel simulation live agent invocations recorded",
            status=job.status,
            stage="live_agent_invocations",
            severity="info" if live_invocations.get("passed") else "critical",
            user_id=user.id,
            actor_type="simulator",
            actor_id=template_id or "user_kernel_spec",
            source_type="user_kernel_simulation",
            source_id=spec.simulation_type,
            commit=False,
        )
    await append_event(
        session,
        job,
        event_type="scenario_trace_recorded",
        payload=trace_payload,
        message="user-created kernel simulation trace recorded",
        status=job.status,
        stage="scenario_trace",
        severity="info" if result.passed else "critical",
        user_id=user.id,
        actor_type="simulator",
        actor_id=template_id or "user_kernel_spec",
        source_type="user_kernel_simulation",
        source_id=spec.simulation_type,
        commit=False,
    )
    for decision in trace_payload.get("policy_decisions") or []:
        if not isinstance(decision, dict):
            continue
        await append_event(
            session,
            job,
            event_type="policy_decision_recorded",
            payload=decision.get("redacted_decision") if isinstance(decision.get("redacted_decision"), dict) else decision,
            message=str(decision.get("reason") or "kernel policy decision recorded"),
            status=job.status,
            stage="policy_decision",
            severity="critical" if decision.get("decision") in {"deny", "blocked"} else "info",
            user_id=user.id,
            actor_type="simulator",
            actor_id=template_id or "user_kernel_spec",
            source_type="user_kernel_simulation",
            source_id=spec.simulation_type,
            commit=False,
        )
    await session.commit()
    await session.refresh(job)
    await _project_thread_event(session, user=user, thread=thread, payload=trace_payload)
    await session.refresh(job)
    return await _out(session, job, user_id=user.id)


@router.get("/runs", response_model=list[KernelSimulationOut])
async def list_user_kernel_simulation_runs(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[KernelSimulationOut]:
    rows = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user.id,
                WorkJob.kind == PROTOCOL_SIMULATION_KIND,
                WorkJob.subject_type == "user",
                WorkJob.subject_id == str(user.id),
            )
            .order_by(desc(WorkJob.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return [await _out(session, row, user_id=user.id) for row in rows]


@router.get("/runs/{job_id}", response_model=KernelSimulationOut)
async def get_user_kernel_simulation_run(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KernelSimulationOut:
    return await _out(session, await _get_owned_job(session, user, job_id), user_id=user.id)


@router.post("/runs/{job_id}/replay", response_model=KernelSimulationReplayOut)
async def replay_user_kernel_simulation_run(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KernelSimulationReplayOut:
    job = await _get_owned_job(session, user, job_id)
    payload = job.input_payload if isinstance(job.input_payload, dict) else {}
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise HTTPException(409, "kernel simulation spec is missing")
    try:
        result = run_custom_kernel_simulation(spec)
    except CustomSimulationError as exc:
        raise HTTPException(409, f"kernel simulation replay failed: {exc}") from exc
    original = job.output_payload if isinstance(job.output_payload, dict) else {}
    original_summary = original.get("trace_summary") if isinstance(original.get("trace_summary"), dict) else {}
    replay_summary = result.trace_summary
    if original.get("execution_mode") == "hybrid":
        replay_summary = {
            **result.trace_summary,
            **live_invocation_summary(original),
        }
    replay_result = KernelReplayResult(
        replay_passed=original_summary == replay_summary,
        original_summary=original_summary,
        replay_summary=replay_summary,
    )
    await append_event(
        session,
        job,
        event_type="simulation_replayed",
        payload=replay_result.to_payload(),
        message="user-created kernel simulation replayed",
        status=job.status,
        stage="replay",
        severity="info" if replay_result.replay_passed else "warning",
        user_id=user.id,
        actor_type="simulator",
        actor_id="user_kernel_replay",
        source_type="user_kernel_simulation",
        source_id=str((job.metadata_json or {}).get("simulation_type") or ""),
        commit=True,
    )
    await session.refresh(job)
    return KernelSimulationReplayOut(
        job=serialize_job(job),
        replay_passed=replay_result.replay_passed,
        original_summary=replay_result.original_summary,
        replay_summary=replay_result.replay_summary,
    )
