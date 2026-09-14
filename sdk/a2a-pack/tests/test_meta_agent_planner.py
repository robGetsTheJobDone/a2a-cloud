from __future__ import annotations

from typing import Any

from a2a_pack import (
    A2AAgent,
    DiscoveredAgent,
    Grant,
    InMemoryA2AClient,
    InMemoryDiscovery,
    LocalRunContext,
    LocalWorkspaceClient,
    MetaAgent,
    MetaAgentManifest,
    NoAuth,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
    build_manifest_dag_planner,
    run_meta_agent_goal,
    skill,
)


class _Writer(A2AAgent):
    name = "writer"
    description = "Drafts concise text."
    auth_model = NoAuth

    @skill(description="Draft text", tags=("writing",))
    async def draft(
        self,
        ctx: RunContext[NoAuth],
        topic: str,
        tone: str = "plain",
    ) -> dict[str, Any]:
        del ctx
        return {"summary": f"{tone}:{topic}"}


class _Chart(A2AAgent):
    name = "chart-agent"
    description = "Renders charts from text."
    auth_model = NoAuth

    @skill(description="Render chart", tags=("charting",))
    async def render_chart(
        self,
        ctx: RunContext[NoAuth],
        text: str,
    ) -> dict[str, Any]:
        del ctx
        return {"summary": f"chart:{text}"}


def _discovery() -> InMemoryDiscovery:
    return InMemoryDiscovery(
        {
            "writer": DiscoveredAgent("writer", None, _Writer().card()),
            "chart-agent": DiscoveredAgent("chart-agent", None, _Chart().card()),
        }
    )


def _manifest() -> MetaAgentManifest:
    return MetaAgentManifest.from_mapping(
        {
            "composition": {
                "max_nodes": 3,
                "max_parallel": 1,
                "sub_agents": [
                    {
                        "name": "writer",
                        "skills": ["draft"],
                        "default_args": {"tone": "sharp"},
                    },
                    {
                        "tag": "charting",
                        "skills": ["render_chart"],
                    },
                ],
            },
            "goal": {
                "objective": "Ship a report.",
                "success_criteria": ["draft and chart complete"],
            },
        }
    )


def _memory_workspace() -> LocalWorkspaceClient:
    ws = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(
            max_files=64,
            allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
            require_reason=False,
        ),
        bucket="user-42-files",
        issuer="studio-meta",
    )
    ws.install_grant(
        Grant(
            grant_id="memory-root",
            issuer="user-42",
            audience="studio-meta",
            bucket="user-42-files",
            mode=WorkspaceMode.READ_WRITE_OVERLAY,
            allow_patterns=("memory/**",),
            outputs_prefix="memory/",
        )
    )
    return ws


async def test_manifest_planner_resolves_raw_skills_and_defaults() -> None:
    ctx = LocalRunContext(auth=NoAuth(), discover=_discovery())
    captured: list[list[dict[str, str]]] = []

    async def llm_call(
        _ctx: RunContext[Any],
        messages: list[dict[str, str]],
        _state: dict[str, Any],
    ) -> dict[str, Any]:
        captured.append(messages)
        return {
            "nodes": [
                {
                    "id": "draft",
                    "agent": "writer",
                    "skill": "draft",
                    "args": {"topic": "kernel"},
                },
                {
                    "id": "chart",
                    "agent": "chart-agent",
                    "skill": "render_chart",
                    "deps": ["draft"],
                    "args": {"text": "{{nodes.draft.result.summary}}"},
                },
            ]
        }

    planner = build_manifest_dag_planner(
        ctx,
        manifest=_manifest(),
        llm_call=llm_call,
    )
    planned = await planner({"goal": "Ship a report.", "attempt": 0})

    assert planned["nodes"][0]["agent"] == "writer"
    assert planned["nodes"][0]["skill"] == "draft"
    assert planned["nodes"][0]["args"] == {"tone": "sharp", "topic": "kernel"}
    assert planned["nodes"][1]["agent"] == "chart-agent"
    prompt = captured[0][1]["content"]
    assert "writer" in prompt
    assert "draft" in prompt
    assert "render_chart" in prompt


