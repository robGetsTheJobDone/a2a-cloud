from __future__ import annotations

import pytest

from a2a_pack import (
    Grant,
    LocalRunContext,
    LocalWorkspaceClient,
    NoAuth,
    WorkspaceAccess,
    WorkspaceDenied,
    WorkspaceMode,
    memory_tools,
)


def _memory_workspace(*, writable: bool = True) -> LocalWorkspaceClient:
    mode = WorkspaceMode.READ_WRITE_OVERLAY if writable else WorkspaceMode.READ_ONLY
    ws = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(
            max_files=64,
            allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
            require_reason=False,
        ),
        bucket="user-42-files",
        issuer="meta-agent",
    )
    ws.install_grant(
        Grant(
            grant_id="memory-root",
            issuer="user-42",
            audience="meta-agent",
            bucket="user-42-files",
            mode=mode,
            allow_patterns=("memory/**",),
            outputs_prefix="memory/",
        )
    )
    return ws


async def test_ctx_memory_persists_notes_across_contexts() -> None:
    ws = _memory_workspace()
    first = LocalRunContext(auth=NoAuth(), workspace=ws, caller="user-42")

    saved = await first.memory.put_note(
        "plans/current",
        {"goal": "ship meta-agent memory", "status": "draft"},
        metadata={"source": "test"},
    )

    second = LocalRunContext(auth=NoAuth(), workspace=ws, caller="user-42")
    recalled = await second.memory.get_note("plans/current")
    listed = await second.memory.list_notes()
    hits = await second.memory.search_notes("meta-agent")

    assert saved.key == "plans/current"
    assert recalled is not None
    assert recalled.value["goal"] == "ship meta-agent memory"
    assert recalled.metadata == {"source": "test"}
    assert [item.key for item in listed] == ["plans/current"]
    assert [item.key for item in hits] == ["plans/current"]
    assert "memory/meta-agent/user-42/notes/plans/current.json" in set(ws.iter_paths())


async def test_ctx_memory_appends_jsonl_logs() -> None:
    ws = _memory_workspace()
    ctx = LocalRunContext(auth=NoAuth(), workspace=ws, caller="user-42")

    await ctx.memory.append_log("runs", {"step": 1})
    await ctx.memory.append_log("runs", {"step": 2})

    entries = await ctx.memory.read_log("runs")

    assert [entry.value["step"] for entry in entries] == [1, 2]
    assert "memory/meta-agent/user-42/logs/runs.jsonl" in set(ws.iter_paths())


async def test_ctx_memory_is_scoped_by_agent_and_caller() -> None:
    ws = _memory_workspace()
    user_42 = LocalRunContext(auth=NoAuth(), workspace=ws, caller="user-42")
    user_7 = LocalRunContext(auth=NoAuth(), workspace=ws, caller="user-7")

    await user_42.memory.remember("preference", "dark-mode")

    assert await user_42.memory.recall("preference") is not None
    assert await user_7.memory.recall("preference") is None


async def test_ctx_memory_respects_workspace_write_grant() -> None:
    ctx = LocalRunContext(auth=NoAuth(), workspace=_memory_workspace(writable=False))

    with pytest.raises(WorkspaceDenied, match="memory write denied"):
        await ctx.memory.put_note("plan", {"status": "blocked"})


async def test_ctx_memory_rejects_unsafe_keys() -> None:
    ctx = LocalRunContext(auth=NoAuth(), workspace=_memory_workspace())

    with pytest.raises(ValueError, match="safe relative path"):
        await ctx.memory.put_note("../secret", "nope")


def test_memory_tools_returns_memory_functions() -> None:
    ctx = LocalRunContext(auth=NoAuth(), workspace=_memory_workspace())

    tools = memory_tools(ctx)
    names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in tools}

    assert {"remember", "recall", "list_notes", "search_notes", "append_log"}.issubset(
        names
    )


