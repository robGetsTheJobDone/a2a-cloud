from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DagRun, DagRunNode, SubagentRun


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dag_run_id(event: dict[str, Any]) -> str | None:
    run_id = event.get("dag_run_id")
    return run_id if isinstance(run_id, str) and run_id else None


class DagRunRecorder:
    def __init__(
        self,
        *,
        session: AsyncSession,
        user_id: int,
        thread_id: str | None,
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.thread_id = thread_id

    async def record(self, event: dict[str, Any]) -> None:
        run_id = _dag_run_id(event)
        if run_id is None:
            return
        etype = str(event.get("type") or "")
        if etype == "dag_started":
            await self._record_started(run_id, event)
        elif etype == "dag_node_started":
            await self._record_node_started(run_id, event)
        elif etype == "dag_node_complete":
            await self._record_node_complete(run_id, event)
        elif etype == "dag_node_skipped":
            await self._record_node_skipped(run_id, event)
        elif etype == "dag_complete":
            await self._record_complete(run_id, event)
        else:
            return
        await self.session.commit()

    async def _run(self, run_id: str) -> DagRun | None:
        return (
            await self.session.execute(
                select(DagRun).where(
                    DagRun.dag_run_id == run_id,
                    DagRun.user_id == self.user_id,
                )
            )
        ).scalar_one_or_none()

    async def _node(self, run_id: str, node_id: str) -> DagRunNode | None:
        return (
            await self.session.execute(
                select(DagRunNode).where(
                    DagRunNode.dag_run_id == run_id,
                    DagRunNode.node_id == node_id,
                    DagRunNode.user_id == self.user_id,
                )
            )
        ).scalar_one_or_none()

    async def _record_started(self, run_id: str, event: dict[str, Any]) -> None:
        run = await self._run(run_id)
        nodes = event.get("nodes") if isinstance(event.get("nodes"), list) else []
        if run is None:
            run = DagRun(
                dag_run_id=run_id,
                user_id=self.user_id,
                thread_id=self.thread_id,
                goal=str(event.get("goal") or ""),
                status="running",
                nodes_json=nodes,
            )
            self.session.add(run)
        else:
            run.thread_id = run.thread_id or self.thread_id
            run.goal = str(event.get("goal") or run.goal or "")
            run.status = "running"
            run.nodes_json = nodes or run.nodes_json
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("id") or "")
            if not node_id:
                continue
            node = await self._node(run_id, node_id)
            if node is None:
                node = DagRunNode(
                    dag_run_id=run_id,
                    node_id=node_id,
                    user_id=self.user_id,
                    agent_name=str(raw.get("agent") or raw.get("name") or ""),
                    skill_name=str(raw.get("skill") or ""),
                    deps=list(raw.get("deps") or []),
                    args_json=json.dumps(raw.get("args") or {}, ensure_ascii=False),
                    status="pending",
                )
                self.session.add(node)
        run.updated_at = _utcnow()

    async def _record_node_started(self, run_id: str, event: dict[str, Any]) -> None:
        node_id = str(event.get("node_id") or "")
        if not node_id:
            return
        node = await self._node(run_id, node_id)
        if node is None:
            node = DagRunNode(
                dag_run_id=run_id,
                node_id=node_id,
                user_id=self.user_id,
                agent_name=str(event.get("agent") or ""),
                skill_name=str(event.get("skill") or ""),
                deps=list(event.get("deps") or []),
                args_json=json.dumps(event.get("args_preview") or {}, ensure_ascii=False),
            )
            self.session.add(node)
        node.status = "running"
        node.agent_name = str(event.get("agent") or node.agent_name)
        node.skill_name = str(event.get("skill") or node.skill_name)
        node.deps = list(event.get("deps") or node.deps or [])
        node.args_json = json.dumps(event.get("args_preview") or {}, ensure_ascii=False)
        node.started_at = _utcnow()
        run = await self._run(run_id)
        if run is not None:
            run.updated_at = _utcnow()

    async def _record_node_complete(self, run_id: str, event: dict[str, Any]) -> None:
        node_id = str(event.get("node_id") or "")
        if not node_id:
            return
        node = await self._node(run_id, node_id)
        if node is None:
            node = DagRunNode(
                dag_run_id=run_id,
                node_id=node_id,
                user_id=self.user_id,
                agent_name=str(event.get("agent") or ""),
                skill_name=str(event.get("skill") or ""),
            )
            self.session.add(node)
        ok = bool(event.get("ok"))
        grant_id = event.get("grant_id")
        node.status = "complete" if ok else "error"
        node.summary = str(event.get("summary") or "")
        node.result = event.get("result") if isinstance(event.get("result"), dict) else {}
        node.grant_id = grant_id if isinstance(grant_id, str) and grant_id else None
        node.elapsed_ms = int(event.get("elapsed_ms") or 0)
        node.completed_at = _utcnow()
        node.agent_name = str(event.get("agent") or node.agent_name)
        node.skill_name = str(event.get("skill") or node.skill_name)
        if node.grant_id:
            sub = (
                await self.session.execute(
                    select(SubagentRun).where(
                        SubagentRun.grant_id == node.grant_id,
                        SubagentRun.user_id == self.user_id,
                    )
                )
            ).scalar_one_or_none()
            if sub is not None:
                node.file_ops = sub.file_ops or []
                event["file_ops"] = node.file_ops
        else:
            node.file_ops = (
                event.get("file_ops") if isinstance(event.get("file_ops"), list) else []
            )
        run = await self._run(run_id)
        if run is not None:
            run.updated_at = _utcnow()

    async def _record_node_skipped(self, run_id: str, event: dict[str, Any]) -> None:
        node_id = str(event.get("node_id") or "")
        if not node_id:
            return
        node = await self._node(run_id, node_id)
        if node is None:
            node = DagRunNode(
                dag_run_id=run_id,
                node_id=node_id,
                user_id=self.user_id,
                agent_name=str(event.get("agent") or ""),
                skill_name=str(event.get("skill") or ""),
            )
            self.session.add(node)
        node.agent_name = str(event.get("agent") or node.agent_name)
        node.skill_name = str(event.get("skill") or node.skill_name)
        node.status = "skipped"
        node.summary = str(event.get("summary") or "skipped")
        node.result = event.get("result") if isinstance(event.get("result"), dict) else {}
        node.elapsed_ms = int(event.get("elapsed_ms") or 0)
        node.completed_at = _utcnow()
        run = await self._run(run_id)
        if run is not None:
            run.updated_at = _utcnow()

    async def _record_complete(self, run_id: str, event: dict[str, Any]) -> None:
        run = await self._run(run_id)
        if run is None:
            run = DagRun(
                dag_run_id=run_id,
                user_id=self.user_id,
                thread_id=self.thread_id,
            )
            self.session.add(run)
        ok = bool(event.get("ok"))
        run.status = "complete" if ok else "error"
        run.summary = str(event.get("summary") or "")
        run.completed_at = _utcnow()
        run.updated_at = _utcnow()
