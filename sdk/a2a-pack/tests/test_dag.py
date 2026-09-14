from __future__ import annotations

import json

import pytest

from a2a_pack.dag import (
    DagLimits,
    DagNode,
    context_node_caller,
    execute_dag,
    execute_dag_nodes,
    parse_dag,
    plan_dag,
    render_args,
    topological_rounds,
)
from a2a_pack.a2a_client import CallResult


def test_parse_plan_and_topological_rounds_use_raw_agent_skills() -> None:
    dag = {
        "goal": "ship a report",
        "nodes": [
            {
                "id": "research",
                "agent": "researcher",
                "skill": "search",
                "args": {"topic": "agents"},
            },
            {
                "id": "chart",
                "agent": "analyst",
                "skill": "chart",
                "deps": ["research"],
                "args": {"source": "{{nodes.research.summary}}"},
            },
        ],
    }

    goal, nodes = parse_dag(json.dumps(dag))
    assert goal == "ship a report"
    assert nodes == [
        DagNode(
            id="research",
            agent="researcher",
            skill="search",
            args={"topic": "agents"},
        ),
        DagNode(
            id="chart",
            agent="analyst",
            skill="chart",
            args={"source": "{{nodes.research.summary}}"},
            deps=("research",),
        ),
    ]
    assert [[node.id for node in round_nodes] for round_nodes in topological_rounds(nodes)] == [
        ["research"],
        ["chart"],
    ]

    planned = plan_dag("override goal", json.dumps(dag), limits=DagLimits(max_nodes=4, max_parallel=2))
    assert planned["ok"] is True
    assert planned["goal"] == "override goal"
    assert planned["max_nodes"] == 4
    assert planned["max_parallel"] == 2
    assert planned["rounds"] == [["research"], ["chart"]]


def test_parse_dag_honors_configurable_limits() -> None:
    dag = {
        "nodes": [
            {"id": "a", "agent": "agent-a", "skill": "run", "args": {}},
            {"id": "b", "agent": "agent-b", "skill": "run", "args": {}},
        ]
    }

    with pytest.raises(ValueError, match="max 1 nodes"):
        parse_dag(json.dumps(dag), max_nodes=1)


def test_render_args_preserves_exact_objects_and_embeds_json() -> None:
    completed = {
        "research": {
            "summary": "found demand",
            "grant_id": "grant-123",
            "result": {"score": 91},
            "file_ops": [{"path": "outputs/report.csv"}],
        }
    }

    assert render_args(
        {
            "grant": "{{nodes.research.grant_id}}",
            "result": "{{nodes.research.result}}",
            "inline": "score {{nodes.research.result.score}}",
            "file": "{{nodes.research.file_ops[0].path}}",
        },
        completed,
    ) == {
        "grant": "grant-123",
        "result": {"score": 91},
        "inline": "score 91",
        "file": "outputs/report.csv",
    }


async def test_execute_dag_uses_injected_raw_skill_caller_and_events() -> None:
    dag = {
        "goal": "make artifact",
        "nodes": [
            {
                "id": "research",
                "agent": "researcher",
                "skill": "search",
                "args": {"topic": "agents"},
            },
            {
                "id": "write",
                "agent": "writer",
                "skill": "draft",
                "deps": ["research"],
                "args": {
                    "brief": "{{nodes.research.summary}}",
                    "payload": "{{nodes.research.result}}",
                },
            },
        ],
    }
    calls: list[tuple[str, str, dict]] = []
    events: list[dict] = []

    async def call_node(node: DagNode, rendered_args: dict) -> dict:
        calls.append((node.agent, node.skill, rendered_args))
        if node.id == "research":
            return {
                "ok": True,
                "grant_id": "grant-research",
                "result": {"summary": "done", "artifact": {"path": "outputs/a.md"}},
            }
        return {"ok": True, "result": {"message": "drafted"}}

    result = await execute_dag(
        "",
        json.dumps(dag),
        call_node,
        emit=events.append,
        run_id="dag-test",
    )

    assert result["ok"] is True
    assert result["summary"] == "completed 2/2 nodes"
    assert calls == [
        ("researcher", "search", {"topic": "agents"}),
        (
            "writer",
            "draft",
            {
                "brief": "summary=done",
                "payload": {"summary": "done", "artifact": {"path": "outputs/a.md"}},
            },
        ),
    ]
    assert [event["type"] for event in events] == [
        "dag_started",
        "dag_node_started",
        "dag_node_complete",
        "dag_node_started",
        "dag_node_complete",
        "dag_complete",
    ]