async def test_manifest_planner_rejects_undeclared_skill_before_execution() -> None:
    ctx = LocalRunContext(auth=NoAuth(), discover=_discovery())
    called = False

    async def llm_call(
        _ctx: RunContext[Any],
        _messages: list[dict[str, str]],
        _state: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": "bad",
                    "agent": "writer",
                    "skill": "delete_repo",
                    "args": {},
                }
            ]
        }

    async def call_node(_node: Any, _args: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True}

    result = await run_meta_agent_goal(
        ctx,
        goal="Ship a report.",
        planner=build_manifest_dag_planner(
            ctx,
            manifest=_manifest(),
            llm_call=llm_call,
        ),
        call_node=call_node,
    )

    assert result["ok"] is False
    assert result["status"] == "error"
    assert "undeclared skill" in result["summary"]
    assert called is False
    assert any(event.kind == "meta_plan_error" for event in ctx.events)


async def test_meta_agent_base_pursue_runs_raw_skill_dag() -> None:
    class _StudioMeta(MetaAgent):
        name = "studio-meta"
        description = "Coordinates a writer."
        auth_model = NoAuth
        meta_agent_manifest = _manifest()

        async def planner_llm_call(
            self,
            _ctx: RunContext[Any],
            _messages: list[dict[str, str]],
            _state: dict[str, Any],
        ) -> dict[str, Any]:
            return {
                "nodes": [
                    {
                        "id": "draft",
                        "agent": "writer",
                        "skill": "draft",
                        "args": {"topic": "kernel"},
                    }
                ]
            }

    ctx = LocalRunContext(
        auth=NoAuth(),
        discover=_discovery(),
        a2a=InMemoryA2AClient(agents={"writer": _Writer()}),
    )

    result = await _StudioMeta().invoke("pursue", ctx, goal="Ship a report.")

    assert result["ok"] is True
    assert result["result"]["nodes"][0]["result"]["summary"] == "sharp:kernel"
    assert any(event.kind == "meta_goal" for event in ctx.events)
    assert any(event.kind == "meta_plan" for event in ctx.events)
    assert any(event.kind == "dag" for event in ctx.events)


async def test_meta_agent_pursue_uses_manifest_memory_between_runs() -> None:
    captured: list[str] = []

    class _MemoryMeta(MetaAgent):
        name = "studio-meta"
        description = "Coordinates a writer with memory."
        auth_model = NoAuth
        meta_agent_manifest = MetaAgentManifest.from_mapping(
            {
                "composition": {
                    "max_nodes": 3,
                    "max_parallel": 1,
                    "sub_agents": [
                        {
                            "name": "writer",
                            "skills": ["draft"],
                            "default_args": {"tone": "sharp"},
                        }
                    ],
                },
                "goal": {
                    "objective": "Ship a report.",
                    "success_criteria": ["draft complete"],
                },
                "memory": {"tiers": ["files"], "namespace": "runs"},
            }
        )

        async def planner_llm_call(
            self,
            _ctx: RunContext[Any],
            messages: list[dict[str, str]],
            _state: dict[str, Any],
        ) -> dict[str, Any]:
            captured.append(messages[1]["content"])
            return {
                "nodes": [
                    {
                        "id": "draft",
                        "agent": "writer",
                        "skill": "draft",
                        "args": {"topic": "kernel"},
                    }
                ]
            }

    ctx = LocalRunContext(
        auth=NoAuth(),
        discover=_discovery(),
        a2a=InMemoryA2AClient(agents={"writer": _Writer()}),
        workspace=_memory_workspace(),
        caller="user-42",
    )

    await _MemoryMeta().invoke("pursue", ctx, goal="Ship a report.")
    await _MemoryMeta().invoke("pursue", ctx, goal="Ship a report.")

    assert len(captured) == 2
    assert '"relevant"' not in captured[0]
    assert '"relevant"' in captured[1]
    assert '"source": "meta-agent"' in captured[1]
