from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..control_room import (
    get_or_create_policy,
    llm_usage_status,
    month_start,
    monthly_llm_spend_usd,
    policy_dict,
)
from ..db import get_session
from ..models import (
    AgentDeployment,
    AgentDeploymentEvent,
    AgentProofRun,
    DagRun,
    DagRunNode,
    LLMUsageEvent,
    SubagentRun,
    SubagentRunEvent,
    TrialRoom,
    TrialRun,
    User,
    WorkEvent,
    WorkJob,
)
from ..protocol_simulation import PROTOCOL_SIMULATION_KIND, summarize_scenario_trace_payload
from ..trial_rooms import receipt_id

router = APIRouter(prefix="/v1/me/control-room", tags=["control-room"])

_AGENT_ALLOWLIST_NAME_RE = re.compile(r"^[^\s/]{1,128}$")
class ControlPolicyOut(BaseModel):
    monthly_budget_cents: int
    run_budget_cents: int
    max_agent_calls_per_run: int
    require_approval_for_file_writes: bool
    deny_external_network: bool
    only_approved_agents: bool
    pii_safe_mode: bool
    approved_agents: list[str] = Field(default_factory=list)


class ControlPolicyIn(BaseModel):
    monthly_budget_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    run_budget_cents: int | None = Field(default=None, ge=0, le=1_000_000)
    max_agent_calls_per_run: int | None = Field(default=None, ge=1, le=100)
    require_approval_for_file_writes: bool | None = None
    deny_external_network: bool | None = None
    only_approved_agents: bool | None = None
    pii_safe_mode: bool | None = None
    approved_agents: list[str] | None = Field(default=None, max_length=200)


class ControlSummaryOut(BaseModel):
    monthly_spend_cents: int
    monthly_budget_cents: int
    run_budget_cents: int
    llm_calls_month: int
    llm_tokens_month: int
    agent_runs: int
    dag_runs: int
    failures: int
    files_touched: int


class ControlTimelineItemOut(BaseModel):
    id: str
    source: str
    title: str
    status: str
    summary: str | None = None
    agent_name: str | None = None
    skill_name: str | None = None
    cost_cents: int = 0
    token_count: int = 0
    file_ops: list[dict[str, Any]] = Field(default_factory=list)
    receipt_path: str
    created_at: datetime
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ControlRoomOut(BaseModel):
    policy: ControlPolicyOut
    summary: ControlSummaryOut
    timeline: list[ControlTimelineItemOut]


class ReceiptOut(BaseModel):
    receipt_id: str
    source: str
    subject: str
    status: str
    summary: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    ids: dict[str, Any] = Field(default_factory=dict)
    scopes: dict[str, Any] = Field(default_factory=dict)
    args_preview: dict[str, Any] = Field(default_factory=dict)
    result_preview: dict[str, Any] = Field(default_factory=dict)
    file_ops: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    costs: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=ControlRoomOut)
async def get_control_room(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=100, ge=10, le=250),
) -> ControlRoomOut:
    policy = await get_or_create_policy(session, user.id)
    summary = await _summary(user.id, policy.monthly_budget_cents, policy.run_budget_cents, session)
    timeline = await _timeline(user.id, session, limit=limit)
    return ControlRoomOut(
        policy=ControlPolicyOut(**policy_dict(policy)),
        summary=summary,
        timeline=timeline,
    )