async def test_execute_dag_skips_unrun_nodes_after_failure() -> None:
    dag = {
        "nodes": [
            {"id": "a", "agent": "agent-a", "skill": "run", "args": {}},
            {
                "id": "b",
                "agent": "agent-b",
                "skill": "run",
                "deps": ["a"],
                "args": {},
            },
        ]
    }
    events: list[dict] = []

    async def call_node(node: DagNode, rendered_args: dict) -> dict:
        return {"ok": False, "error": "nope"}

    result = await execute_dag(
        "goal",
        json.dumps(dag),
        call_node,
        emit=events.append,
        run_id="dag-fail",
    )

    assert result["ok"] is False
    assert result["summary"] == "failed after 1/2 nodes"
    assert [node["node_id"] for node in result["nodes"]] == ["a", "b"]
    assert result["nodes"][1]["result"] == {"skipped": True}
    assert "dag_node_skipped" in [event["type"] for event in events]


async def test_execute_dag_nodes_validates_prebuilt_nodes() -> None:
    async def call_node(node: DagNode, rendered_args: dict) -> dict:
        raise AssertionError("invalid DAG should not execute")

    result = await execute_dag_nodes(
        "goal",
        [
            DagNode(id="a", agent="agent-a", skill="run", args={}, deps=("missing",)),
        ],
        call_node,
    )

    assert result == {
        "ok": False,
        "error": "node a depends on unknown node(s): ['missing']",
    }


async def test_context_node_caller_uses_discovery_call_and_grant_resolver() -> None:
    class _Found:
        name = "worker"
        url = "https://worker.example"

    class _Discovery:
        async def get_agent(self, name: str) -> _Found:
            assert name == "worker"
            return _Found()

    class _Ctx:
        discover = _Discovery()

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def call(
            self,
            target: str,
            skill: str,
            *,
            args: dict | None = None,
            grant: str | None = None,
            timeout: float | None = None,
            target_name: str | None = None,
        ) -> CallResult:
            self.calls.append(
                {
                    "target": target,
                    "skill": skill,
                    "args": args,
                    "grant": grant,
                    "timeout": timeout,
                    "target_name": target_name,
                }
            )
            return CallResult(
                result={"message": "done"},
                events=({"kind": "progress", "payload": {"message": "ok"}},),
                artifacts=({"name": "out.txt"},),
                grant_id="grant-1",
            )

    async def grant_resolver(node: DagNode, rendered_args: dict) -> str:
        assert node.id == "n1"
        assert rendered_args == {"x": 1}
        return "grant-token"

    ctx = _Ctx()
    caller = context_node_caller(ctx, grant_resolver=grant_resolver, timeout=12)

    result = await caller(
        DagNode(id="n1", agent="worker", skill="run", args={}),
        {"x": 1},
    )

    assert ctx.calls == [
        {
            "target": "https://worker.example",
            "skill": "run",
            "args": {"x": 1},
            "grant": "grant-token",
            "timeout": 12,
            "target_name": "worker",
        }
    ]
    assert result == {
        "ok": True,
        "grant_id": "grant-1",
        "result": {"message": "done"},
        "events": [{"kind": "progress", "payload": {"message": "ok"}}],
        "artifacts": [{"name": "out.txt"}],
    }
