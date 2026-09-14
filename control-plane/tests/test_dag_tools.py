from __future__ import annotations

import json

import pytest

from main_agent.tools.dag import (
    MAX_DAG_NODES,
    _parse_dag,
    _render_args,
    _topological_rounds,
)


def test_parse_dag_and_topological_rounds() -> None:
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
            {
                "id": "draft",
                "agent": "writer",
                "skill": "report",
                "deps": ["research", "chart"],
                "args": {"chart": "{{nodes.chart.file_ops[0].path}}"},
            },
        ],
    }

    goal, nodes = _parse_dag(json.dumps(dag))
    assert goal == "ship a report"
    assert [node.id for node in nodes] == ["research", "chart", "draft"]
    assert [[node.id for node in r] for r in _topological_rounds(nodes)] == [
        ["research"],
        ["chart"],
        ["draft"],
    ]


def test_parse_dag_rejects_cycles() -> None:
    dag = {
        "nodes": [
            {
                "id": "a",
                "agent": "agent-a",
                "skill": "run",
                "deps": ["b"],
                "args": {},
            },
            {
                "id": "b",
                "agent": "agent-b",
                "skill": "run",
                "deps": ["a"],
                "args": {},
            },
        ],
    }

    with pytest.raises(ValueError, match="dependency cycle"):
        _parse_dag(json.dumps(dag))


def test_parse_dag_rejects_too_many_nodes() -> None:
    dag = {
        "nodes": [
            {"id": f"n{i}", "agent": "worker", "skill": "run", "args": {}}
            for i in range(MAX_DAG_NODES + 1)
        ],
    }

    with pytest.raises(ValueError, match="too large"):
        _parse_dag(json.dumps(dag))


def test_render_args_supports_node_output_placeholders() -> None:
    completed = {
        "research": {
            "summary": "found demand",
            "grant_id": "grant-123",
            "result": {"score": 91},
            "file_ops": [
                {
                    "op": "create",
                    "path": "outputs/report.csv",
                    "size": 42,
                },
            ],
        },
    }

    rendered = _render_args(
        {
            "prompt": "Use {{nodes.research.summary}}",
            "grant": "{{nodes.research.grant_id}}",
            "result": "{{nodes.research.result}}",
            "score": "{{nodes.research.result.score}}",
            "file": "{{nodes.research.file_ops[0].path}}",
            "nested": ["score {{nodes.research.result.score}}"],
        },
        completed,
    )

    assert rendered == {
        "prompt": "Use found demand",
        "grant": "grant-123",
        "result": {"score": 91},
        "score": 91,
        "file": "outputs/report.csv",
        "nested": ["score 91"],
    }