@router.patch("/policy", response_model=ControlPolicyOut)
async def update_control_policy(
    body: ControlPolicyIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ControlPolicyOut:
    policy = await get_or_create_policy(session, user.id)
    patch = body.model_dump(exclude_unset=True)
    if "approved_agents" in patch and patch["approved_agents"] is not None:
        patch["approved_agents"] = _normalize_approved_agents(patch["approved_agents"])

    monthly_budget = int(
        (
            patch["monthly_budget_cents"]
            if "monthly_budget_cents" in patch
            else policy.monthly_budget_cents
        )
        or 0
    )
    run_budget = int(
        (
            patch["run_budget_cents"]
            if "run_budget_cents" in patch
            else policy.run_budget_cents
        )
        or 0
    )
    if monthly_budget > 0 and run_budget > monthly_budget:
        raise HTTPException(400, "per-run budget cannot exceed the monthly budget")

    for key, value in patch.items():
        if value is not None:
            setattr(policy, key, value)
    await session.commit()
    await session.refresh(policy)
    return ControlPolicyOut(**policy_dict(policy))


@router.get("/receipts/{source}/{item_id}", response_model=ReceiptOut)
async def get_receipt(
    source: str,
    item_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ReceiptOut:
    if source == "subagent":
        return await _subagent_receipt(item_id, user.id, session)
    if source == "dag":
        return await _dag_receipt(item_id, user.id, session)
    if source == "proof":
        return await _proof_receipt(item_id, user.id, session)
    if source == "trial":
        return await _trial_receipt(item_id, user.id, session)
    if source == "deployment":
        return await _deployment_receipt(item_id, user.id, session)
    if source == "llm":
        return await _llm_receipt(item_id, user.id, session)
    if source == "review_loop":
        return await _review_loop_receipt(item_id, user.id, session)
    if source == "protocol_simulation":
        return await _protocol_simulation_receipt(item_id, user.id, session)
    raise HTTPException(404, "receipt source not found")


async def _summary(
    user_id: int,
    monthly_budget_cents: int,
    run_budget_cents: int,
    session: AsyncSession,
) -> ControlSummaryOut:
    since = month_start()
    monthly_spend_cents = int(round((await monthly_llm_spend_usd(session, user_id)) * 100))
    llm_calls = (
        await session.execute(
            select(func.count(LLMUsageEvent.id)).where(
                LLMUsageEvent.user_id == user_id,
                LLMUsageEvent.created_at >= since,
            )
        )
    ).scalar_one()
    llm_tokens = (
        await session.execute(
            select(func.coalesce(func.sum(LLMUsageEvent.total_tokens), 0)).where(
                LLMUsageEvent.user_id == user_id,
                LLMUsageEvent.created_at >= since,
            )
        )
    ).scalar_one()
    subagents = (
        await session.execute(
            select(SubagentRun).where(
                SubagentRun.user_id == user_id,
                SubagentRun.created_at >= since,
            )
        )
    ).scalars().all()
    dags = (
        await session.execute(
            select(DagRun).where(DagRun.user_id == user_id, DagRun.created_at >= since)
        )
    ).scalars().all()
    failures = sum(1 for row in [*subagents, *dags] if row.status in {"error", "denied", "failed"})
    files_touched = sum(len(row.file_ops or []) for row in subagents)
    return ControlSummaryOut(
        monthly_spend_cents=monthly_spend_cents,
        monthly_budget_cents=monthly_budget_cents,
        run_budget_cents=run_budget_cents,
        llm_calls_month=int(llm_calls or 0),
        llm_tokens_month=int(llm_tokens or 0),
        agent_runs=len(subagents),
        dag_runs=len(dags),
        failures=failures,
        files_touched=files_touched,
    )


async def _timeline(
    user_id: int,
    session: AsyncSession,
    *,
    limit: int,
) -> list[ControlTimelineItemOut]:
    subagents = (
        await session.execute(
            select(SubagentRun)
            .where(SubagentRun.user_id == user_id)
            .order_by(desc(SubagentRun.created_at))
            .limit(limit)
        )
    ).scalars().all()
    dags = (
        await session.execute(
            select(DagRun)
            .where(DagRun.user_id == user_id)
            .order_by(desc(DagRun.created_at))
            .limit(limit)
        )
    ).scalars().all()
    proofs = (
        await session.execute(
            select(AgentProofRun)
            .where(AgentProofRun.user_id == user_id)
            .order_by(desc(AgentProofRun.created_at))
            .limit(limit)
        )
    ).scalars().all()
    trials = (
        await session.execute(
            select(TrialRun, TrialRoom)
            .join(TrialRoom, TrialRun.trial_room_id == TrialRoom.id)
            .where(TrialRun.user_id == user_id)
            .order_by(desc(TrialRun.created_at))
            .limit(limit)
        )
    ).all()
    deployments = (
        await session.execute(
            select(AgentDeployment)
            .where(AgentDeployment.user_id == user_id)
            .order_by(desc(AgentDeployment.created_at))
            .limit(limit)
        )
    ).scalars().all()
    llm = (
        await session.execute(
            select(LLMUsageEvent)
            .where(LLMUsageEvent.user_id == user_id)
            .order_by(desc(LLMUsageEvent.created_at))
            .limit(limit)
        )
    ).scalars().all()
    review_loops = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user_id,
                WorkJob.kind == "adversarial_review_loop",
            )
            .order_by(desc(WorkJob.created_at))
            .limit(limit)
        )
    ).scalars().all()
    protocol_simulations = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user_id,
                WorkJob.kind == PROTOCOL_SIMULATION_KIND,
            )
            .order_by(desc(WorkJob.created_at))
            .limit(limit)
        )
    ).scalars().all()
    protocol_events_by_job: dict[str, list[WorkEvent]] = {row.job_id: [] for row in protocol_simulations}
    protocol_job_ids = [row.job_id for row in protocol_simulations]
    if protocol_job_ids:
        protocol_events = (
            await session.execute(
                select(WorkEvent)
                .where(WorkEvent.job_id.in_(protocol_job_ids))
                .order_by(WorkEvent.id.asc())
            )
        ).scalars().all()
        for event in protocol_events:
            protocol_events_by_job.setdefault(event.job_id, []).append(event)

    out: list[ControlTimelineItemOut] = []
    out.extend(_subagent_item(row) for row in subagents)
    out.extend(_dag_item(row) for row in dags)
    out.extend(_proof_item(row) for row in proofs)
    out.extend(_trial_item(run, room) for run, room in trials)
    out.extend(_deployment_item(row) for row in deployments)
    out.extend(_llm_item(row) for row in llm)
    out.extend(_review_loop_item(row) for row in review_loops)
    out.extend(_protocol_simulation_item(row, protocol_events_by_job.get(row.job_id, [])) for row in protocol_simulations)
    out.sort(key=lambda row: _timeline_sort_key(row.created_at), reverse=True)
    return out[:limit]