async def test_ctx_memory_kv_tier_calls_control_plane(monkeypatch) -> None:
    calls: list[dict] = []

    class _Response:
        status_code = 200
        text = "ok"

        def json(self) -> dict:
            return {
                "agent_name": "meta-agent",
                "namespace": "notes",
                "key": "plan",
                "value": {"status": "saved"},
                "metadata": {"source": "test"},
                "created_at": "2026-06-02T00:00:00+00:00",
                "updated_at": "2026-06-02T00:00:00+00:00",
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
    ctx = LocalRunContext(auth=NoAuth(), workspace=_memory_workspace())
    ctx._cp_url = "https://api.example"  # noqa: SLF001
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001

    saved = await ctx.memory.for_tier("kv", agent_name="meta-agent").put_note(
        "plan",
        {"status": "saved"},
        metadata={"source": "test"},
    )

    assert saved.value == {"status": "saved"}
    assert calls == [
        {
            "method": "PUT",
            "url": "https://api.example/v1/agents/meta-agent/memory",
            "headers": {"authorization": "Bearer jwt-123"},
            "json": {
                "namespace": "notes",
                "key": "plan",
                "value": {"status": "saved"},
                "metadata": {"source": "test"},
            },
        }
    ]


async def test_ctx_memory_vector_tier_calls_semantic_control_plane_search(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class _Response:
        status_code = 200
        text = "ok"

        def json(self) -> list[dict]:
            return [
                {
                    "agent_name": "meta-agent",
                    "namespace": "notes",
                    "key": "plan",
                    "value": {"status": "saved"},
                    "metadata": {"source": "semantic"},
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:00+00:00",
                    "score": 0.91,
                }
            ]

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
    ctx = LocalRunContext(auth=NoAuth(), workspace=_memory_workspace())
    ctx._cp_url = "https://api.example"  # noqa: SLF001
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001

    hits = await ctx.memory.for_tier("vector", agent_name="meta-agent").search(
        "saved plan",
        limit=3,
    )

    assert [hit.key for hit in hits] == ["plan"]
    assert calls == [
        {
            "method": "GET",
            "url": "https://api.example/v1/agents/meta-agent/memory/search",
            "headers": {"authorization": "Bearer jwt-123"},
            "params": {"namespace": "notes", "limit": 3, "q": "saved plan"},
        }
    ]


async def test_ctx_memory_manifest_policy_routes_kv_and_vector(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    class _Response:
        status_code = 200
        text = "ok"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            payload = {
                "agent_name": "meta-agent",
                "namespace": kwargs.get("json", {}).get("namespace", "brain"),
                "key": kwargs.get("json", {}).get("key", "plan"),
                "value": kwargs.get("json", {}).get("value", {"hit": True}),
                "metadata": kwargs.get("json", {}).get("metadata", {}),
                "created_at": "2026-06-02T00:00:00+00:00",
                "updated_at": "2026-06-02T00:00:00+00:00",
            }
            if url.endswith("/search"):
                payload = [{**payload, "score": 0.75}]
            return _Response(payload)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ctx = LocalRunContext(auth=NoAuth(), workspace=_memory_workspace())
    ctx._cp_url = "https://api.example"  # noqa: SLF001
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001
    memory = ctx.memory.for_manifest(
        {"tiers": ["files", "kv", "vector"], "namespace": "brain"},
        agent_name="meta-agent",
    )

    saved = await memory.remember("plan", {"status": "saved"})
    hits = await memory.search("saved plan")

    assert saved.namespace == "brain"
    assert [hit.key for hit in hits] == ["plan"]
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"] == "https://api.example/v1/agents/meta-agent/memory"
    assert calls[0]["json"]["namespace"] == "brain"
    assert calls[1]["method"] == "GET"
    assert calls[1]["url"] == "https://api.example/v1/agents/meta-agent/memory/search"
    assert calls[1]["params"] == {"namespace": "brain", "limit": 10, "q": "saved plan"}
