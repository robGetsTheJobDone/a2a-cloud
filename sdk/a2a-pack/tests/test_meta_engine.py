from __future__ import annotations

from a2a_pack import DagLimits, LocalRunContext, NoAuth, run_meta_agent_goal


async def test_meta_engine_replans_after_failed_node() -> None:
    plans = [
        {
            "goal": "ship report",
            "nodes": [
                {
                    "id": "draft",
                    "agent": "writer",
                    "skill": "draft",
                    "args": {"fail": True},
                }
            ],
        },
        {
            "goal": "ship report",
            "nodes": [
                {
                    "id": "draft",
                    "agent": "writer",
                    "skill": "draft",
                    "args": {"fail": False},
                }
            ],
        },
    ]
    planner_states: list[dict] = []

    def planner(state: dict):
        planner_states.append(state)
        return plans[state["attempt"]]

    async def call_node(node, rendered_args):
        if rendered_args.get("fail"):
            return {"ok": False, "error": "draft failed"}
        return {"ok": True, "result": {"summary": "draft ready"}}

    ctx = LocalRunContext(auth=NoAuth())
    result = await run_meta_agent_goal(
        ctx,
        goal="ship report",
        success_criteria=["draft ready"],
        planner=planner,
        call_node=call_node,
        max_replans=1,
        limits=DagLimits(max_nodes=2, max_parallel=1),
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert len(planner_states) == 2
    assert planner_states[1]["previous_result"]["result"]["ok"] is False
    assert any(event.kind == "meta_replan" for event in ctx.events)


async def test_meta_engine_returns_error_for_invalid_plan() -> None:
    ctx = LocalRunContext(auth=NoAuth())

    result = await run_meta_agent_goal(
        ctx,
        goal="ship report",
        planner=lambda _state: {"nodes": []},
        call_node=lambda _node, _args: {"ok": True},
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert "non-empty" in result["summary"]