def _timeline_sort_key(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _subagent_item(row: SubagentRun) -> ControlTimelineItemOut:
    return ControlTimelineItemOut(
        id=f"subagent:{row.grant_id}",
        source="subagent",
        title=f"{row.agent_name}.{row.skill_name}",
        status=row.status,
        summary=row.summary,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        file_ops=row.file_ops or [],
        receipt_path=f"subagent/{row.grant_id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={"thread_id": row.thread_id, "grant_id": row.grant_id},
    )


def _dag_item(row: DagRun) -> ControlTimelineItemOut:
    return ControlTimelineItemOut(
        id=f"dag:{row.dag_run_id}",
        source="dag",
        title=row.goal or row.dag_run_id,
        status=row.status,
        summary=row.summary,
        receipt_path=f"dag/{row.dag_run_id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={"thread_id": row.thread_id, "nodes": len(row.nodes_json or [])},
    )


def _proof_item(row: AgentProofRun) -> ControlTimelineItemOut:
    return ControlTimelineItemOut(
        id=f"proof:{row.id}",
        source="proof",
        title=f"Proof: {row.agent_name}.{row.skill_name}",
        status=row.status,
        summary=row.summary or row.error,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        file_ops=row.file_ops or [],
        receipt_path=f"proof/{row.id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={"grant_id": row.grant_id, "head_sha": row.head_sha},
    )


def _trial_item(row: TrialRun, room: TrialRoom) -> ControlTimelineItemOut:
    return ControlTimelineItemOut(
        id=f"trial:{row.id}",
        source="trial",
        title=f"Trial: {row.agent_name}.{row.skill_name}",
        status=row.status,
        summary=row.summary or row.error,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        file_ops=row.file_ops or [],
        receipt_path=f"trial/{row.id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={"room": room.slug, "score": row.score},
    )


def _deployment_item(row: AgentDeployment) -> ControlTimelineItemOut:
    return ControlTimelineItemOut(
        id=f"deployment:{row.deploy_id}",
        source="deployment",
        title=f"Deploy: {row.agent_name}",
        status=row.status,
        summary=row.error,
        agent_name=row.agent_name,
        receipt_path=f"deployment/{row.deploy_id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={"trigger": row.trigger, "head_sha": row.head_sha, "image": row.image},
    )


def _llm_item(row: LLMUsageEvent) -> ControlTimelineItemOut:
    return ControlTimelineItemOut(
        id=f"llm:{row.id}",
        source="llm",
        title=f"LLM: {row.model or 'model call'}",
        status=llm_usage_status(row),
        summary=_llm_summary(row),
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        cost_cents=int(round((row.cost_usd or 0.0) * 100)),
        token_count=row.total_tokens,
        receipt_path=f"llm/{row.id}",
        created_at=row.created_at,
        completed_at=row.created_at,
        metadata={"thread_id": row.thread_id, "source": row.source},
    )


def _review_loop_item(row: WorkJob) -> ControlTimelineItemOut:
    metadata = row.metadata_json or {}
    payload = row.input_payload or {}
    target_agent = str(payload.get("target_agent") or row.subject_id or "agent")
    reviewer = str(metadata.get("reviewer_agent_name") or row.worker_name or "agent-reviewer")
    limits = payload.get("loop_limits") if isinstance(payload.get("loop_limits"), dict) else {}
    return ControlTimelineItemOut(
        id=f"review_loop:{row.job_id}",
        source="review_loop",
        title=f"Reviewer loop: {target_agent}",
        status=row.status,
        summary=row.summary or row.error,
        agent_name=target_agent,
        receipt_path=f"review_loop/{row.job_id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={
            "reviewer": reviewer,
            "template_ref": metadata.get("template_ref"),
            "kill_switch_available": metadata.get("kill_switch_available"),
            "active_apply_enabled": metadata.get("active_apply_enabled"),
            "budget_ceiling_cents": metadata.get("budget_ceiling_cents"),
            "ttl_seconds": limits.get("ttl_seconds") or metadata.get("ttl_seconds"),
            "max_iterations": limits.get("max_iterations") or metadata.get("max_iterations"),
        },
    )


