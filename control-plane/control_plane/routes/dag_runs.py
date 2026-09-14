from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import DagRun, DagRunNode, User

router = APIRouter(prefix="/v1/me/dag-runs", tags=["dag-runs"])


class DagRunNodeOut(BaseModel):
    node_id: str
    agent_name: str
    skill_name: str
    deps: list[str]
    args_json: str
    status: str
    summary: str | None
    error: str | None
    result: dict[str, Any]
    grant_id: str | None
    file_ops: list[dict[str, Any]]
    elapsed_ms: int | None
    started_at: datetime | None
    completed_at: datetime | None


class DagRunOut(BaseModel):
    dag_run_id: str
    thread_id: str | None
    goal: str
    status: str
    summary: str | None
    error: str | None
    nodes_json: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    nodes: list[DagRunNodeOut] = Field(default_factory=list)


def _text_from_result(result: dict[str, Any]) -> str | None:
    for key in ("error", "message", "summary", "reason"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _node_error(node: DagRunNode) -> str | None:
    if node.status != "error":
        return None
    result = node.result if isinstance(node.result, dict) else {}
    return _text_from_result(result) or node.summary or "node failed"


def _run_error(
    run: DagRun,
    nodes: list[DagRunNode] | None = None,
) -> str | None:
    if run.status != "error":
        return None
    failed = [node for node in (nodes or []) if node.status == "error"]
    if len(failed) == 1:
        err = _node_error(failed[0])
        if err:
            return f"{failed[0].node_id}: {err}"
    if failed:
        return run.summary or f"{len(failed)} DAG nodes failed"
    return run.summary or "DAG failed"


def _node_out(node: DagRunNode) -> DagRunNodeOut:
    return DagRunNodeOut(
        node_id=node.node_id,
        agent_name=node.agent_name,
        skill_name=node.skill_name,
        deps=node.deps or [],
        args_json=node.args_json or "{}",
        status=node.status,
        summary=node.summary,
        error=_node_error(node),
        result=node.result or {},
        grant_id=node.grant_id,
        file_ops=node.file_ops or [],
        elapsed_ms=node.elapsed_ms,
        started_at=node.started_at,
        completed_at=node.completed_at,
    )


def _run_out(run: DagRun, nodes: list[DagRunNode] | None = None) -> DagRunOut:
    return DagRunOut(
        dag_run_id=run.dag_run_id,
        thread_id=run.thread_id,
        goal=run.goal,
        status=run.status,
        summary=run.summary,
        error=_run_error(run, nodes),
        nodes_json=run.nodes_json or [],
        created_at=run.created_at,
        updated_at=run.updated_at,
        completed_at=run.completed_at,
        nodes=[_node_out(node) for node in (nodes or [])],
    )


@router.get("", response_model=list[DagRunOut])
async def list_dag_runs(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    thread_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[DagRunOut]:
    stmt = (
        select(DagRun)
        .where(DagRun.user_id == user.id)
        .order_by(desc(DagRun.id))
        .limit(limit)
    )
    if thread_id:
        stmt = stmt.where(DagRun.thread_id == thread_id)
    runs = (await session.execute(stmt)).scalars().all()
    if not runs:
        return []

    run_ids = [run.dag_run_id for run in runs]
    nodes = (
        await session.execute(
            select(DagRunNode)
            .where(
                DagRunNode.user_id == user.id,
                DagRunNode.dag_run_id.in_(run_ids),
            )
            .order_by(DagRunNode.id.asc())
        )
    ).scalars().all()
    by_run: dict[str, list[DagRunNode]] = {run_id: [] for run_id in run_ids}
    for node in nodes:
        by_run.setdefault(node.dag_run_id, []).append(node)
    return [_run_out(run, by_run.get(run.dag_run_id, [])) for run in runs]


@router.get("/{dag_run_id}", response_model=DagRunOut)
async def get_dag_run(
    dag_run_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DagRunOut:
    run = (
        await session.execute(
            select(DagRun).where(
                DagRun.dag_run_id == dag_run_id,
                DagRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "dag run not found")
    nodes = (
        await session.execute(
            select(DagRunNode)
            .where(
                DagRunNode.dag_run_id == dag_run_id,
                DagRunNode.user_id == user.id,
            )
            .order_by(DagRunNode.id.asc())
        )
    ).scalars().all()
    return _run_out(run, nodes)
