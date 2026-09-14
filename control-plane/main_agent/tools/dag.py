"""Deterministic multi-agent DAG orchestration for the main agent."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from a2a_pack.dag import (
    MAX_DAG_NODES,
    MAX_PARALLEL_NODES,
    DagLimits,
    DagNode,
    execute_dag,
    node_wire as _node_wire,
    parse_dag as _parse_dag,
    render_args as _render_args,
    resolve_path as _resolve_path,
    summarize_result as _summarize_result,
    topological_rounds as _topological_rounds,
    plan_dag,
)
from langchain_core.tools import tool

from .handoff import build_handoff_tools

if TYPE_CHECKING:
    from ..orchestrator import OrchestratorContext


def build_dag_tools(ctx: "OrchestratorContext") -> list[Any]:
    hooks = ctx.hooks
    call_agent = next(
        (t for t in build_handoff_tools(ctx) if getattr(t, "name", "") == "call_agent"),
        None,
    )
    limits = DagLimits(max_nodes=MAX_DAG_NODES, max_parallel=MAX_PARALLEL_NODES)

    async def emit(event: dict[str, Any]) -> None:
        if hooks and hooks.emit is not None:
            try:
                await hooks.emit(event)
            except Exception:  # noqa: BLE001
                pass

    @tool
    async def plan_agent_dag(goal: str, dag_json: str) -> str:
        """Validate a proposed multi-agent DAG before executing it.

        Args:
            goal: User-facing objective for the workflow.
            dag_json: JSON string with ``nodes``. Each node must include:
                ``id``, ``agent``, ``skill``, ``args`` object, and optional
                ``deps`` array. Use ``{{nodes.<id>.summary}}``,
                ``{{nodes.<id>.grant_id}}``, ``{{nodes.<id>.result.key}}``, or
                ``{{nodes.<id>.file_ops[0].path}}`` placeholders in downstream
                args when a later skill needs an earlier output.

        Returns JSON containing the normalized DAG or an error.
        """
        return json.dumps(plan_dag(goal, dag_json, limits=limits))

    @tool
    async def execute_agent_dag(goal: str, dag_json: str) -> str:
        """Execute a validated multi-agent DAG with deterministic scheduling.

        The LLM proposes the graph; this tool validates it, runs ready nodes
        concurrently, invokes each specialist through ``call_agent`` with a
        scoped grant, and emits DAG progress events for the dashboard.

        Args:
            goal: User-facing objective for the workflow.
            dag_json: JSON string with nodes as described by ``plan_agent_dag``.

        Returns JSON with per-node results, grant ids, and final status.
        """
        if call_agent is None:
            return json.dumps({"ok": False, "error": "call_agent unavailable"})

        async def call_node(node: DagNode, rendered_args: dict[str, Any]) -> Any:
            return await call_agent.ainvoke(
                {
                    "name": node.agent,
                    "skill": node.skill,
                    "args_json": json.dumps(
                        rendered_args,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                }
            )

        result = await execute_dag(
            goal,
            dag_json,
            call_node,
            emit=emit,
            limits=limits,
        )
        return json.dumps(result)

    return [plan_agent_dag, execute_agent_dag]


__all__ = [
    "DagNode",
    "MAX_DAG_NODES",
    "MAX_PARALLEL_NODES",
    "_node_wire",
    "_parse_dag",
    "_render_args",
    "_resolve_path",
    "_summarize_result",
    "_topological_rounds",
    "build_dag_tools",
]