def _protocol_simulation_item(row: WorkJob, events: list[WorkEvent] | None = None) -> ControlTimelineItemOut:
    metadata = row.metadata_json or {}
    payload = row.input_payload or {}
    protocol_ref = metadata.get("protocol_ref") if isinstance(metadata.get("protocol_ref"), dict) else {}
    target_agent = str(payload.get("target_agent") or row.subject_id or "agent")
    protocol_name = str(protocol_ref.get("display_name") or protocol_ref.get("id") or row.source_id or "protocol")
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    trace_summary = _scenario_trace_summary_from_events(events or [])
    return ControlTimelineItemOut(
        id=f"protocol_simulation:{row.job_id}",
        source="protocol_simulation",
        title=f"Protocol simulation: {protocol_name}",
        status=row.status,
        summary=row.summary or row.error,
        agent_name=target_agent,
        receipt_path=f"protocol_simulation/{row.job_id}",
        created_at=row.created_at,
        completed_at=row.completed_at,
        metadata={
            "protocol_id": protocol_ref.get("id"),
            "protocol_version": protocol_ref.get("version"),
            "template_ref": metadata.get("template_ref"),
            "simulation_only": metadata.get("simulation_only"),
            "proposal_only": metadata.get("proposal_only"),
            "active_apply_enabled": metadata.get("active_apply_enabled"),
            "kill_switch_available": metadata.get("kill_switch_available"),
            "run_budget_cents": limits.get("run_budget_cents") or metadata.get("run_budget_cents"),
            "ttl_seconds": limits.get("ttl_seconds") or metadata.get("ttl_seconds"),
            "max_episodes": limits.get("max_episodes") or metadata.get("max_episodes"),
            "trace_summary": trace_summary,
        },
    )


