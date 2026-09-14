from __future__ import annotations

import pytest

from a2a_pack import (
    Grant,
    LocalRunContext,
    LocalWorkspaceClient,
    MetaRunPlan,
    NoAuth,
    WorkspaceAccess,
    WorkspaceMode,
)


def _workspace() -> LocalWorkspaceClient:
    ws = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(require_reason=False),
        bucket="user-42-files",
        issuer="meta-agent",
    )
    ws.install_grant(
        Grant(
            grant_id="g1",
            issuer="user-42",
            audience="meta-agent",
            bucket="user-42-files",
            mode=WorkspaceMode.READ_ONLY,
        )
    )
    return ws


async def test_ctx_meta_runs_calls_control_plane(monkeypatch) -> None:
    calls: list[dict] = []

    class _Response:
        status_code = 201
        text = "ok"

        def json(self) -> dict:
            return {
                "run_id": "run-1",
                "agent_name": "meta-agent",
                "thread_id": "thread-1",
                "goal": "build a planner",
                "success_criteria": ["plan exists"],
                "status": "planning",
                "current_plan": {"nodes": []},
                "progress": [],
                "state": {},
                "summary": None,
                "error": None,
                "created_at": "2026-06-02T00:00:00+00:00",
                "updated_at": "2026-06-02T00:00:00+00:00",
                "completed_at": None,
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return _Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ctx = LocalRunContext(auth=NoAuth(), workspace=_workspace())
    ctx._cp_url = "https://api.example"  # noqa: SLF001
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001

    run = await ctx.meta_runs.create(
        run_id="run-1",
        thread_id="thread-1",
        goal="build a planner",
        success_criteria=["plan exists"],
        current_plan={"nodes": []},
    )

    assert run.run_id == "run-1"
    assert run.current_plan.nodes == []
    assert calls == [
        {
            "method": "POST",
            "url": "https://api.example/v1/agents/meta-agent/meta-runs",
            "headers": {"authorization": "Bearer jwt-123"},
            "json": {
                "goal": "build a planner",
                "success_criteria": ["plan exists"],
                "thread_id": "thread-1",
                "run_id": "run-1",
                "status": "planning",
                "current_plan": {"nodes": []},
                "progress": [],
                "state": {},
                "summary": None,
            },
        }
    ]


async def test_ctx_meta_runs_rejects_malformed_plan_before_http(monkeypatch) -> None:
    class _Client:
        async def __aenter__(self) -> "_Client":
            raise AssertionError("malformed plan should not reach HTTP")

        async def __aexit__(self, *args) -> None:
            return None

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ctx = LocalRunContext(auth=NoAuth(), workspace=_workspace())
    ctx._cp_url = "https://api.example"  # noqa: SLF001
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001

    with pytest.raises(ValueError):
        await ctx.meta_runs.create(
            goal="build a planner",
            current_plan={"nodes": [{"agent": "writer", "skill": "draft"}]},
        )


def test_meta_run_plan_preserves_extra_plan_metadata() -> None:
    plan = MetaRunPlan.model_validate(
        {
            "ok": True,
            "nodes": [
                {
                    "id": "draft",
                    "agent": "writer",
                    "skill": "draft",
                    "args": {},
                    "rank": 1,
                }
            ],
            "planner": "meta-dag",
        }
    )

    assert plan.nodes[0].id == "draft"
    assert plan.model_dump(mode="json")["nodes"][0]["rank"] == 1
    assert plan.model_dump(mode="json")["planner"] == "meta-dag"
