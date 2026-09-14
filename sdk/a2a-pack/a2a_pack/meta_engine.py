"""Meta-agent DAG execution loop with bounded replanning."""
from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from .context import AgentEvent
from .dag import (
    DagEventEmitter,
    DagGrantResolver,
    DagLimits,
    context_node_caller,
    execute_dag,
    plan_dag,
)

MetaDagPlanner = Callable[
    [dict[str, Any]],
    Awaitable[str | dict[str, Any]] | str | dict[str, Any],
]


async def run_meta_agent_goal(
    ctx: Any,
    *,
    goal: str,
    planner: MetaDagPlanner,
    success_criteria: list[str] | None = None,
    run_id: str | None = None,
    agent_name: str | None = None,
    max_replans: int = 1,
    limits: DagLimits | None = None,
    grant_resolver: DagGrantResolver | None = None,
    emit: DagEventEmitter | None = None,
    call_node: Any | None = None,
) -> dict[str, Any]:
    """Plan/execute/replan a meta-agent goal using the SDK DAG executor."""

    limits = limits or DagLimits()
    criteria = list(success_criteria or [])
    attempt = 0
    history: list[dict[str, Any]] = []
    meta_run = None
    meta_runs = _meta_runs_or_none(ctx, agent_name=agent_name)

    while attempt <= max_replans:
        planner_state = {
            "goal": goal,
            "success_criteria": criteria,
            "attempt": attempt,
            "max_replans": max_replans,
            "history": history,
            "previous_result": history[-1] if history else None,
        }
        try:
            dag_json = await _call_planner(planner, planner_state)
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "status": "error",
                "summary": f"planner failed: {type(exc).__name__}: {exc}",
                "attempts": attempt + 1,
                "history": history,
            }
            await ctx.emit_event(
                AgentEvent(
                    kind="meta_plan_error",
                    payload={"attempt": attempt, "error": result["summary"]},
                )
            )
            await _persist_final(meta_runs, meta_run, result)
            return result
        planned = plan_dag(goal, dag_json, limits=limits)
        if not planned.get("ok"):
            result = {
                "ok": False,
                "status": "error",
                "summary": str(planned.get("error") or "planner emitted invalid DAG"),
                "attempts": attempt + 1,
                "history": history,
                "plan": planned,
            }
            await _persist_final(meta_runs, meta_run, result)
            return result

        if meta_runs is not None and meta_run is None:
            try:
                meta_run = await meta_runs.create(
                    run_id=run_id,
                    goal=goal,
                    success_criteria=criteria,
                    status="running",
                    current_plan=planned,
                    progress=[],
                    state={"attempt": attempt},
                )
            except Exception:  # noqa: BLE001
                meta_run = None
        elif meta_runs is not None and meta_run is not None:
            await _persist_update(
                meta_runs,
                meta_run.run_id,
                status="running",
                current_plan=planned,
                state={"attempt": attempt},
            )

        await ctx.emit_event(
            AgentEvent(
                kind="meta_plan",
                payload={"attempt": attempt, "plan": planned},
            )
        )
        node_caller = call_node or context_node_caller(
            ctx,
            grant_resolver=grant_resolver,
        )
        result = await execute_dag(
            goal,
            dag_json,
            node_caller,
            emit=_combine_emitters(ctx, emit, meta_runs, meta_run),
            limits=limits,
            run_id=run_id,
        )
        history.append({"attempt": attempt, "plan": planned, "result": result})
        if result.get("ok"):
            final = {
                "ok": True,
                "status": "complete",
                "summary": result.get("summary") or "goal complete",
                "attempts": attempt + 1,
                "history": history,
                "result": result,
            }
            await _persist_final(meta_runs, meta_run, final)
            return final
        attempt += 1
        if attempt <= max_replans:
            await ctx.emit_event(
                AgentEvent(
                    kind="meta_replan",
                    payload={"attempt": attempt, "previous_result": result},
                )
            )

    final = {
        "ok": False,
        "status": "blocked",
        "summary": f"goal blocked after {len(history)} attempt(s)",
        "attempts": len(history),
        "history": history,
        "result": history[-1]["result"] if history else {},
    }
    await _persist_final(meta_runs, meta_run, final)
    return final


async def _call_planner(planner: MetaDagPlanner, state: dict[str, Any]) -> str:
    planned = planner(state)
    planned = await planned if inspect.isawaitable(planned) else planned
    if isinstance(planned, str):
        return planned
    if isinstance(planned, dict):
        return json.dumps(planned)
    raise TypeError("meta-agent planner must return a DAG JSON string or dict")


def _combine_emitters(
    ctx: Any,
    emit: DagEventEmitter | None,
    meta_runs: Any,
    meta_run: Any,
) -> DagEventEmitter:
    async def combined(event: dict[str, Any]) -> None:
        await ctx.emit_event(AgentEvent(kind="dag", payload=event))
        if emit is not None:
            result = emit(event)
            if inspect.isawaitable(result):
                await result
        if meta_runs is not None and meta_run is not None:
            await _persist_update(
                meta_runs,
                meta_run.run_id,
                progress=[event],
                state={"last_event_type": event.get("type")},
            )

    return combined


def _meta_runs_or_none(ctx: Any, *, agent_name: str | None) -> Any | None:
    try:
        if agent_name is None:
            return ctx.meta_runs
        from .meta_runs import MetaAgentRunsClient

        return MetaAgentRunsClient(ctx, agent_name=agent_name)
    except Exception:  # noqa: BLE001
        return None


async def _persist_update(meta_runs: Any, run_id: str, **fields: Any) -> None:
    try:
        await meta_runs.update(run_id, **fields)
    except Exception:  # noqa: BLE001
        return


async def _persist_final(meta_runs: Any, meta_run: Any, result: dict[str, Any]) -> None:
    if meta_runs is None or meta_run is None:
        return
    await _persist_update(
        meta_runs,
        meta_run.run_id,
        status=str(
            result.get("status") or ("complete" if result.get("ok") else "blocked")
        ),
        summary=str(result.get("summary") or ""),
        state={"attempts": result.get("attempts"), "ok": bool(result.get("ok"))},
    )


__all__ = ["MetaDagPlanner", "run_meta_agent_goal"]