async def _subagent_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    row = (
        await session.execute(
            select(SubagentRun).where(
                SubagentRun.grant_id == item_id,
                SubagentRun.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "subagent receipt not found")
    events = (
        await session.execute(
            select(SubagentRunEvent)
            .where(SubagentRunEvent.run_id == row.id)
            .order_by(SubagentRunEvent.id.asc())
        )
    ).scalars().all()
    receipt = {
        "source": "subagent",
        "subject": f"{row.agent_name}.{row.skill_name}",
        "status": row.status,
        "summary": row.summary,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "ids": {"grant_id": row.grant_id, "thread_id": row.thread_id},
        "scopes": row.scopes or {},
        "args_preview": _json_preview(row.args_json),
        "file_ops": row.file_ops or [],
        "events": [_event_out(event.event_type, event.payload, event.created_at) for event in events],
        "costs": await _usage_costs(user_id, session, grant_id=row.grant_id, thread_id=row.thread_id),
        "metadata": {"rerun_of_grant_id": row.rerun_of_grant_id},
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _dag_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    row = (
        await session.execute(
            select(DagRun).where(DagRun.dag_run_id == item_id, DagRun.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "dag receipt not found")
    nodes = (
        await session.execute(
            select(DagRunNode)
            .where(DagRunNode.dag_run_id == item_id, DagRunNode.user_id == user_id)
            .order_by(DagRunNode.id.asc())
        )
    ).scalars().all()
    file_ops = [op for node in nodes for op in (node.file_ops or [])]
    receipt = {
        "source": "dag",
        "subject": row.goal or row.dag_run_id,
        "status": row.status,
        "summary": row.summary,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "ids": {"dag_run_id": row.dag_run_id, "thread_id": row.thread_id},
        "file_ops": file_ops,
        "events": [
            {
                "type": "dag_node",
                "created_at": (node.started_at or row.created_at).isoformat(),
                "payload": {
                    "node_id": node.node_id,
                    "agent": node.agent_name,
                    "skill": node.skill_name,
                    "status": node.status,
                    "summary": node.summary,
                    "grant_id": node.grant_id,
                },
            }
            for node in nodes
        ],
        "costs": await _usage_costs(user_id, session, thread_id=row.thread_id),
        "metadata": {"nodes": row.nodes_json or []},
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _proof_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    proof_id = _parse_int_id(item_id, "proof receipt")
    row = (
        await session.execute(
            select(AgentProofRun).where(
                AgentProofRun.id == proof_id,
                AgentProofRun.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "proof receipt not found")
    receipt = {
        "source": "proof",
        "subject": f"{row.agent_name}.{row.skill_name}",
        "status": row.status,
        "summary": row.summary or row.error,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "ids": {"id": row.id, "grant_id": row.grant_id},
        "args_preview": _json_preview(row.args_json),
        "result_preview": row.result or {},
        "file_ops": row.file_ops or [],
        "events": row.events or [],
        "costs": await _usage_costs(user_id, session, grant_id=row.grant_id),
        "metadata": {
            "card_hash": row.card_hash,
            "repo_url": row.repo_url,
            "head_sha": row.head_sha,
            "image": row.image,
            "agent_url": row.agent_url,
            "elapsed_ms": row.elapsed_ms,
        },
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _trial_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    trial_id = _parse_int_id(item_id, "trial receipt")
    row = (
        await session.execute(
            select(TrialRun, TrialRoom)
            .join(TrialRoom, TrialRun.trial_room_id == TrialRoom.id)
            .where(TrialRun.id == trial_id, TrialRun.user_id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "trial receipt not found")
    run, room = row
    source_receipt = run.receipt_json or {}
    receipt = {
        "source": "trial",
        "subject": f"{run.agent_name}.{run.skill_name}",
        "status": run.status,
        "summary": run.summary or run.error,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "ids": {"id": run.id, "grant_id": run.grant_id, "room": room.slug},
        "args_preview": _json_preview(run.args_json),
        "result_preview": run.result or {},
        "file_ops": run.file_ops or [],
        "events": run.events or [],
        "costs": await _usage_costs(user_id, session, grant_id=run.grant_id),
        "metadata": {
            "score": run.score,
            "evaluator_notes": run.evaluator_notes,
            "room_title": room.title,
            "trial_receipt": source_receipt,
        },
    }
    return ReceiptOut(receipt_id=source_receipt.get("receipt_id") or receipt_id(receipt), **receipt)


async def _deployment_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    row = (
        await session.execute(
            select(AgentDeployment).where(
                AgentDeployment.deploy_id == item_id,
                AgentDeployment.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "deployment receipt not found")
    events = (
        await session.execute(
            select(AgentDeploymentEvent)
            .where(AgentDeploymentEvent.deployment_id == row.id)
            .order_by(AgentDeploymentEvent.id.asc())
        )
    ).scalars().all()
    receipt = {
        "source": "deployment",
        "subject": row.agent_name,
        "status": row.status,
        "summary": row.error,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "ids": {"deploy_id": row.deploy_id, "agent_id": row.agent_id},
        "events": [_event_out(event.stage, {"status": event.status, "message": event.message, **(event.data or {})}, event.created_at) for event in events],
        "metadata": {
            "trigger": row.trigger,
            "source_repo_url": row.source_repo_url,
            "head_sha": row.head_sha,
            "image": row.image,
            "agent_url": row.agent_url,
            "verification": row.verification or {},
        },
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _llm_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    llm_id = _parse_int_id(item_id, "LLM receipt")
    row = (
        await session.execute(
            select(LLMUsageEvent).where(
                LLMUsageEvent.id == llm_id,
                LLMUsageEvent.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "LLM receipt not found")
    receipt = {
        "source": "llm",
        "subject": row.model or "model call",
        "status": llm_usage_status(row),
        "summary": _llm_summary(row),
        "created_at": row.created_at,
        "completed_at": row.created_at,
        "ids": {"id": row.id, "thread_id": row.thread_id, "grant_id": row.grant_id},
        "costs": _cost_dict([row]),
        "metadata": {
            "source": row.source,
            "provider": row.provider,
            "agent_name": row.agent_name,
            "skill_name": row.skill_name,
            **(row.metadata_json or {}),
        },
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _review_loop_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    row = (
        await session.execute(
            select(WorkJob).where(
                WorkJob.job_id == item_id,
                WorkJob.user_id == user_id,
                WorkJob.kind == "adversarial_review_loop",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "review loop receipt not found")
    events = (
        await session.execute(
            select(WorkEvent)
            .where(WorkEvent.job_id == row.job_id)
            .order_by(WorkEvent.id.asc())
        )
    ).scalars().all()
    payload = row.input_payload or {}
    metadata = row.metadata_json or {}
    subject = str(payload.get("target_agent") or row.subject_id or row.job_id)
    receipt = {
        "source": "review_loop",
        "subject": subject,
        "status": row.status,
        "summary": row.summary or row.error,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "ids": {
            "job_id": row.job_id,
            "correlation_id": row.correlation_id,
            "target_agent": subject,
        },
        "args_preview": payload if isinstance(payload, dict) else _json_preview(payload),
        "result_preview": row.output_payload or row.error_payload or {},
        "events": [
            _event_out(
                event.event_type,
                {
                    "status": event.status,
                    "message": event.message,
                    **(event.payload or {}),
                },
                event.created_at,
            )
            for event in events
        ],
        "metadata": {
            "reviewer": row.worker_name,
            "template_ref": metadata.get("template_ref"),
            "kill_switch_available": metadata.get("kill_switch_available"),
            "active_apply_enabled": metadata.get("active_apply_enabled"),
            "source_document": metadata.get("source_document"),
        },
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _protocol_simulation_receipt(item_id: str, user_id: int, session: AsyncSession) -> ReceiptOut:
    row = (
        await session.execute(
            select(WorkJob).where(
                WorkJob.job_id == item_id,
                WorkJob.user_id == user_id,
                WorkJob.kind == PROTOCOL_SIMULATION_KIND,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "protocol simulation receipt not found")
    events = (
        await session.execute(
            select(WorkEvent)
            .where(WorkEvent.job_id == row.job_id)
            .order_by(WorkEvent.id.asc())
        )
    ).scalars().all()
    payload = row.input_payload or {}
    metadata = row.metadata_json or {}
    protocol_ref = metadata.get("protocol_ref") if isinstance(metadata.get("protocol_ref"), dict) else {}
    subject = str(payload.get("target_agent") or row.subject_id or row.job_id)
    receipt = {
        "source": "protocol_simulation",
        "subject": subject,
        "status": row.status,
        "summary": row.summary or row.error,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
        "ids": {
            "job_id": row.job_id,
            "correlation_id": row.correlation_id,
            "target_agent": subject,
            "protocol_id": protocol_ref.get("id"),
            "protocol_version": protocol_ref.get("version"),
        },
        "args_preview": payload if isinstance(payload, dict) else _json_preview(payload),
        "result_preview": row.output_payload or row.error_payload or {},
        "events": [
            _event_out(
                event.event_type,
                {
                    "status": event.status,
                    "message": event.message,
                    **(event.payload or {}),
                },
                event.created_at,
            )
            for event in events
        ],
        "metadata": {
            "protocol_ref": protocol_ref,
            "template_ref": metadata.get("template_ref"),
            "simulation_only": metadata.get("simulation_only"),
            "proposal_only": metadata.get("proposal_only"),
            "active_apply_enabled": metadata.get("active_apply_enabled"),
            "kill_switch_available": metadata.get("kill_switch_available"),
            "source_document": metadata.get("source_document"),
            "run_budget_cents": metadata.get("run_budget_cents"),
            "ttl_seconds": metadata.get("ttl_seconds"),
            "max_episodes": metadata.get("max_episodes"),
            "trace_summary": _scenario_trace_summary_from_events(events),
        },
    }
    return ReceiptOut(receipt_id=receipt_id(receipt), **receipt)


async def _usage_costs(
    user_id: int,
    session: AsyncSession,
    *,
    grant_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    stmt = select(LLMUsageEvent).where(LLMUsageEvent.user_id == user_id)
    filters = []
    if grant_id:
        filters.append(LLMUsageEvent.grant_id == grant_id)
    if thread_id:
        filters.append(LLMUsageEvent.thread_id == thread_id)
    if not filters:
        return _cost_dict([])
    stmt = stmt.where(or_(*filters))
    rows = (await session.execute(stmt)).scalars().all()
    return _cost_dict(rows)


def _cost_dict(rows: list[LLMUsageEvent]) -> dict[str, Any]:
    return {
        "llm_calls": len(rows),
        "prompt_tokens": sum(row.prompt_tokens for row in rows),
        "completion_tokens": sum(row.completion_tokens for row in rows),
        "total_tokens": sum(row.total_tokens for row in rows),
        "cost_usd": round(sum(float(row.cost_usd or 0.0) for row in rows), 6),
    }


def _llm_summary(row: LLMUsageEvent) -> str:
    if llm_usage_status(row) in {"error", "failed"}:
        error = (row.metadata_json or {}).get("error")
        return str(error or "LLM call failed")
    return f"{row.total_tokens} tokens"


def _json_preview(raw: str | None) -> dict[str, Any]:
    import json

    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _normalize_approved_agents(raw: list[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for value in raw:
        name = str(value).strip()
        if not name:
            continue
        if not _AGENT_ALLOWLIST_NAME_RE.match(name):
            raise HTTPException(
                400,
                "approved agent names must be 1-128 characters and contain no whitespace or slashes",
            )
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names[:200]


def _parse_int_id(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(404, f"{label} not found") from exc
    if parsed < 1:
        raise HTTPException(404, f"{label} not found")
    return parsed


def _event_out(kind: str, payload: dict[str, Any], created_at: datetime) -> dict[str, Any]:
    return {
        "type": kind,
        "payload": payload or {},
        "created_at": created_at.isoformat(),
    }


def _scenario_trace_summary_from_events(events: list[WorkEvent]) -> dict[str, Any]:
    scenario_ids: list[str] = []
    alerts: set[str] = set()
    violations: set[str] = set()
    summary = {
        "scenario_count": 0,
        "event_count": 0,
        "invariant_pass_count": 0,
        "invariant_fail_count": 0,
        "replay_pass_count": 0,
        "alert_count": 0,
        "violation_count": 0,
        "passed": False,
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
    for event in events:
        if event.event_type != "scenario_trace_recorded":
            continue
        payload = event.payload or {}
        event_summary = payload.get("trace_summary")
        if not isinstance(event_summary, dict):
            event_summary = summarize_scenario_trace_payload(payload)
        for scenario_id in event_summary.get("scenario_ids", []):
            if str(scenario_id).strip() and str(scenario_id) not in scenario_ids:
                scenario_ids.append(str(scenario_id))
        for alert in event_summary.get("alerts", []):
            if str(alert).strip():
                alerts.add(str(alert))
        for violation in event_summary.get("violations", []):
            if str(violation).strip():
                violations.add(str(violation))
        summary["event_count"] += int(event_summary.get("event_count") or 0)
        summary["invariant_pass_count"] += int(event_summary.get("invariant_pass_count") or 0)
        summary["invariant_fail_count"] += int(event_summary.get("invariant_fail_count") or 0)
        summary["replay_pass_count"] += int(event_summary.get("replay_pass_count") or 0)
    summary["scenario_ids"] = scenario_ids
    summary["scenario_count"] = len(scenario_ids)
    summary["alerts"] = sorted(alerts)
    summary["alert_count"] = len(alerts)
    summary["violations"] = sorted(violations)
    summary["violation_count"] = len(violations)
    summary["passed"] = summary["scenario_count"] > 0 and summary["invariant_fail_count"] == 0
    return summary
