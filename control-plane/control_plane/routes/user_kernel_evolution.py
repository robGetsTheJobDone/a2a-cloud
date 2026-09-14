"""User-scoped simulation-only kernel evolution endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..kernel_contracts import KernelEvidenceEvent
from ..graph_kernel.evolution import KernelEvolutionError, run_kernel_evolution_experiment
from ..graph_kernel.traces import KernelReplayResult
from ..models import ChatThread, User, WorkJob
from ..thread_events import append_thread_event
from ..work_ledger import append_event, create_job, list_job_events, serialize_event, serialize_job


KERNEL_EVOLUTION_KIND = "kernel_evolution"

router = APIRouter(prefix="/v1/me/kernel-evolution", tags=["kernel-evolution"])


class KernelEvolutionRunIn(BaseModel):
    title: str = Field(default="Kernel evolution experiment", min_length=1, max_length=160)
    base_template_id: str | None = Field(default=None, min_length=1, max_length=128)
    base_spec: dict[str, Any] | None = None
    participants: list[Any] | None = Field(default=None, min_length=2, max_length=12)
    strategy: str | None = Field(default=None, min_length=1, max_length=80)
    simulation_type: str | None = Field(default=None, min_length=1, max_length=80)
    seed: str | None = Field(default=None, min_length=1, max_length=160)
    variant_count: int = Field(default=3, ge=1, le=8)
    max_score_delta: float = Field(default=3, ge=0, le=25)
    thread_id: str | None = Field(default=None, min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def _one_base(self) -> "KernelEvolutionRunIn":
        sources = [bool(self.base_template_id), self.base_spec is not None, self.participants is not None]
        if sum(1 for item in sources if item) != 1:
            raise ValueError("provide exactly one of base_template_id, base_spec, or participants")
        return self


class KernelEvolutionOut(BaseModel):
    job: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)


class KernelEvolutionReplayOut(BaseModel):
    job: dict[str, Any]
    replay_passed: bool
    original_summary: dict[str, Any]
    replay_summary: dict[str, Any]


async def _events(session: AsyncSession, job: WorkJob, *, user_id: int) -> list[dict[str, Any]]:
    rows = await list_job_events(session, job.job_id, user_id=user_id)
    return [serialize_event(row) for row in rows]


async def _out(session: AsyncSession, job: WorkJob, *, user_id: int) -> KernelEvolutionOut:
    return KernelEvolutionOut(job=serialize_job(job), events=await _events(session, job, user_id=user_id))


async def _get_owned_job(session: AsyncSession, user: User, job_id: str) -> WorkJob:
    row = (
        await session.execute(
            select(WorkJob).where(
                WorkJob.job_id == job_id,
                WorkJob.user_id == user.id,
                WorkJob.kind == KERNEL_EVOLUTION_KIND,
                WorkJob.subject_type == "user",
                WorkJob.subject_id == str(user.id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "kernel evolution experiment not found")
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


def _experiment_payload(body: KernelEvolutionRunIn) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    payload.pop("idempotency_key", None)
    return payload


async def _project_thread_events(
    session: AsyncSession,
    *,
    user: User,
    thread: ChatThread | None,
    result_payload: dict[str, Any],
) -> None:
    if thread is None:
        return
    experiment_id = str(result_payload.get("experiment_id") or "")
    source = {"kind": "kernel_evolution", "user_id": user.id, "experiment_id": experiment_id}
    await append_thread_event(
        session,
        thread_id=thread.id,
        user_id=user.id,
        event=KernelEvidenceEvent(
            evidence_kind="kernel_evolution",
            event_type="evolution_experiment_recorded",
            title="Kernel evolution experiment recorded",
            status="complete" if result_payload.get("passed") else "failed",
            severity="info" if result_payload.get("passed") else "warning",
            message="simulation-only kernel evolution experiment recorded",
            source=source,
            payload={
                "experiment_id": result_payload.get("experiment_id"),
                "base_ref": result_payload.get("base_ref"),
                "seed": result_payload.get("seed"),
                "variant_count": result_payload.get("variant_count"),
                "winner_variant_id": result_payload.get("winner_variant_id"),
                "passed": result_payload.get("passed"),
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
        ).to_payload(),
    )
    proposal = result_payload.get("proposal") if isinstance(result_payload.get("proposal"), dict) else None
    if proposal is not None:
        await append_thread_event(
            session,
            thread_id=thread.id,
            user_id=user.id,
            event=KernelEvidenceEvent(
                evidence_kind="kernel_evolution_proposal",
                event_type="evolution_proposal_recorded",
                title="Kernel evolution disabled proposal recorded",
                status="disabled",
                severity="info",
                message="winning variant emitted a disabled proposal draft",
                source=source,
                payload=proposal,
            ).to_payload(),
        )


async def _project_replay_thread_event(
    session: AsyncSession,
    *,
    user: User,
    job: WorkJob,
    replay_result: KernelReplayResult,
) -> None:
    payload = job.input_payload if isinstance(job.input_payload, dict) else {}
    thread_id = payload.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return
    thread = await _assert_thread(session, user, thread_id)
    await append_thread_event(
        session,
        thread_id=thread.id,
        user_id=user.id,
        event=KernelEvidenceEvent(
            evidence_kind="kernel_evolution_replay",
            event_type="evolution_replayed",
            title="Kernel evolution replay recorded",
            status="complete" if replay_result.replay_passed else "mismatch",
            severity="info" if replay_result.replay_passed else "warning",
            message="kernel evolution deterministic replay completed",
            source={
                "kind": "kernel_evolution",
                "user_id": user.id,
                "job_id": job.job_id,
            },
            payload=replay_result.to_payload(),
        ).to_payload(),
    )


def _replay_summary(payload: dict[str, Any]) -> dict[str, Any]:
    variants = payload.get("variants") if isinstance(payload.get("variants"), list) else []
    proposal = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else {}
    return {
        "experiment_id": payload.get("experiment_id"),
        "winner_variant_id": payload.get("winner_variant_id"),
        "variant_count": payload.get("variant_count"),
        "variant_digests": [
            {
                "variant_id": variant.get("variant_id"),
                "score": variant.get("score"),
                "passed": variant.get("passed"),
                "replay_digest": variant.get("replay_digest"),
                "proposal_ref": variant.get("proposal_ref"),
            }
            for variant in variants
            if isinstance(variant, dict)
        ],
        "proposal_digest": proposal.get("proposal_digest"),
        "active_apply_enabled": False,
    }


@router.post("/runs", response_model=KernelEvolutionOut, status_code=201)
async def run_user_kernel_evolution(
    body: KernelEvolutionRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KernelEvolutionOut:
    thread = await _assert_thread(session, user, body.thread_id)
    experiment_payload = _experiment_payload(body)
    try:
        result = run_kernel_evolution_experiment(experiment_payload)
    except KernelEvolutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    result_payload = result.to_payload()
    job_id = f"kevo-{uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    job = await create_job(
        session,
        user_id=user.id,
        kind=KERNEL_EVOLUTION_KIND,
        payload={
            "experiment": experiment_payload,
            "thread_id": thread.id if thread else None,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        title=f"Kernel evolution: {result.title}",
        metadata={
            "base_ref": result.base_ref,
            "seed": result.seed,
            "variant_count": len(result.variants),
            "winner_variant_id": result.winner_variant_id,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        job_id=job_id,
        status="complete" if result.passed else "failed",
        queue="kernel-evolution",
        priority=10,
        correlation_id=job_id,
        source_type="kernel_evolution",
        source_id=result.experiment_id,
        subject_type="user",
        subject_id=str(user.id),
        worker_type="simulator",
        worker_name="kernel_evolution@v1",
        idempotency_key=body.idempotency_key,
        idempotency_scope="kernel_evolution",
        record_event=False,
        commit=False,
    )
    if job.job_id != job_id:
        return await _out(session, job, user_id=user.id)

    job.started_at = now
    job.completed_at = now
    job.summary = "kernel evolution proposal generated" if result.passed else "kernel evolution produced no valid proposal"
    job.output_payload = result_payload
    if not result.passed:
        job.error = "kernel evolution variants failed"
        job.error_payload = {"active_apply_enabled": False, "variant_count": len(result.variants)}

    await append_event(
        session,
        job,
        event_type="evolution_experiment_created",
        payload={
            "experiment_id": result.experiment_id,
            "base_ref": result.base_ref,
            "seed": result.seed,
            "variant_count": len(result.variants),
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        },
        message=f"kernel evolution experiment created: {result.title}",
        status=job.status,
        stage="created",
        user_id=user.id,
        actor_type="user",
        actor_id=str(user.id),
        source_type="kernel_evolution",
        source_id=result.experiment_id,
        commit=False,
    )
    for variant in result.variants:
        await append_event(
            session,
            job,
            event_type="evolution_variant_recorded",
            payload=variant.to_payload(),
            message=f"kernel evolution variant recorded: {variant.variant_id}",
            status=job.status,
            stage="variant",
            severity="info" if variant.passed else "warning",
            user_id=user.id,
            actor_type="simulator",
            actor_id="kernel_evolution@v1",
            source_type="kernel_evolution",
            source_id=result.experiment_id,
            commit=False,
        )
    if result.proposal is not None:
        await append_event(
            session,
            job,
            event_type="evolution_proposal_recorded",
            payload=result.proposal,
            message="kernel evolution disabled proposal recorded",
            status=job.status,
            stage="proposal",
            user_id=user.id,
            actor_type="simulator",
            actor_id="kernel_evolution@v1",
            source_type="kernel_evolution",
            source_id=result.experiment_id,
            commit=False,
        )

    await session.commit()
    await session.refresh(job)
    await _project_thread_events(session, user=user, thread=thread, result_payload=result_payload)
    await session.refresh(job)
    return await _out(session, job, user_id=user.id)


@router.get("/runs", response_model=list[KernelEvolutionOut])
async def list_user_kernel_evolution_runs(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[KernelEvolutionOut]:
    rows = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == user.id,
                WorkJob.kind == KERNEL_EVOLUTION_KIND,
                WorkJob.subject_type == "user",
                WorkJob.subject_id == str(user.id),
            )
            .order_by(desc(WorkJob.created_at))
            .limit(limit)
        )
    ).scalars().all()
    return [await _out(session, row, user_id=user.id) for row in rows]


@router.get("/runs/{job_id}", response_model=KernelEvolutionOut)
async def get_user_kernel_evolution_run(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KernelEvolutionOut:
    return await _out(session, await _get_owned_job(session, user, job_id), user_id=user.id)


@router.post("/runs/{job_id}/replay", response_model=KernelEvolutionReplayOut)
async def replay_user_kernel_evolution_run(
    job_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> KernelEvolutionReplayOut:
    job = await _get_owned_job(session, user, job_id)
    payload = job.input_payload if isinstance(job.input_payload, dict) else {}
    experiment = payload.get("experiment") if isinstance(payload.get("experiment"), dict) else None
    if experiment is None:
        raise HTTPException(409, "kernel evolution experiment spec is missing")
    try:
        result = run_kernel_evolution_experiment(experiment)
    except KernelEvolutionError as exc:
        raise HTTPException(409, f"kernel evolution replay failed: {exc}") from exc
    original = job.output_payload if isinstance(job.output_payload, dict) else {}
    original_summary = _replay_summary(original)
    replay_summary = _replay_summary(result.to_payload())
    replay_result = KernelReplayResult(
        replay_passed=original_summary == replay_summary,
        original_summary=original_summary,
        replay_summary=replay_summary,
    )
    await append_event(
        session,
        job,
        event_type="evolution_replayed",
        payload=replay_result.to_payload(),
        message="kernel evolution experiment replayed",
        status=job.status,
        stage="replay",
        severity="info" if replay_result.replay_passed else "warning",
        user_id=user.id,
        actor_type="simulator",
        actor_id="kernel_evolution_replay",
        source_type="kernel_evolution",
        source_id=str((job.metadata_json or {}).get("base_ref") or ""),
        commit=True,
    )
    await session.refresh(job)
    await _project_replay_thread_event(session, user=user, job=job, replay_result=replay_result)
    await session.refresh(job)
    return KernelEvolutionReplayOut(
        job=serialize_job(job),
        replay_passed=replay_result.replay_passed,
        original_summary=replay_result.original_summary,
        replay_summary=replay_result.replay_summary,
    )
