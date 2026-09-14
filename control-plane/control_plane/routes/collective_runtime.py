"""Collective runtime status, protocol, and routing-readiness APIs."""
from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..collective_runtime_scoring import (
    aggregate_scores,
    score_work_job,
)
from ..control_room import get_or_create_policy, policy_dict
from ..models import (
    Agent,
    AgentMemoryEntry,
    AgentReviewRun,
    DagRun,
    DagRunNode,
    User,
    WorkEvent,
    WorkJob,
)
from ..job_wake import nudge_agent_api_worker
from ..work_ledger import append_event, complete_job, create_job, fail_job
from ..collective_runtime_gate import (
    AGENT_API_RUN_KIND,
    collective_runtime_status,
    require_collective_runtime_enabled,
)

router = APIRouter(prefix="/v1/me/collective-runtime", tags=["collective-runtime"])

COLLECTIVE_RUNTIME_RUN_KIND = "collective_runtime_run"
COLLECTIVE_RUNTIME_QUEUE = "collective-runtime"

_COMPLETE_STATUSES = {"complete", "completed", "passed", "success", "succeeded"}
_FAILED_STATUSES = {"error", "failed", "errored", "blocked", "cancelled", "canceled"}
_ACTIVE_STATUSES = {"queued", "running", "pending", "leased"}


class RuntimeProtocolOut(BaseModel):
    id: str
    label: str
    description: str
    use_when: list[str]
    roles: list[str]
    required_evidence: list[str]
    review_gates: list[str]
    human_controls: list[str]
    budget_class: Literal["small", "medium", "large"]


class AgentRegistryItemOut(BaseModel):
    agent_id: int
    name: str
    status: str
    public: bool
    description: str
    capabilities: list[str]
    tools: list[str]
    preferred_task_types: list[str]
    total_jobs: int
    completed_jobs: int
    failed_jobs: int
    success_rate: float
    review_critical_count: int
    review_warning_count: int
    memory_count: int
    dag_node_count: int
    outcome_score: float
    trust_score: float
    routing_state: Literal["trusted", "candidate", "degraded", "blocked"]
    last_activity_at: str | None = None


class CollectiveRuntimeStatusOut(BaseModel):
    ok: bool = True
    runtime: dict[str, Any]
    summary: dict[str, Any]


class CollectiveRuntimeRegistryOut(BaseModel):
    agents: list[AgentRegistryItemOut]


class OutcomeScoreOut(BaseModel):
    job_id: str | None
    kind: str | None
    status: str | None
    worker_name: str | None
    protocol_id: str
    score: float
    reasons: list[str]
    metrics: dict[str, Any]


class ScoreAggregateOut(BaseModel):
    id: str
    label: str | None = None
    count: int
    average_score: float
    completed_count: int
    failed_count: int
    in_flight_count: int


class CollectiveRuntimeScorecardsOut(BaseModel):
    recent_jobs: list[OutcomeScoreOut]
    protocols: list[ScoreAggregateOut]
    agents: list[ScoreAggregateOut]


class CollectiveRuntimeMemoryOut(BaseModel):
    agent_name: str
    namespace: str
    key: str
    value: dict[str, Any]
    metadata: dict[str, Any]
    updated_at: str | None


class MemoryExtractionIn(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    include_failed: bool = True


class MemoryExtractionOut(BaseModel):
    ok: bool = True
    created_count: int
    updated_count: int
    skipped_count: int
    memories: list[CollectiveRuntimeMemoryOut]


class CollectiveRuntimePlanIn(BaseModel):
    goal: str = Field(..., min_length=1, max_length=16000)
    risk: Literal["low", "medium", "high"] = "medium"
    budget_class: Literal["small", "medium", "large"] | None = None
    max_agents: int = Field(default=5, ge=1, le=12)


class CollectiveRuntimePlanOut(BaseModel):
    protocol_id: str
    protocol_label: str
    topology: list[dict[str, Any]]
    selected_agents: list[AgentRegistryItemOut]
    human_controls: list[str]
    approval_gates: list[dict[str, Any]]
    memory_refs: list[dict[str, Any]]
    notes: list[str]


class CollectiveRuntimeRunIn(CollectiveRuntimePlanIn):
    thread_id: str | None = Field(default=None, max_length=160)
    approved_gate_ids: list[str] = Field(default_factory=list, max_length=32)
    idempotency_key: str | None = Field(default=None, max_length=255)


class CollectiveRuntimeApproveIn(BaseModel):
    approved_gate_ids: list[str] = Field(default_factory=list, max_length=32)


class CollectiveRuntimeRunNodeOut(BaseModel):
    node_id: str
    role: str
    agent_name: str
    skill_name: str
    deps: list[str]
    status: str
    child_job_id: str | None = None
    summary: str | None = None
    result: dict[str, Any]
    file_ops: list[dict[str, Any]]


class CollectiveRuntimeRunJobOut(BaseModel):
    job_id: str
    node_id: str | None = None
    role: str | None = None
    agent_name: str | None = None
    skill_name: str | None = None
    status: str
    summary: str | None = None
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class CollectiveRuntimeRunOut(BaseModel):
    dag_run_id: str
    job_id: str | None
    status: str
    goal: str
    protocol_id: str
    protocol_label: str
    topology: list[dict[str, Any]]
    approval_gates: list[dict[str, Any]]
    approved_gate_ids: list[str]
    blocked_gate_ids: list[str]
    memory_refs: list[dict[str, Any]]
    nodes: list[CollectiveRuntimeRunNodeOut]
    child_jobs: list[CollectiveRuntimeRunJobOut]
    notes: list[str]
    summary: str | None
    created_at: str | None
    updated_at: str | None
    completed_at: str | None


_PROTOCOLS: tuple[RuntimeProtocolOut, ...] = (
    RuntimeProtocolOut(
        id="single_agent_task",
        label="Single agent task",
        description=(
            "Use one capable agent when the task is narrow, low-risk, and "
            "evidence requirements are light."
        ),
        use_when=["small direct tasks", "low-risk edits", "simple answers"],
        roles=["executor"],
        required_evidence=["work_job", "final_summary"],
        review_gates=[],
        human_controls=["ask on low confidence"],
        budget_class="small",
    ),
    RuntimeProtocolOut(
        id="research_parallel_scouts",
        label="Research parallel scouts",
        description=(
            "Fan out independent research agents, then synthesize and review "
            "their findings."
        ),
        use_when=["research", "market scan", "technical comparison", "uncertain facts"],
        roles=["planner", "scout", "scout", "synthesizer", "critic"],
        required_evidence=["source_refs", "scout_notes", "synthesis", "critic_review"],
        review_gates=["critic_review"],
        human_controls=["ask before relying on weak or stale evidence"],
        budget_class="medium",
    ),
    RuntimeProtocolOut(
        id="code_change_with_review",
        label="Code change with review",
        description=(
            "Plan, implement, review, and verify a code change with evidence "
            "tied to work ledger and DAG nodes."
        ),
        use_when=["bug fix", "feature implementation", "refactor", "test repair"],
        roles=["planner", "coder", "reviewer", "verifier", "synthesizer"],
        required_evidence=["changed_files", "test_output", "review_findings", "final_summary"],
        review_gates=["reviewer_approval", "verification_result"],
        human_controls=["ask before merge", "ask on sensitive file changes"],
        budget_class="medium",
    ),
    RuntimeProtocolOut(
        id="security_review",
        label="Security review",
        description=(
            "Route risky changes through a stricter reviewer/verifier path "
            "with human approval before action."
        ),
        use_when=["security", "auth", "payments", "secrets", "permissions"],
        roles=[
            "planner",
            "security_reviewer",
            "implementer",
            "verifier",
            "human_approver",
        ],
        required_evidence=["threat_notes", "changed_files", "security_findings", "approval"],
        review_gates=["security_reviewer_approval", "human_approval"],
        human_controls=["always ask before merge", "ask before secret or permission changes"],
        budget_class="large",
    ),
    RuntimeProtocolOut(
        id="docs_update",
        label="Docs update",
        description="Update documentation with a lightweight reviewer and source/evidence check.",
        use_when=["docs", "runbook", "copy", "explanation"],
        roles=["writer", "reviewer"],
        required_evidence=["source_refs", "changed_files", "review_notes"],
        review_gates=["reviewer_check"],
        human_controls=["ask before publishing public claims"],
        budget_class="small",
    ),
)


@router.get("", response_model=CollectiveRuntimeStatusOut)
async def collective_runtime_overview(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectiveRuntimeStatusOut:
    runtime = await collective_runtime_status(session)
    agent_count = await _count_user_visible_agents(session, user)
    recent_dag_runs = (
        await session.execute(
            select(DagRun)
            .where(DagRun.user_id == user.id)
            .order_by(desc(DagRun.id))
            .limit(25)
        )
    ).scalars().all()
    return CollectiveRuntimeStatusOut(
        runtime=runtime,
        summary={
            "agent_count": agent_count,
            "protocol_count": len(_PROTOCOLS),
            "recent_dag_run_count": len(recent_dag_runs),
            "recent_failed_dag_run_count": sum(
                1 for run in recent_dag_runs if run.status in _FAILED_STATUSES
            ),
        },
    )


@router.get("/protocols", response_model=list[RuntimeProtocolOut])
async def list_collective_runtime_protocols(
    _user: User = Depends(current_user),
) -> list[RuntimeProtocolOut]:
    return list(_PROTOCOLS)


@router.get("/registry", response_model=CollectiveRuntimeRegistryOut)
async def collective_runtime_registry(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=200),
) -> CollectiveRuntimeRegistryOut:
    return CollectiveRuntimeRegistryOut(
        agents=await _registry_items(session=session, user=user, limit=limit)
    )


@router.get("/scorecards", response_model=CollectiveRuntimeScorecardsOut)
async def collective_runtime_scorecards(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=1, le=500),
) -> CollectiveRuntimeScorecardsOut:
    jobs = (
        await session.execute(
            select(WorkJob)
            .where(WorkJob.user_id == user.id)
            .order_by(desc(WorkJob.id))
            .limit(limit)
        )
    ).scalars().all()
    events_by_job = await _events_by_job(session, [job.job_id for job in jobs])
    scores = [
        score_work_job(job, events_by_job.get(job.job_id, []))
        for job in jobs
    ]
    return CollectiveRuntimeScorecardsOut(
        recent_jobs=[OutcomeScoreOut(**item) for item in scores],
        protocols=_aggregates_by_key(
            scores,
            key="protocol_id",
            labels={protocol.id: protocol.label for protocol in _PROTOCOLS},
        ),
        agents=_aggregates_by_key(scores, key="worker_name"),
    )


