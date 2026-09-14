"""Reusable raw-skill DAG planning and execution primitives.

The DAG engine deliberately knows only about nodes, dependencies, rendered
arguments, limits, and events. Authority, grants, transport, and policy are
provided by the caller through ``call_node`` and ``emit`` adapters.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


MAX_DAG_NODES = 8
MAX_PARALLEL_NODES = 3
_TEMPLATE_RE = re.compile(r"\{\{\s*nodes\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_.\[\]-]+)\s*\}\}")

DagNodeCaller = Callable[["DagNode", dict[str, Any]], Awaitable[Any]]
DagEventEmitter = Callable[[dict[str, Any]], Awaitable[None] | None]
DagGrantResolver = Callable[
    ["DagNode", dict[str, Any]],
    Awaitable[str | None] | str | None,
]


@dataclass(frozen=True)
class DagLimits:
    max_nodes: int = MAX_DAG_NODES
    max_parallel: int = MAX_PARALLEL_NODES

    def __post_init__(self) -> None:
        if self.max_nodes < 1:
            raise ValueError("max_nodes must be at least 1")
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")


@dataclass(frozen=True)
class DagNode:
    id: str
    agent: str
    skill: str
    args: dict[str, Any]
    deps: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()


def parse_dag(
    dag_json: str,
    *,
    max_nodes: int = MAX_DAG_NODES,
) -> tuple[str, list[DagNode]]:
    try:
        raw = json.loads(dag_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"dag_json is invalid JSON: {exc}") from exc
    if isinstance(raw, list):
        goal = ""
        nodes_raw = raw
    elif isinstance(raw, dict):
        goal = str(raw.get("goal") or "")
        nodes_raw = raw.get("nodes")
    else:
        raise ValueError("dag_json must be an object with nodes[] or a nodes array")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ValueError("dag_json.nodes must be a non-empty array")
    if len(nodes_raw) > max_nodes:
        raise ValueError(f"DAG is too large: max {max_nodes} nodes")

    nodes: list[DagNode] = []
    seen: set[str] = set()
    for i, raw_node in enumerate(nodes_raw):
        if not isinstance(raw_node, dict):
            raise ValueError(f"node {i} must be an object")
        node_id = str(raw_node.get("id") or "").strip()
        agent = str(raw_node.get("agent") or raw_node.get("name") or "").strip()
        skill = str(raw_node.get("skill") or "").strip()
        if not node_id:
            raise ValueError(f"node {i} missing id")
        if node_id in seen:
            raise ValueError(f"duplicate node id: {node_id}")
        if not agent or not skill:
            raise ValueError(f"node {node_id} must include agent and skill")
        args = raw_node.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"node {node_id} args must be an object")
        deps_raw = raw_node.get("deps") or raw_node.get("depends_on") or []
        if not isinstance(deps_raw, list):
            raise ValueError(f"node {node_id} deps must be an array")
        deps = tuple(str(dep).strip() for dep in deps_raw if str(dep).strip())
        outputs_raw = raw_node.get("expected_outputs") or []
        expected_outputs = (
            tuple(str(item).strip() for item in outputs_raw if str(item).strip())
            if isinstance(outputs_raw, list)
            else ()
        )
        seen.add(node_id)
        nodes.append(
            DagNode(
                id=node_id,
                agent=agent,
                skill=skill,
                args=args,
                deps=deps,
                expected_outputs=expected_outputs,
            )
        )

    validate_dag_nodes(nodes, max_nodes=max_nodes)
    return goal, nodes


def validate_dag_nodes(
    nodes: Sequence[DagNode],
    *,
    max_nodes: int = MAX_DAG_NODES,
) -> None:
    if not nodes:
        raise ValueError("DAG nodes must be non-empty")
    if len(nodes) > max_nodes:
        raise ValueError(f"DAG is too large: max {max_nodes} nodes")
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise ValueError(f"duplicate node id: {node.id}")
        seen.add(node.id)
    ids = {node.id for node in nodes}
    for node in nodes:
        missing = [dep for dep in node.deps if dep not in ids]
        if missing:
            raise ValueError(f"node {node.id} depends on unknown node(s): {missing}")
    topological_rounds(nodes)


def topological_rounds(nodes: Sequence[DagNode]) -> list[list[DagNode]]:
    by_id = {node.id: node for node in nodes}
    remaining = set(by_id)
    done: set[str] = set()
    rounds: list[list[DagNode]] = []
    while remaining:
        ready = sorted(
            [
                node
                for node_id, node in by_id.items()
                if node_id in remaining and set(node.deps).issubset(done)
            ],
            key=lambda node: node.id,
        )
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ValueError(f"DAG has a dependency cycle involving: {cycle}")
        rounds.append(ready)
        for node in ready:
            remaining.remove(node.id)
            done.add(node.id)
    return rounds


def node_wire(node: DagNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "agent": node.agent,
        "skill": node.skill,
        "deps": list(node.deps),
        "args": node.args,
        "expected_outputs": list(node.expected_outputs),
    }


def summarize_result(result: Any) -> str:
    if isinstance(result, dict):
        if "error" in result:
            return f"error: {str(result['error'])[:140]}"
        for key in ("summary", "message", "chart_path", "output_path", "path", "url"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return f"{key}={value[:160]}"
        return "ok"
    if result is None:
        return "ok"
    return str(result)[:160]


def resolve_path(data: Mapping[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not part:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_-]+)\[(\d+)\]", part)
        if m:
            key, idx_s = m.groups()
            cur = cur.get(key) if isinstance(cur, Mapping) else None
            if not isinstance(cur, list):
                return None
            idx = int(idx_s)
            cur = cur[idx] if 0 <= idx < len(cur) else None
            continue
        if isinstance(cur, Mapping):
            cur = cur.get(part)
        else:
            return None
    return cur


def render_args(value: Any, completed: Mapping[str, Mapping[str, Any]]) -> Any:
    if isinstance(value, str):

        def resolve(match: re.Match[str]) -> Any:
            node_id, path = match.groups()
            node = completed.get(node_id)
            if node is None:
                return None
            return resolve_path(node, path)

        exact = _TEMPLATE_RE.fullmatch(value)
        if exact:
            resolved = resolve(exact)
            return "" if resolved is None else resolved

        def repl(match: re.Match[str]) -> str:
            resolved = resolve(match)
            if resolved is None:
                return ""
            return resolved if isinstance(resolved, str) else json.dumps(resolved)

        return _TEMPLATE_RE.sub(repl, value)
    if isinstance(value, list):
        return [render_args(item, completed) for item in value]
    if isinstance(value, dict):
        return {k: render_args(v, completed) for k, v in value.items()}
    return value


def plan_dag(
    goal: str,
    dag_json: str,
    *,
    limits: DagLimits | None = None,
) -> dict[str, Any]:
    limits = limits or DagLimits()
    try:
        parsed_goal, nodes = parse_dag(dag_json, max_nodes=limits.max_nodes)
        rounds = topological_rounds(nodes)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "goal": goal or parsed_goal,
        "max_nodes": limits.max_nodes,
        "max_parallel": limits.max_parallel,
        "rounds": [[node.id for node in round_nodes] for round_nodes in rounds],
        "nodes": [node_wire(node) for node in nodes],
    }


def context_node_caller(
    ctx: Any,
    *,
    grant_resolver: DagGrantResolver | None = None,
    resolve_agent: bool = True,
    timeout: float | None = None,
) -> DagNodeCaller:
    """Build a DAG ``call_node`` adapter over an existing ``RunContext``.

    This keeps the DAG executor raw-skill based: each node still declares
    ``agent`` and ``skill``. Discovery, grant narrowing, transport, and CP
    forwarding stay on the existing ``ctx.discover`` / ``ctx.call`` path.
    """

    async def call_node(node: DagNode, rendered_args: dict[str, Any]) -> dict[str, Any]:
        target = node.agent
        target_name = node.agent
        if resolve_agent:
            try:
                discovered = await ctx.discover.get_agent(node.agent)
                target = discovered.url or discovered.name
                target_name = discovered.name
            except Exception:  # noqa: BLE001
                target = node.agent
        grant = None
        if grant_resolver is not None:
            resolved = grant_resolver(node, rendered_args)
            grant = await resolved if inspect.isawaitable(resolved) else resolved
        call = await ctx.call(
            target,
            node.skill,
            args=rendered_args,
            grant=grant,
            timeout=timeout,
            target_name=target_name,
        )
        return {
            "ok": True,
            "grant_id": call.grant_id,
            "result": call.result,
            "events": list(call.events),
            "artifacts": list(call.artifacts),
        }

    return call_node


async def execute_dag(
    goal: str,
    dag_json: str,
    call_node: DagNodeCaller,
    *,
    emit: DagEventEmitter | None = None,
    limits: DagLimits | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    limits = limits or DagLimits()
    try:
        parsed_goal, nodes = parse_dag(dag_json, max_nodes=limits.max_nodes)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return await execute_dag_nodes(
        goal or parsed_goal,
        nodes,
        call_node,
        emit=emit,
        limits=limits,
        run_id=run_id,
    )


async def execute_dag_nodes(
    goal: str,
    nodes: Sequence[DagNode],
    call_node: DagNodeCaller,
    *,
    emit: DagEventEmitter | None = None,
    limits: DagLimits | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    limits = limits or DagLimits()
    try:
        validate_dag_nodes(nodes, max_nodes=limits.max_nodes)
        rounds = topological_rounds(nodes)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    dag_run_id = run_id or f"dag-{secrets.token_hex(8)}"
    await _emit(
        emit,
        {
            "type": "dag_started",
            "dag_run_id": dag_run_id,
            "goal": goal,
            "nodes": [node_wire(node) for node in nodes],
        },
    )

    completed: dict[str, dict[str, Any]] = {}
    failed = False
    sem = asyncio.Semaphore(limits.max_parallel)

    async def run_node(node: DagNode) -> dict[str, Any]:
        rendered_args = render_args(copy.deepcopy(node.args), completed)
        ok = False
        parsed: dict[str, Any] = {}
        error: str | None = None
        grant_id: str | None = None
        result: Any = None
        async with sem:
            await _emit(
                emit,
                {
                    "type": "dag_node_started",
                    "dag_run_id": dag_run_id,
                    "node_id": node.id,
                    "agent": node.agent,
                    "skill": node.skill,
                    "deps": list(node.deps),
                    "args_preview": rendered_args,
                },
            )
            started = time.monotonic()
            try:
                parsed = _normalize_call_result(await call_node(node, rendered_args))
                ok = bool(parsed.get("ok"))
                error = (
                    parsed.get("error")
                    if isinstance(parsed.get("error"), str)
                    else None
                )
                grant = parsed.get("grant_id")
                grant_id = grant if isinstance(grant, str) else None
                result = parsed.get("result")
                if error:
                    ok = False
            except Exception as exc:  # noqa: BLE001
                ok = False
                error = f"{type(exc).__name__}: {exc}"
                parsed = {"error": error}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        summary = error or summarize_result(result)
        node_result = {
            "node_id": node.id,
            "agent": node.agent,
            "skill": node.skill,
            "ok": ok,
            "grant_id": grant_id,
            "summary": summary,
            "result": result if isinstance(result, dict) else {"value": result},
            "elapsed_ms": elapsed_ms,
            "file_ops": [],
        }
        await _emit(
            emit,
            {
                "type": "dag_node_complete",
                "dag_run_id": dag_run_id,
                **node_result,
            },
        )
        return node_result

    for round_nodes in rounds:
        if failed:
            break
        results = await asyncio.gather(*(run_node(node) for node in round_nodes))
        for res in results:
            completed[res["node_id"]] = res
            if not res["ok"]:
                failed = True

    skipped: dict[str, dict[str, Any]] = {}
    if failed:
        for node in nodes:
            if node.id in completed:
                continue
            node_result = {
                "node_id": node.id,
                "agent": node.agent,
                "skill": node.skill,
                "ok": False,
                "grant_id": None,
                "summary": "skipped due to failed dependency",
                "result": {"skipped": True},
                "elapsed_ms": 0,
                "file_ops": [],
            }
            skipped[node.id] = node_result
            await _emit(
                emit,
                {
                    "type": "dag_node_skipped",
                    "dag_run_id": dag_run_id,
                    **node_result,
                },
            )

    ok = not failed and len(completed) == len(nodes)
    summary = (
        f"completed {len(completed)}/{len(nodes)} nodes"
        if ok
        else f"failed after {len(completed)}/{len(nodes)} nodes"
    )
    await _emit(
        emit,
        {
            "type": "dag_complete",
            "dag_run_id": dag_run_id,
            "ok": ok,
            "summary": summary,
        },
    )
    return {
        "ok": ok,
        "dag_run_id": dag_run_id,
        "summary": summary,
        "nodes": list(completed.values()) + list(skipped.values()),
    }


def _normalize_call_result(raw: Any) -> dict[str, Any]:
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict):
        return {"result": parsed}
    return parsed


async def _emit(emit: DagEventEmitter | None, event: dict[str, Any]) -> None:
    if emit is None:
        return
    result = emit(event)
    if inspect.isawaitable(result):
        await result


__all__ = [
    "DagEventEmitter",
    "DagGrantResolver",
    "DagLimits",
    "DagNode",
    "DagNodeCaller",
    "MAX_DAG_NODES",
    "MAX_PARALLEL_NODES",
    "context_node_caller",
    "execute_dag",
    "execute_dag_nodes",
    "node_wire",
    "parse_dag",
    "plan_dag",
    "render_args",
    "resolve_path",
    "summarize_result",
    "topological_rounds",
    "validate_dag_nodes",
]