@router.get("/memories", response_model=list[CollectiveRuntimeMemoryOut])
async def list_collective_runtime_memories(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CollectiveRuntimeMemoryOut]:
    rows = (
        await session.execute(
            select(AgentMemoryEntry)
            .where(
                AgentMemoryEntry.user_id == user.id,
                AgentMemoryEntry.namespace.in_([
                    "collective-runtime",
                    "collective-runtime-feedback",
                ]),
            )
            .order_by(AgentMemoryEntry.updated_at.desc(), AgentMemoryEntry.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_memory_out(row) for row in rows]


@router.post("/memories/extract", response_model=MemoryExtractionOut)
async def extract_collective_runtime_memories(
    body: MemoryExtractionIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> MemoryExtractionOut:
    jobs = (
        await session.execute(
            select(WorkJob)
            .where(WorkJob.user_id == user.id, WorkJob.worker_name.is_not(None))
            .order_by(desc(WorkJob.id))
            .limit(body.limit)
        )
    ).scalars().all()
    if not body.include_failed:
        jobs = [job for job in jobs if str(job.status or "").lower() not in _FAILED_STATUSES]

    names = sorted({str(job.worker_name) for job in jobs if job.worker_name})
    agents = (
        await session.execute(
            select(Agent).where(Agent.name.in_(names))
        )
    ).scalars().all() if names else []
    agents_by_name = {agent.name: agent for agent in agents}
    events_by_job = await _events_by_job(session, [job.job_id for job in jobs])

    created = 0
    updated = 0
    skipped = 0
    out: list[CollectiveRuntimeMemoryOut] = []
    for job in jobs:
        agent = agents_by_name.get(str(job.worker_name or ""))
        if agent is None:
            skipped += 1
            continue
        score = score_work_job(job, events_by_job.get(job.job_id, []))
        value = _memory_value_from_score(job, score)
        row = (
            await session.execute(
                select(AgentMemoryEntry).where(
                    AgentMemoryEntry.agent_id == agent.id,
                    AgentMemoryEntry.user_id == user.id,
                    AgentMemoryEntry.namespace == "collective-runtime",
                    AgentMemoryEntry.key == f"work-jobs/{job.job_id}",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = AgentMemoryEntry(
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                namespace="collective-runtime",
                key=f"work-jobs/{job.job_id}",
                value=value,
                metadata_json={
                    "source": "collective_runtime_memory_extraction",
                    "job_id": job.job_id,
                    "protocol_id": score["protocol_id"],
                },
            )
            session.add(row)
            created += 1
        else:
            row.value = value
            row.metadata_json = {
                "source": "collective_runtime_memory_extraction",
                "job_id": job.job_id,
                "protocol_id": score["protocol_id"],
            }
            updated += 1
        await session.flush()
        out.append(_memory_out(row))
    await session.commit()
    return MemoryExtractionOut(
        created_count=created,
        updated_count=updated,
        skipped_count=skipped,
        memories=out,
    )


@router.post("/plan", response_model=CollectiveRuntimePlanOut)
async def plan_collective_runtime_topology(
    body: CollectiveRuntimePlanIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectiveRuntimePlanOut:
    return await _build_plan(body=body, user=user, session=session)


@router.post("/runs", response_model=CollectiveRuntimeRunOut)
async def create_collective_runtime_run(
    body: CollectiveRuntimeRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectiveRuntimeRunOut:
    await require_collective_runtime_enabled(session)
    plan = await _build_plan(body=body, user=user, session=session)
    if not plan.selected_agents:
        raise HTTPException(409, "no eligible agents available for collective runtime run")

    approved_gate_ids = sorted({str(item) for item in body.approved_gate_ids if str(item)})
    blocked_gate_ids = _blocked_gate_ids(plan.approval_gates, approved_gate_ids)
    dag_run_id = f"cr-{uuid4().hex[:16]}"
    status = "blocked" if blocked_gate_ids else "running"
    protocol = _protocol(plan.protocol_id)
    topology = _topology_with_runtime_args(
        topology=plan.topology,
        goal=body.goal,
        protocol=protocol,
        approval_gates=plan.approval_gates,
        approved_gate_ids=approved_gate_ids,
        memory_refs=plan.memory_refs,
    )
    parent = await create_job(
        session,
        user_id=user.id,
        kind=COLLECTIVE_RUNTIME_RUN_KIND,
        payload={
            "goal": body.goal,
            "protocol_id": plan.protocol_id,
            "dag_run_id": dag_run_id,
            "topology": topology,
            "approval_gates": plan.approval_gates,
            "approved_gate_ids": approved_gate_ids,
            "blocked_gate_ids": blocked_gate_ids,
            "memory_refs": plan.memory_refs,
        },
        title=f"Collective runtime: {protocol.label}",
        metadata={
            "source": "collective_runtime",
            "protocol_id": plan.protocol_id,
            "protocol_label": protocol.label,
            "dag_run_id": dag_run_id,
            "client_idempotency_key": body.idempotency_key,
            "approval_gates": plan.approval_gates,
            "approved_gate_ids": approved_gate_ids,
            "blocked_gate_ids": blocked_gate_ids,
            "memory_refs": plan.memory_refs,
        },
        job_id=f"crj-{uuid4().hex[:16]}",
        status=status,
        queue=COLLECTIVE_RUNTIME_QUEUE,
        thread_id=body.thread_id,
        source_type="collective_runtime",
        source_id=plan.protocol_id,
        subject_type="dag_run",
        subject_id=dag_run_id,
        worker_type="orchestrator",
        worker_name="collective_runtime",
        commit=False,
    )
    run = DagRun(
        dag_run_id=dag_run_id,
        user_id=user.id,
        thread_id=body.thread_id,
        goal=body.goal,
        status=status,
        summary=(
            "Blocked by required approval gates"
            if blocked_gate_ids
            else "Collective runtime DAG queued"
        ),
        nodes_json=topology,
    )
    session.add(run)
    await session.flush()
    for raw in topology:
        node = DagRunNode(
            dag_run_id=dag_run_id,
            node_id=str(raw["id"]),
            user_id=user.id,
            agent_name=str(raw["agent"]),
            skill_name=str(raw.get("skill") or "turn"),
            deps=list(raw.get("deps") or []),
            args_json=json.dumps(raw.get("args") or {}, ensure_ascii=False),
            status="blocked" if blocked_gate_ids else "pending",
            result={},
            file_ops=[],
        )
        session.add(node)
    await append_event(
        session,
        parent,
        event_type="collective_runtime_run_planned",
        payload={
            "dag_run_id": dag_run_id,
            "protocol_id": plan.protocol_id,
            "blocked_gate_ids": blocked_gate_ids,
            "node_count": len(topology),
        },
        status=status,
        user_id=user.id,
        stage="planned",
        commit=False,
    )
    await session.flush()
    if not blocked_gate_ids:
        await _advance_runtime_run(session=session, user=user, run=run, parent=parent)
    await session.commit()
    await nudge_agent_api_worker()
    return await _runtime_run_out(session=session, user=user, run=run)


@router.get("/runs", response_model=list[CollectiveRuntimeRunOut])
async def list_collective_runtime_runs(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=100),
) -> list[CollectiveRuntimeRunOut]:
    runs = (
        await session.execute(
            select(DagRun)
            .where(DagRun.user_id == user.id, DagRun.dag_run_id.like("cr-%"))
            .order_by(desc(DagRun.id))
            .limit(limit)
        )
    ).scalars().all()
    return [await _runtime_run_out(session=session, user=user, run=run) for run in runs]


@router.get("/runs/{dag_run_id}", response_model=CollectiveRuntimeRunOut)
async def get_collective_runtime_run(
    dag_run_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectiveRuntimeRunOut:
    run = await _get_runtime_run(session=session, user=user, dag_run_id=dag_run_id)
    return await _runtime_run_out(session=session, user=user, run=run)


@router.post("/runs/{dag_run_id}/approve", response_model=CollectiveRuntimeRunOut)
async def approve_collective_runtime_run(
    dag_run_id: str,
    body: CollectiveRuntimeApproveIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectiveRuntimeRunOut:
    await require_collective_runtime_enabled(session)
    run = await _get_runtime_run(session=session, user=user, dag_run_id=dag_run_id)
    parent = await _runtime_parent_job(session=session, user=user, dag_run_id=dag_run_id)
    if parent is None:
        raise HTTPException(404, "collective runtime parent job not found")
    metadata = dict(parent.metadata_json or {})
    approval_gates = list(metadata.get("approval_gates") or [])
    approved_gate_ids = sorted({
        *[str(item) for item in metadata.get("approved_gate_ids") or [] if str(item)],
        *[str(item) for item in body.approved_gate_ids if str(item)],
    })
    blocked_gate_ids = _blocked_gate_ids(approval_gates, approved_gate_ids)
    metadata["approved_gate_ids"] = approved_gate_ids
    metadata["blocked_gate_ids"] = blocked_gate_ids
    parent.metadata_json = metadata
    payload = dict(parent.input_payload or {})
    payload["approved_gate_ids"] = approved_gate_ids
    payload["blocked_gate_ids"] = blocked_gate_ids
    parent.input_payload = payload
    if blocked_gate_ids:
        await append_event(
            session,
            parent,
            event_type="collective_runtime_run_approval_partial",
            payload={
                "dag_run_id": dag_run_id,
                "approved_gate_ids": approved_gate_ids,
                "blocked_gate_ids": blocked_gate_ids,
            },
            status="blocked",
            user_id=user.id,
            stage="approval",
            commit=True,
        )
        return await _runtime_run_out(session=session, user=user, run=run)

    nodes = await _runtime_nodes(session=session, user=user, dag_run_id=dag_run_id)
    for node in nodes:
        if node.status == "blocked":
            node.status = "pending"
    run.status = "running"
    run.summary = "Approval gates cleared; collective runtime DAG queued"
    parent.status = "running"
    parent.summary = run.summary
    await append_event(
        session,
        parent,
        event_type="collective_runtime_run_approved",
        payload={"dag_run_id": dag_run_id, "approved_gate_ids": approved_gate_ids},
        status="running",
        user_id=user.id,
        stage="approval",
        commit=False,
    )
    await _advance_runtime_run(session=session, user=user, run=run, parent=parent)
    await session.commit()
    await nudge_agent_api_worker()
    return await _runtime_run_out(session=session, user=user, run=run)


@router.post("/runs/{dag_run_id}/advance", response_model=CollectiveRuntimeRunOut)
async def advance_collective_runtime_run(
    dag_run_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CollectiveRuntimeRunOut:
    await require_collective_runtime_enabled(session)
    run = await _get_runtime_run(session=session, user=user, dag_run_id=dag_run_id)
    parent = await _runtime_parent_job(session=session, user=user, dag_run_id=dag_run_id)
    if parent is None:
        raise HTTPException(404, "collective runtime parent job not found")
    if run.status == "blocked":
        return await _runtime_run_out(session=session, user=user, run=run)
    await _advance_runtime_run(session=session, user=user, run=run, parent=parent)
    await session.commit()
    await nudge_agent_api_worker()
    return await _runtime_run_out(session=session, user=user, run=run)


async def _build_plan(
    *,
    body: CollectiveRuntimePlanIn,
    user: User,
    session: AsyncSession,
) -> CollectiveRuntimePlanOut:
    registry = await _registry_items(session=session, user=user, limit=200)
    protocol = _select_protocol(body.goal, body.risk, body.budget_class)
    selected = _select_agents_for_protocol(registry, protocol, body.max_agents)
    topology = _topology_for_protocol(protocol, selected)
    policy = await get_or_create_policy(session, user.id)
    approval_gates = _approval_gates(
        goal=body.goal,
        risk=body.risk,
        protocol=protocol,
        selected_agents=selected,
        policy=policy_dict(policy),
    )
    memory_refs = await _memory_refs_for_goal(
        session=session,
        user=user,
        goal=body.goal,
        protocol=protocol,
        limit=5,
    )
    notes = []
    if not selected:
        notes.append(
            "No eligible agents found for this user; create or install agents "
            "before executing."
        )
    if not await _runtime_enabled(session):
        notes.append("Collective runtime is disabled; this plan is a preview only.")
    return CollectiveRuntimePlanOut(
        protocol_id=protocol.id,
        protocol_label=protocol.label,
        topology=topology,
        selected_agents=selected,
        human_controls=protocol.human_controls,
        approval_gates=approval_gates,
        memory_refs=memory_refs,
        notes=notes,
    )


async def _count_user_visible_agents(session: AsyncSession, user: User) -> int:
    rows = (
        await session.execute(
            select(Agent.id)
            .where(or_(Agent.owner_id == user.id, Agent.public.is_(True)))
            .limit(1000)
        )
    ).all()
    return len(rows)


async def _runtime_enabled(session: AsyncSession) -> bool:
    return bool((await collective_runtime_status(session)).get("enabled"))


async def _registry_items(
    *,
    session: AsyncSession,
    user: User,
    limit: int,
) -> list[AgentRegistryItemOut]:
    agents = (
        await session.execute(
            select(Agent)
            .where(or_(Agent.owner_id == user.id, Agent.public.is_(True)))
            .order_by(Agent.updated_at.desc(), Agent.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    names = [agent.name for agent in agents]
    if not names:
        return []

    jobs = (
        await session.execute(
            select(WorkJob)
            .where(WorkJob.user_id == user.id, WorkJob.worker_name.in_(names))
            .order_by(desc(WorkJob.id))
            .limit(2000)
        )
    ).scalars().all()
    reviews = (
        await session.execute(
            select(AgentReviewRun)
            .where(AgentReviewRun.agent_name.in_(names))
            .where(
                or_(AgentReviewRun.user_id == user.id, AgentReviewRun.user_id.is_(None))
            )
            .order_by(desc(AgentReviewRun.id))
            .limit(1000)
        )
    ).scalars().all()
    memories = (
        await session.execute(
            select(AgentMemoryEntry)
            .where(AgentMemoryEntry.user_id == user.id, AgentMemoryEntry.agent_name.in_(names))
            .order_by(desc(AgentMemoryEntry.id))
            .limit(2000)
        )
    ).scalars().all()
    dag_nodes = (
        await session.execute(
            select(DagRunNode)
            .where(DagRunNode.user_id == user.id, DagRunNode.agent_name.in_(names))
            .order_by(desc(DagRunNode.id))
            .limit(2000)
        )
    ).scalars().all()
    events_by_job = await _events_by_job(session, [job.job_id for job in jobs])

    jobs_by_agent = _group_by(jobs, "worker_name")
    reviews_by_agent = _group_by(reviews, "agent_name")
    memories_by_agent = _group_by(memories, "agent_name")
    dag_nodes_by_agent = _group_by(dag_nodes, "agent_name")

    return [
        _agent_registry_item(
            agent,
            jobs=jobs_by_agent.get(agent.name, []),
            events_by_job=events_by_job,
            reviews=reviews_by_agent.get(agent.name, []),
            memories=memories_by_agent.get(agent.name, []),
            dag_nodes=dag_nodes_by_agent.get(agent.name, []),
        )
        for agent in agents
    ]


def _group_by(rows: list[Any], attr: str) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        key = getattr(row, attr, None)
        if isinstance(key, str) and key:
            grouped.setdefault(key, []).append(row)
    return grouped


async def _events_by_job(
    session: AsyncSession,
    job_ids: list[str],
) -> dict[str, list[WorkEvent]]:
    if not job_ids:
        return {}
    events = (
        await session.execute(
            select(WorkEvent)
            .where(WorkEvent.job_id.in_(job_ids))
            .order_by(WorkEvent.job_id.asc(), WorkEvent.event_seq.asc(), WorkEvent.id.asc())
            .limit(5000)
        )
    ).scalars().all()
    grouped: dict[str, list[WorkEvent]] = {job_id: [] for job_id in job_ids}
    for event in events:
        grouped.setdefault(event.job_id, []).append(event)
    return grouped


def _aggregates_by_key(
    scores: list[dict[str, Any]],
    *,
    key: str,
    labels: dict[str, str] | None = None,
) -> list[ScoreAggregateOut]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        raw = score.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        grouped.setdefault(raw, []).append(score)
    rows = []
    for group_id, group_scores in grouped.items():
        aggregate = aggregate_scores(group_scores)
        rows.append(
            ScoreAggregateOut(
                id=group_id,
                label=(labels or {}).get(group_id),
                **aggregate,
            )
        )
    rows.sort(key=lambda item: (item.average_score, item.count), reverse=True)
    return rows


def _memory_out(row: AgentMemoryEntry) -> CollectiveRuntimeMemoryOut:
    return CollectiveRuntimeMemoryOut(
        agent_name=row.agent_name,
        namespace=row.namespace,
        key=row.key,
        value=row.value if isinstance(row.value, dict) else {"value": row.value},
        metadata=row.metadata_json or {},
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


def _memory_value_from_score(job: WorkJob, score: dict[str, Any]) -> dict[str, Any]:
    good = float(score.get("score") or 0.0) >= 0.7
    reasons = [str(item) for item in score.get("reasons") or []]
    lesson = (
        "Repeat this pattern for similar work: " + "; ".join(reasons[:4])
        if good
        else "Use caution on similar work: " + "; ".join(reasons[:4])
    )
    return {
        "lesson": lesson,
        "job_id": job.job_id,
        "title": job.title,
        "summary": job.summary,
        "protocol_id": score.get("protocol_id"),
        "score": score.get("score"),
        "reasons": reasons,
        "metrics": score.get("metrics") or {},
        "status": job.status,
        "kind": job.kind,
        "subject": {
            "type": job.subject_type,
            "id": job.subject_id,
        },
    }


async def _memory_refs_for_goal(
    *,
    session: AsyncSession,
    user: User,
    goal: str,
    protocol: RuntimeProtocolOut,
    limit: int,
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(AgentMemoryEntry)
            .where(
                AgentMemoryEntry.user_id == user.id,
                AgentMemoryEntry.namespace.in_([
                    "collective-runtime",
                    "collective-runtime-feedback",
                ]),
            )
            .order_by(AgentMemoryEntry.updated_at.desc(), AgentMemoryEntry.id.desc())
            .limit(50)
        )
    ).scalars().all()
    if not rows:
        return []

    goal_terms = set(_keywords(goal)) | set(protocol.use_when) | {protocol.id}
    scored: list[tuple[int, AgentMemoryEntry]] = []
    for row in rows:
        value = row.value if isinstance(row.value, dict) else {}
        text = " ".join([
            str(value.get("lesson") or ""),
            str(value.get("title") or ""),
            str(value.get("summary") or ""),
            str(value.get("protocol_id") or ""),
            row.agent_name,
        ]).lower()
        overlap = sum(1 for term in goal_terms if term.lower() in text)
        scored.append((overlap, row))
    scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
    refs = []
    for overlap, row in scored[:limit]:
        value = row.value if isinstance(row.value, dict) else {}
        refs.append({
            "agent_name": row.agent_name,
            "namespace": row.namespace,
            "key": row.key,
            "relevance": overlap,
            "lesson": value.get("lesson"),
            "score": value.get("score"),
            "protocol_id": value.get("protocol_id"),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })
    return refs


def _agent_registry_item(
    agent: Agent,
    *,
    jobs: list[WorkJob],
    events_by_job: dict[str, list[WorkEvent]],
    reviews: list[AgentReviewRun],
    memories: list[AgentMemoryEntry],
    dag_nodes: list[DagRunNode],
) -> AgentRegistryItemOut:
    completed_jobs = sum(1 for job in jobs if job.status in _COMPLETE_STATUSES)
    failed_jobs = sum(1 for job in jobs if job.status in _FAILED_STATUSES or job.error)
    total_jobs = len(jobs)
    success_rate = completed_jobs / total_jobs if total_jobs else 0.0
    outcome_scores = [
        score_work_job(job, events_by_job.get(job.job_id, []))
        for job in jobs
    ]
    outcome_score = float(aggregate_scores(outcome_scores)["average_score"])
    critical = sum(int(review.critical_count or 0) for review in reviews)
    warnings = sum(int(review.warning_count or 0) for review in reviews)
    trust_score = _trust_score(
        agent=agent,
        total_jobs=total_jobs,
        completed_jobs=completed_jobs,
        failed_jobs=failed_jobs,
        outcome_score=outcome_score,
        critical_count=critical,
        warning_count=warnings,
        dag_node_count=len(dag_nodes),
    )
    return AgentRegistryItemOut(
        agent_id=agent.id,
        name=agent.name,
        status=agent.status,
        public=bool(agent.public),
        description=agent.description or "",
        capabilities=_capabilities(agent),
        tools=_tools(agent),
        preferred_task_types=_preferred_task_types(agent),
        total_jobs=total_jobs,
        completed_jobs=completed_jobs,
        failed_jobs=failed_jobs,
        success_rate=round(success_rate, 4),
        review_critical_count=critical,
        review_warning_count=warnings,
        memory_count=len(memories),
        dag_node_count=len(dag_nodes),
        outcome_score=outcome_score,
        trust_score=trust_score,
        routing_state=_routing_state(agent, trust_score, critical),
        last_activity_at=_last_activity(jobs, dag_nodes),
    )


def _capabilities(agent: Agent) -> list[str]:
    card = agent.card if isinstance(agent.card, dict) else {}
    raw = card.get("capabilities") or card.get("skills") or []
    capabilities: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                capabilities.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id") or item.get("title")
                if isinstance(name, str):
                    capabilities.append(name)
    if not capabilities and agent.description:
        capabilities.extend(_keywords(agent.description))
    return sorted(set(capabilities))[:20]


def _tools(agent: Agent) -> list[str]:
    card = agent.card if isinstance(agent.card, dict) else {}
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    raw = runtime.get("tools_used") or card.get("tools") or []
    if not isinstance(raw, list):
        return []
    return sorted({str(item) for item in raw if str(item).strip()})[:30]


def _preferred_task_types(agent: Agent) -> list[str]:
    text = " ".join([agent.name, agent.description or "", " ".join(_capabilities(agent))]).lower()
    out = []
    for key, markers in {
        "code": ("code", "repo", "bug", "test", "edit", "refactor"),
        "research": ("research", "search", "browser", "market", "source"),
        "review": ("review", "critic", "security", "audit", "verify"),
        "docs": ("docs", "write", "content", "copy"),
        "ops": ("deploy", "schedule", "monitor", "ops"),
    }.items():
        if any(marker in text for marker in markers):
            out.append(key)
    return out or ["general"]


def _trust_score(
    *,
    agent: Agent,
    total_jobs: int,
    completed_jobs: int,
    failed_jobs: int,
    outcome_score: float,
    critical_count: int,
    warning_count: int,
    dag_node_count: int,
) -> float:
    score = 0.45
    if agent.status == "running":
        score += 0.1
    if total_jobs:
        score += 0.35 * (completed_jobs / total_jobs)
        score -= 0.3 * (failed_jobs / total_jobs)
        score += 0.2 * (outcome_score - 0.5)
    if dag_node_count:
        score += min(0.1, dag_node_count * 0.01)
    score -= min(0.3, critical_count * 0.08 + warning_count * 0.015)
    return round(max(0.0, min(1.0, score)), 4)


def _routing_state(
    agent: Agent,
    trust_score: float,
    critical_count: int,
) -> Literal["trusted", "candidate", "degraded", "blocked"]:
    if agent.status not in {"running", "ready", "active"}:
        return "blocked"
    if critical_count >= 3 or trust_score < 0.25:
        return "degraded"
    if trust_score >= 0.72:
        return "trusted"
    return "candidate"


def _last_activity(jobs: list[WorkJob], dag_nodes: list[DagRunNode]) -> str | None:
    values = [row.updated_at for row in jobs if row.updated_at] + [
        row.completed_at or row.started_at
        for row in dag_nodes
        if row.completed_at or row.started_at
    ]
    if not values:
        return None
    return max(values).isoformat()


def _keywords(text: str) -> list[str]:
    words = [word.strip(".,:;()[]{}").lower() for word in text.split()]
    return [word for word in words if len(word) > 4][:8]


def _select_protocol(
    goal: str,
    risk: str,
    budget_class: str | None,
) -> RuntimeProtocolOut:
    text = goal.lower()
    if risk == "high" or any(
        item in text
        for item in ("security", "auth", "secret", "permission", "payment")
    ):
        return _protocol("security_review")
    if any(
        item in text
        for item in ("bug", "fix", "implement", "code", "test", "refactor", "merge")
    ):
        return _protocol("code_change_with_review")
    if any(
        item in text
        for item in ("research", "compare", "market", "source", "latest", "investigate")
    ):
        return _protocol("research_parallel_scouts")
    if any(item in text for item in ("docs", "document", "runbook", "copy", "explain")):
        return _protocol("docs_update")
    if budget_class == "small" and risk == "low":
        return _protocol("single_agent_task")
    return _protocol("code_change_with_review")


def _protocol(protocol_id: str) -> RuntimeProtocolOut:
    return next(item for item in _PROTOCOLS if item.id == protocol_id)


def _select_agents_for_protocol(
    registry: list[AgentRegistryItemOut],
    protocol: RuntimeProtocolOut,
    max_agents: int,
) -> list[AgentRegistryItemOut]:
    wanted = {
        "code_change_with_review": {"code", "review"},
        "security_review": {"code", "review"},
        "research_parallel_scouts": {"research", "review"},
        "docs_update": {"docs", "review"},
        "single_agent_task": {"general", "code", "research", "docs"},
    }.get(protocol.id, {"general"})
    candidates = [
        agent for agent in registry
        if agent.routing_state != "blocked"
        and (
            wanted.intersection(agent.preferred_task_types)
            or "general" in agent.preferred_task_types
        )
    ]
    candidates.sort(
        key=lambda item: (
            item.routing_state == "trusted",
            item.trust_score,
            item.success_rate,
        ),
        reverse=True,
    )
    return candidates[:max_agents]


def _topology_for_protocol(
    protocol: RuntimeProtocolOut,
    agents: list[AgentRegistryItemOut],
) -> list[dict[str, Any]]:
    if not agents:
        return []
    nodes = []
    for index, role in enumerate(protocol.roles):
        agent = agents[min(index, len(agents) - 1)]
        deps = [] if index == 0 else [nodes[-1]["id"]]
        if protocol.id == "research_parallel_scouts" and role == "scout":
            deps = ["planner"] if nodes else []
        node_id = role if role not in {node["id"] for node in nodes} else f"{role}_{index}"
        nodes.append({
            "id": node_id,
            "role": role,
            "agent": agent.name,
            "deps": deps,
            "routing_state": agent.routing_state,
            "trust_score": agent.trust_score,
        })
    return nodes


def _approval_gates(
    *,
    goal: str,
    risk: str,
    protocol: RuntimeProtocolOut,
    selected_agents: list[AgentRegistryItemOut],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    text = goal.lower()
    gates: list[dict[str, Any]] = []

    if risk == "high" or protocol.id == "security_review":
        gates.append(_gate(
            "high_risk_change",
            "High-risk change approval",
            True,
            "Risk is high or the selected protocol is security-sensitive.",
        ))

    touches_code = protocol.id in {"code_change_with_review", "security_review"} or any(
        marker in text for marker in ("code", "file", "repo", "merge", "refactor")
    )
    if touches_code and bool(policy.get("require_approval_for_file_writes")):
        gates.append(_gate(
            "file_write_approval",
            "File write approval",
            True,
            "User policy requires approval before file writes.",
        ))

    degraded = [
        agent.name
        for agent in selected_agents
        if agent.routing_state in {"degraded", "blocked"} or agent.trust_score < 0.45
    ]
    if degraded:
        gates.append(_gate(
            "low_trust_agent",
            "Low-trust agent approval",
            True,
            "Selected agents need approval before execution: " + ", ".join(degraded),
        ))

    approved_agents = set(policy.get("approved_agents") or [])
    if bool(policy.get("only_approved_agents")):
        unapproved = [
            agent.name
            for agent in selected_agents
            if agent.name not in approved_agents
        ]
        gates.append(_gate(
            "approved_agent_policy",
            "Approved-agent policy",
            bool(unapproved),
            (
                "Unapproved selected agents: " + ", ".join(unapproved)
                if unapproved
                else "All selected agents are approved."
            ),
        ))

    if bool(policy.get("deny_external_network")):
        gates.append(_gate(
            "network_restricted",
            "Network restricted",
            False,
            "User policy denies external network access; route only to offline-safe tools.",
        ))

    run_budget = int(policy.get("run_budget_cents") or 0)
    budget_class = protocol.budget_class
    expensive = budget_class == "large" or (budget_class == "medium" and run_budget and run_budget < 500)
    if expensive:
        gates.append(_gate(
            "budget_review",
            "Budget review",
            budget_class == "large",
            f"Protocol budget class is {budget_class}; user run budget is {run_budget} cents.",
        ))

    return gates


def _gate(
    gate_id: str,
    label: str,
    required: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "required": required,
        "reason": reason,
    }


def _blocked_gate_ids(
    approval_gates: list[dict[str, Any]],
    approved_gate_ids: list[str],
) -> list[str]:
    approved = set(approved_gate_ids)
    return [
        str(gate.get("id"))
        for gate in approval_gates
        if gate.get("required") and str(gate.get("id") or "") not in approved
    ]


def _topology_with_runtime_args(
    *,
    topology: list[dict[str, Any]],
    goal: str,
    protocol: RuntimeProtocolOut,
    approval_gates: list[dict[str, Any]],
    approved_gate_ids: list[str],
    memory_refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out = []
    for node in topology:
        role = str(node.get("role") or node.get("id") or "executor")
        agent_name = str(node.get("agent") or "")
        skill = _skill_name_for_role(role=role, agent_name=agent_name, agent_card=None)
        item = dict(node)
        item["skill"] = skill
        item["args"] = {
            "goal": goal,
            "protocol_id": protocol.id,
            "protocol_label": protocol.label,
            "role": role,
            "node_id": str(node.get("id") or role),
            "agent": agent_name,
            "approval_gates": approval_gates,
            "approved_gate_ids": approved_gate_ids,
            "memory_refs": memory_refs,
            "prompt": _node_prompt(
                goal=goal,
                protocol=protocol,
                role=role,
                deps=list(node.get("deps") or []),
                memory_refs=memory_refs,
            ),
        }
        out.append(item)
    return out


async def _get_runtime_run(
    *,
    session: AsyncSession,
    user: User,
    dag_run_id: str,
) -> DagRun:
    run = (
        await session.execute(
            select(DagRun).where(
                DagRun.user_id == user.id,
                DagRun.dag_run_id == dag_run_id,
                DagRun.dag_run_id.like("cr-%"),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "collective runtime run not found")
    return run


async def _runtime_parent_job(
    *,
    session: AsyncSession,
    user: User,
    dag_run_id: str,
) -> WorkJob | None:
    return (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user.id,
                WorkJob.kind == COLLECTIVE_RUNTIME_RUN_KIND,
                WorkJob.subject_type == "dag_run",
                WorkJob.subject_id == dag_run_id,
            )
            .order_by(desc(WorkJob.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _runtime_nodes(
    *,
    session: AsyncSession,
    user: User,
    dag_run_id: str,
) -> list[DagRunNode]:
    return (
        await session.execute(
            select(DagRunNode)
            .where(DagRunNode.user_id == user.id, DagRunNode.dag_run_id == dag_run_id)
            .order_by(DagRunNode.id.asc())
        )
    ).scalars().all()


async def _runtime_child_jobs(
    *,
    session: AsyncSession,
    user: User,
    dag_run_id: str,
) -> list[WorkJob]:
    return (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user.id,
                WorkJob.kind == AGENT_API_RUN_KIND,
                WorkJob.correlation_id == dag_run_id,
            )
            .order_by(WorkJob.id.asc())
        )
    ).scalars().all()


async def _runtime_run_out(
    *,
    session: AsyncSession,
    user: User,
    run: DagRun,
) -> CollectiveRuntimeRunOut:
    parent = await _runtime_parent_job(
        session=session,
        user=user,
        dag_run_id=run.dag_run_id,
    )
    nodes = await _runtime_nodes(session=session, user=user, dag_run_id=run.dag_run_id)
    child_jobs = await _runtime_child_jobs(
        session=session,
        user=user,
        dag_run_id=run.dag_run_id,
    )
    metadata = dict(parent.metadata_json or {}) if parent is not None else {}
    protocol_id = str(metadata.get("protocol_id") or _run_protocol_id(run))
    protocol = _protocol(protocol_id) if any(item.id == protocol_id for item in _PROTOCOLS) else _PROTOCOLS[0]
    approval_gates = list(metadata.get("approval_gates") or [])
    approved_gate_ids = [str(item) for item in metadata.get("approved_gate_ids") or []]
    blocked_gate_ids = _blocked_gate_ids(approval_gates, approved_gate_ids)
    return CollectiveRuntimeRunOut(
        dag_run_id=run.dag_run_id,
        job_id=str(parent.job_id) if parent is not None else None,
        status=run.status,
        goal=run.goal,
        protocol_id=protocol.id,
        protocol_label=protocol.label,
        topology=run.nodes_json or [],
        approval_gates=approval_gates,
        approved_gate_ids=approved_gate_ids,
        blocked_gate_ids=blocked_gate_ids,
        memory_refs=list(metadata.get("memory_refs") or []),
        nodes=[_runtime_node_out(node) for node in nodes],
        child_jobs=[_runtime_child_job_out(job) for job in child_jobs],
        notes=_run_notes(run=run, blocked_gate_ids=blocked_gate_ids),
        summary=run.summary,
        created_at=run.created_at.isoformat() if run.created_at else None,
        updated_at=run.updated_at.isoformat() if run.updated_at else None,
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
    )


def _run_protocol_id(run: DagRun) -> str:
    for node in run.nodes_json or []:
        if not isinstance(node, dict):
            continue
        args = node.get("args") if isinstance(node.get("args"), dict) else {}
        value = args.get("protocol_id") or node.get("protocol_id")
        if isinstance(value, str) and value:
            return value
    return "single_agent_task"


def _runtime_node_out(node: DagRunNode) -> CollectiveRuntimeRunNodeOut:
    args = _node_args(node)
    return CollectiveRuntimeRunNodeOut(
        node_id=node.node_id,
        role=str(args.get("role") or node.node_id),
        agent_name=node.agent_name,
        skill_name=node.skill_name,
        deps=list(node.deps or []),
        status=node.status,
        child_job_id=_str_or_none(args.get("child_job_id")),
        summary=node.summary,
        result=node.result or {},
        file_ops=node.file_ops or [],
    )


def _runtime_child_job_out(job: WorkJob) -> CollectiveRuntimeRunJobOut:
    metadata = dict(job.metadata_json or {})
    return CollectiveRuntimeRunJobOut(
        job_id=job.job_id,
        node_id=_str_or_none(metadata.get("node_id")),
        role=_str_or_none(metadata.get("role")),
        agent_name=job.worker_name,
        skill_name=_str_or_none(metadata.get("skill")),
        status=job.status,
        summary=job.summary,
        error=job.error,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
    )


def _run_notes(*, run: DagRun, blocked_gate_ids: list[str]) -> list[str]:
    notes = []
    if blocked_gate_ids:
        notes.append("Run is blocked until required approval gates are cleared.")
    if run.status in _ACTIVE_STATUSES:
        notes.append("Call advance after child agent jobs complete to queue dependent nodes.")
    return notes


async def _advance_runtime_run(
    *,
    session: AsyncSession,
    user: User,
    run: DagRun,
    parent: WorkJob,
) -> None:
    nodes = await _runtime_nodes(session=session, user=user, dag_run_id=run.dag_run_id)
    child_jobs = await _runtime_child_jobs(
        session=session,
        user=user,
        dag_run_id=run.dag_run_id,
    )
    _sync_nodes_from_child_jobs(nodes=nodes, child_jobs=child_jobs)
    if any(node.status in _FAILED_STATUSES for node in nodes):
        failed = next(node for node in nodes if node.status in _FAILED_STATUSES)
        run.status = "error"
        run.summary = f"Collective runtime node failed: {failed.node_id}"
        await fail_job(
            session,
            parent,
            error=run.summary,
            result={"dag_run_id": run.dag_run_id, "failed_node_id": failed.node_id},
            summary=run.summary,
            user_id=user.id,
            event_type="collective_runtime_run_failed",
            commit=False,
        )
        return
    if nodes and all(node.status in _COMPLETE_STATUSES for node in nodes):
        run.status = "complete"
        run.summary = "Collective runtime DAG complete"
        await complete_job(
            session,
            parent,
            result={"dag_run_id": run.dag_run_id, "node_count": len(nodes)},
            summary=run.summary,
            user_id=user.id,
            status="complete",
            event_type="collective_runtime_run_completed",
            commit=False,
        )
        return

    queued = await _queue_ready_nodes(
        session=session,
        user=user,
        run=run,
        parent=parent,
        nodes=nodes,
        child_jobs=child_jobs,
    )
    run.status = "running"
    run.summary = (
        f"Queued {queued} ready DAG node{'s' if queued != 1 else ''}"
        if queued
        else "Waiting for active DAG nodes"
    )
    parent.status = "running"
    parent.summary = run.summary
    if queued:
        await append_event(
            session,
            parent,
            event_type="collective_runtime_nodes_queued",
            payload={"dag_run_id": run.dag_run_id, "queued_count": queued},
            status="running",
            user_id=user.id,
            stage="dispatch",
            commit=False,
        )


def _sync_nodes_from_child_jobs(
    *,
    nodes: list[DagRunNode],
    child_jobs: list[WorkJob],
) -> None:
    jobs_by_node: dict[str, WorkJob] = {}
    for job in child_jobs:
        metadata = dict(job.metadata_json or {})
        node_id = metadata.get("node_id")
        if isinstance(node_id, str) and node_id:
            jobs_by_node[node_id] = job
    for node in nodes:
        job = jobs_by_node.get(node.node_id)
        if job is None:
            continue
        status = str(job.status or "").lower()
        if status in _COMPLETE_STATUSES:
            node.status = "complete"
            node.summary = job.summary
            node.result = job.output_payload if isinstance(job.output_payload, dict) else {}
            file_ops = node.result.get("file_ops") if isinstance(node.result, dict) else None
            node.file_ops = file_ops if isinstance(file_ops, list) else []
            node.completed_at = job.completed_at
        elif status in _FAILED_STATUSES or job.error:
            node.status = "error"
            node.summary = job.summary or job.error
            node.result = job.error_payload if isinstance(job.error_payload, dict) else {}
            node.completed_at = job.completed_at
        elif status == "running":
            node.status = "running"
            node.started_at = job.started_at
        elif status == "queued":
            node.status = "queued"


async def _queue_ready_nodes(
    *,
    session: AsyncSession,
    user: User,
    run: DagRun,
    parent: WorkJob,
    nodes: list[DagRunNode],
    child_jobs: list[WorkJob],
) -> int:
    existing_node_ids = {
        str((job.metadata_json or {}).get("node_id"))
        for job in child_jobs
        if isinstance(job.metadata_json, dict) and (job.metadata_json or {}).get("node_id")
    }
    node_by_id = {node.node_id: node for node in nodes}
    agents = await _agents_by_name(session, [node.agent_name for node in nodes])
    queued = 0
    for node in nodes:
        if node.status not in {"pending"}:
            continue
        if node.node_id in existing_node_ids:
            continue
        if any(node_by_id.get(dep) is None or node_by_id[dep].status != "complete" for dep in node.deps or []):
            continue
        agent = agents.get(node.agent_name)
        if agent is None:
            node.status = "error"
            node.summary = f"agent not found: {node.agent_name}"
            continue
        child = await _queue_node_child_job(
            session=session,
            user=user,
            run=run,
            parent=parent,
            node=node,
            agent=agent,
            nodes=node_by_id,
        )
        node.status = "queued"
        args = _node_args(node)
        args["child_job_id"] = child.job_id
        args["child_status"] = child.status
        node.args_json = json.dumps(args, ensure_ascii=False)
        queued += 1
    return queued


async def _queue_node_child_job(
    *,
    session: AsyncSession,
    user: User,
    run: DagRun,
    parent: WorkJob,
    node: DagRunNode,
    agent: Agent,
    nodes: dict[str, DagRunNode],
) -> WorkJob:
    args = _node_args(node)
    role = str(args.get("role") or node.node_id)
    skill = _skill_name_for_role(role=role, agent_name=agent.name, agent_card=agent.card)
    node.skill_name = skill
    arguments = dict(args)
    arguments["dependencies"] = _dependency_outputs(node=node, nodes=nodes)
    arguments["dag_run_id"] = run.dag_run_id
    arguments["node_id"] = node.node_id
    arguments["role"] = role
    arguments["agent"] = agent.name
    child_id = f"api-{uuid4().hex[:16]}"
    child = await create_job(
        session,
        user_id=user.id,
        kind=AGENT_API_RUN_KIND,
        payload={"agent": agent.name, "skill": skill, "arguments": arguments},
        title=f"{agent.name}.{skill} ({role})",
        metadata={
            "source": "collective_runtime",
            "agent": agent.name,
            "agent_id": agent.id,
            "skill": skill,
            "role": role,
            "node_id": node.node_id,
            "dag_run_id": run.dag_run_id,
            "protocol_id": arguments.get("protocol_id"),
            "parent_collective_runtime_run": parent.job_id,
        },
        job_id=child_id,
        status="queued",
        queue="agent-api",
        parent_job_id=parent.job_id,
        root_job_id=parent.root_job_id or parent.job_id,
        correlation_id=run.dag_run_id,
        source_type="collective_runtime",
        source_id=run.dag_run_id,
        subject_type="dag_node",
        subject_id=f"{run.dag_run_id}:{node.node_id}",
        worker_type="agent",
        worker_name=agent.name,
        commit=False,
    )
    await append_event(
        session,
        parent,
        event_type="collective_runtime_child_queued",
        payload={
            "dag_run_id": run.dag_run_id,
            "node_id": node.node_id,
            "child_job_id": child.job_id,
            "agent": agent.name,
            "skill": skill,
        },
        message=f"Queued {agent.name}.{skill} for {node.node_id}",
        status="running",
        user_id=user.id,
        stage="dispatch",
        commit=False,
    )
    return child


async def _agents_by_name(session: AsyncSession, names: list[str]) -> dict[str, Agent]:
    clean = sorted({name for name in names if name})
    if not clean:
        return {}
    rows = (
        await session.execute(select(Agent).where(Agent.name.in_(clean)))
    ).scalars().all()
    return {row.name: row for row in rows}


def _node_args(node: DagRunNode) -> dict[str, Any]:
    try:
        value = json.loads(node.args_json or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _dependency_outputs(
    *,
    node: DagRunNode,
    nodes: dict[str, DagRunNode],
) -> list[dict[str, Any]]:
    out = []
    for dep in node.deps or []:
        dep_node = nodes.get(dep)
        if dep_node is None:
            continue
        out.append({
            "node_id": dep_node.node_id,
            "role": _node_args(dep_node).get("role") or dep_node.node_id,
            "agent": dep_node.agent_name,
            "status": dep_node.status,
            "summary": dep_node.summary,
            "result": dep_node.result or {},
            "file_ops": dep_node.file_ops or [],
        })
    return out


def _skill_name_for_role(
    *,
    role: str,
    agent_name: str,
    agent_card: dict[str, Any] | None,
) -> str:
    if agent_name == "code-editor-agent":
        return "turn"
    skills = _agent_skill_names(agent_card)
    preferred = {
        "planner": ("plan", "pursue", "run", "turn", "auto"),
        "coder": ("code", "turn", "pursue", "run", "auto"),
        "implementer": ("code", "turn", "pursue", "run", "auto"),
        "reviewer": ("review", "audit", "verify", "run", "turn", "auto"),
        "security_reviewer": ("review", "audit", "security_review", "verify", "run"),
        "verifier": ("verify", "test", "review", "run", "turn", "auto"),
        "critic": ("review", "critique", "audit", "run", "auto"),
        "scout": ("research", "search", "run", "auto"),
        "synthesizer": ("synthesize", "summarize", "run", "turn", "auto"),
        "writer": ("write", "draft", "run", "auto"),
        "executor": ("run", "turn", "auto"),
        "human_approver": ("review", "run", "auto"),
    }.get(role, ("run", "turn", "auto"))
    for name in preferred:
        if name in skills:
            return name
    return skills[0] if skills else "turn"


def _agent_skill_names(agent_card: dict[str, Any] | None) -> list[str]:
    card = agent_card if isinstance(agent_card, dict) else {}
    raw = card.get("skills")
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
    return names


def _node_prompt(
    *,
    goal: str,
    protocol: RuntimeProtocolOut,
    role: str,
    deps: list[str],
    memory_refs: list[dict[str, Any]],
) -> str:
    memory_lines = [
        f"- {item.get('agent_name')}: {item.get('lesson')}"
        for item in memory_refs[:3]
        if item.get("lesson")
    ]
    pieces = [
        f"Goal: {goal}",
        f"Protocol: {protocol.label} ({protocol.id})",
        f"Your role: {role}",
    ]
    if deps:
        pieces.append("Use completed dependency outputs from: " + ", ".join(deps))
    if memory_lines:
        pieces.append("Relevant runtime memory:\n" + "\n".join(memory_lines))
    pieces.append(
        "Return structured evidence: summary, decisions, risks, tests or checks run, "
        "and any file/artifact references. Do not claim completion without evidence."
    )
    return "\n\n".join(pieces)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
