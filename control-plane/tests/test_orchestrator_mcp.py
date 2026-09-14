from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from types import SimpleNamespace

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi import FastAPI
from fastapi.testclient import TestClient

import control_plane.orchestrator_mcp as orchestrator_mcp
from control_plane.orchestrator_mcp import ConnectorControlPlaneOrchestratorAgent
from control_plane.orchestrator_mcp import _McpChatJob
from control_plane.orchestrator_mcp import mount_orchestrator_mcp
from control_plane.orchestrator_mcp import _bridge_interaction_event
from control_plane.orchestrator_mcp import _capture_connector_interrupt
from control_plane.orchestrator_mcp import _submit_connector_interaction
from control_plane.pending_actions import PendingActionsStore
from control_plane.pending_actions import create_pending_actions_store


def _app(monkeypatch) -> FastAPI:
    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.delenv("A2A_AGENT_OWNER_ID", raising=False)
    monkeypatch.delenv("A2A_AGENT_PUBLIC", raising=False)
    monkeypatch.delenv("A2A_OAUTH_RESOURCE", raising=False)
    monkeypatch.delenv("A2A_OAUTH_SCOPES", raising=False)
    monkeypatch.setenv("A2A_CP_URL", "https://api.example.test")
    app = FastAPI()
    mount_orchestrator_mcp(app)
    return app


def test_orchestrator_mcp_requires_bearer_and_advertises_prm(monkeypatch) -> None:
    client = TestClient(_app(monkeypatch))

    response = client.post(
        "/mcp",
        headers={"x-forwarded-proto": "https"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer resource_metadata=")
    assert "https://testserver/.well-known/oauth-protected-resource" in challenge


def test_orchestrator_mcp_serves_oauth_protected_resource_metadata(monkeypatch) -> None:
    client = TestClient(_app(monkeypatch))

    response = client.get(
        "/.well-known/oauth-protected-resource",
        headers={"x-forwarded-proto": "https"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://testserver"
    assert body["authorization_servers"] == [
        "https://auth.a2acloud.io/realms/a2acloud"
    ]
    assert "mcp:invoke" in body["scopes_supported"]
    assert "orchestrator:run" in body["scopes_supported"]


def test_orchestrator_mcp_lists_chat_tool_for_verified_user(monkeypatch) -> None:
    import a2a_pack.mcp.http as mcp_http

    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "cp-token"
        assert cp_url == "https://api.example.test"
        return 2

    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    client = TestClient(_app(monkeypatch))

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer cp-token"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["chat"]
    assert tools[0]["inputSchema"]["required"] == ["prompt"]


def test_orchestrator_mcp_requires_orchestrator_run_scope(monkeypatch) -> None:
    import a2a_pack.mcp.http as mcp_http

    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "keycloak-token"
        assert cp_url == "https://api.example.test"
        return 2

    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    monkeypatch.setattr(mcp_http._oauth, "is_keycloak_token", lambda token: True)
    monkeypatch.setattr(
        mcp_http._oauth,
        "validate_keycloak_token",
        lambda token, *, audience=None: {"scope": "mcp:invoke agent:read"},
    )
    client = TestClient(_app(monkeypatch))

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer keycloak-token"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "chat", "arguments": {"prompt": "hello"}},
        },
    )

    assert response.status_code == 403
    assert "orchestrator:run" in response.text


def test_connector_mcp_lists_async_tools_for_verified_user(monkeypatch) -> None:
    import a2a_pack.mcp.http as mcp_http

    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "cp-token"
        assert cp_url == "https://api.example.test"
        return 2

    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    client = TestClient(_app(monkeypatch))

    response = client.post(
        "/connector-mcp",
        headers={"Authorization": "Bearer cp-token"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "chat",
        "chat_result",
        "resume_interaction",
    ]
    chat_schema = tools[0]["inputSchema"]
    assert "approval_token" not in chat_schema["properties"]
    assert tools[1]["inputSchema"]["required"] == ["job_id"]
    assert "approval_token" not in tools[2]["inputSchema"]["properties"]
    assert tools[2]["inputSchema"]["required"] == ["job_id"]
    assert "approvals" in tools[2]["inputSchema"]["properties"]


def test_connector_mcp_v2_alias_lists_fresh_async_tools(monkeypatch) -> None:
    import a2a_pack.mcp.http as mcp_http

    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "cp-token"
        assert cp_url == "https://api.example.test"
        return 2

    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    client = TestClient(_app(monkeypatch))

    response = client.post(
        "/connector-mcp-v2",
        headers={"Authorization": "Bearer cp-token"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "chat",
        "chat_result",
        "resume_interaction",
    ]
    assert "approval_token" not in tools[2]["inputSchema"]["properties"]


def test_connector_default_waits_up_to_10_seconds(monkeypatch) -> None:
    monkeypatch.delenv("A2A_ORCHESTRATOR_MCP_ASYNC_AFTER_SECONDS", raising=False)
    assert orchestrator_mcp._async_after_seconds() == 10.0


async def test_chat_returns_background_job_after_deadline(monkeypatch) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()
    monkeypatch.setenv("A2A_ORCHESTRATOR_MCP_ASYNC_AFTER_SECONDS", "0.01")

    async def fake_user_id_for_token(token: str) -> int:
        return 2

    async def fake_persist_connector_job(job, *, store=None) -> None:
        return None

    async def fake_run_chat_job(job, **kwargs) -> None:
        job.user_id = 2
        job.thread_id = "thread-1"
        job.events = [{"type": "tool_call", "tool": "call_agent"}]
        job.started.set()
        job.touch()
        await asyncio.sleep(0.05)
        job.content = "finished later"
        job.status = "ok"
        job.touch()
        job.done.set()

    monkeypatch.setattr(
        orchestrator_mcp,
        "_persist_connector_job",
        fake_persist_connector_job,
    )
    monkeypatch.setattr(orchestrator_mcp, "_user_id_for_token", fake_user_id_for_token)
    monkeypatch.setattr(orchestrator_mcp, "_run_chat_job", fake_run_chat_job)

    agent = ConnectorControlPlaneOrchestratorAgent()
    ctx = SimpleNamespace(cp_jwt="cp-token")
    response = await agent.chat(ctx, prompt="slow task")

    assert response["status"] == "running"
    assert response["thread_id"] == "thread-1"
    assert response["poll_tool"] == "chat_result"
    assert response["events"] == [{"type": "tool_call", "tool": "call_agent"}]

    job = orchestrator_mcp._LOCAL_CHAT_JOBS[response["job_id"]]
    await asyncio.wait_for(job.done.wait(), timeout=1.0)

    poll = await agent.chat_result(
        ctx,
        job_id=response["job_id"],
        thread_id=response["thread_id"],
    )
    assert poll["status"] == "ok"
    assert poll["content"] == "finished later"


async def test_connector_chat_enqueues_when_worker_queue_enabled(monkeypatch) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()
    monkeypatch.setenv("A2A_ORCHESTRATOR_MCP_WORKER_QUEUE_ENABLED", "true")
    monkeypatch.setenv("A2A_ORCHESTRATOR_MCP_ASYNC_AFTER_SECONDS", "0.01")
    enqueued: list[str] = []
    persisted_statuses: list[str] = []

    async def fake_user_id_for_token(token: str) -> int:
        return 2

    async def fake_persist_connector_job(job, *, store=None) -> None:
        persisted_statuses.append(job.status)

    async def fake_enqueue_connector_job(job_id: str) -> None:
        enqueued.append(job_id)

    async def fake_wait_for_stored_connector_response(
        job_id: str,
        *,
        timeout: float,
        fallback_job,
    ) -> dict:
        assert timeout == 0.01
        assert fallback_job.status == "queued"
        return {
            "status": "queued",
            "job_id": job_id,
            "thread_id": None,
            "poll_tool": "chat_result",
            "content": "queued",
        }

    async def fail_run_chat_job(*args, **kwargs) -> None:
        raise AssertionError("_run_chat_job should not run in the API process")

    monkeypatch.setattr(orchestrator_mcp, "_user_id_for_token", fake_user_id_for_token)
    monkeypatch.setattr(
        orchestrator_mcp,
        "_persist_connector_job",
        fake_persist_connector_job,
    )
    monkeypatch.setattr(
        orchestrator_mcp,
        "_enqueue_connector_job",
        fake_enqueue_connector_job,
    )
    monkeypatch.setattr(
        orchestrator_mcp,
        "_wait_for_stored_connector_response",
        fake_wait_for_stored_connector_response,
    )
    monkeypatch.setattr(orchestrator_mcp, "_run_chat_job", fail_run_chat_job)

    agent = ConnectorControlPlaneOrchestratorAgent()
    response = await agent.chat(
        SimpleNamespace(cp_jwt="cp-token"),
        prompt="slow task",
        thread_id="thread-in",
        organization_slug="org",
        approval_mode=True,
    )

    assert response["status"] == "queued"
    assert len(enqueued) == 1
    assert persisted_statuses == ["starting", "queued"]
    assert orchestrator_mcp._LOCAL_CHAT_JOBS == {}


async def test_connector_worker_runs_persisted_job_with_fresh_user_token(
    monkeypatch,
) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()
    calls: list[dict] = []

    def fake_issue_token(user_id: int, *, ttl_seconds: int | None = None) -> str:
        assert user_id == 2
        # In-process orchestrator identity, bounded by the job's own lifetime
        # rather than the default week.
        assert ttl_seconds == orchestrator_mcp._JOB_TTL_SECONDS
        return "fresh-cp-token"

    async def fake_run_chat_job(job, **kwargs) -> None:
        calls.append({"job": job, **kwargs})
        job.status = "ok"
        job.content = "done"
        job.done.set()

    monkeypatch.setattr(orchestrator_mcp, "issue_token", fake_issue_token)
    monkeypatch.setattr(orchestrator_mcp, "_run_chat_job", fake_run_chat_job)

    await orchestrator_mcp._run_connector_job_from_payload(
        {
            "job_id": "job-worker",
            "token_hash": "old-hash",
            "prompt": "do work",
            "status": "queued",
            "user_id": 2,
            "input_thread_id": "thread-in",
            "organization_slug": "org",
            "approval_mode": True,
        }
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["token"] == "fresh-cp-token"
    assert call["ctx"].cp_jwt == "fresh-cp-token"
    assert call["prompt"] == "do work"
    assert call["thread_id"] == "thread-in"
    assert call["organization_slug"] == "org"
    assert call["approval_mode"] is True
    assert call["connector_mode"] is True
    assert "job-worker" not in orchestrator_mcp._LOCAL_CHAT_JOBS


async def test_connector_dequeue_socket_timeout_exceeds_blocking_pop(monkeypatch) -> None:
    calls: list[float] = []

    class FakeRedis:
        async def brpop(self, key: str, *, timeout: int):
            assert key == orchestrator_mcp._CONNECTOR_JOB_QUEUE_KEY
            assert timeout == 30
            return None

        async def aclose(self) -> None:
            return None

    async def fake_connector_redis_client(*, socket_timeout: float):
        calls.append(socket_timeout)
        return FakeRedis()

    monkeypatch.setattr(
        orchestrator_mcp,
        "_connector_redis_client",
        fake_connector_redis_client,
    )

    assert await orchestrator_mcp._dequeue_connector_job(timeout_seconds=30) is None
    assert calls == [35.0]


async def test_chat_waits_for_approval_and_batches_sibling_approvals(monkeypatch) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()
    monkeypatch.setenv("A2A_ORCHESTRATOR_MCP_ASYNC_AFTER_SECONDS", "1.0")
    monkeypatch.setenv("A2A_ORCHESTRATOR_MCP_APPROVAL_BATCH_WINDOW_SECONDS", "0.02")

    async def fake_user_id_for_token(token: str) -> int:
        return 2

    async def fake_persist_connector_job(job, *, store=None) -> None:
        return None

    async def fake_run_chat_job(job, **kwargs) -> None:
        job.user_id = 2
        job.thread_id = "thread-approval"
        job.started.set()
        job.touch()
        await asyncio.sleep(0.01)
        _capture_connector_interrupt(
            job,
            user_id=2,
            event={
                "type": "scope_approval_required",
                "approval_id": "scope-1",
                "reason": "Need repo read access.",
                "requested": {"mode": "read_only"},
            },
        )
        await asyncio.sleep(0.005)
        _capture_connector_interrupt(
            job,
            user_id=2,
            event={
                "type": "scope_approval_required",
                "approval_id": "scope-2",
                "reason": "Need task write access.",
                "requested": {"mode": "read_write_overlay"},
            },
        )
        await asyncio.sleep(10)

    monkeypatch.setattr(
        orchestrator_mcp,
        "_persist_connector_job",
        fake_persist_connector_job,
    )
    monkeypatch.setattr(orchestrator_mcp, "_user_id_for_token", fake_user_id_for_token)
    monkeypatch.setattr(orchestrator_mcp, "_run_chat_job", fake_run_chat_job)

    agent = ConnectorControlPlaneOrchestratorAgent()
    ctx = SimpleNamespace(cp_jwt="cp-token")
    response = await agent.chat(ctx, prompt="run dag")

    assert response["status"] == "approval_required"
    assert response["thread_id"] == "thread-approval"
    assert response["approval_count"] == 2
    assert [item["approval_id"] for item in response["approvals"]] == [
        "scope-1",
        "scope-2",
    ]
    assert response["resume"]["approve_all_arguments"] == {
        "job_id": response["job_id"],
        "decision": "approve",
    }
    assert "approval_token" not in response

    job = orchestrator_mcp._LOCAL_CHAT_JOBS[response["job_id"]]
    assert job.detached is True
    assert job.task is not None
    job.task.cancel()
    with suppress(asyncio.CancelledError):
        await job.task


async def test_resume_interaction_missing_job_returns_expired(monkeypatch) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()

    async def fake_load_connector_job(job_id: str) -> dict | None:
        return None

    monkeypatch.setattr(orchestrator_mcp, "_load_connector_job", fake_load_connector_job)
    agent = ConnectorControlPlaneOrchestratorAgent()
    ctx = SimpleNamespace(cp_jwt="cp-token")

    response = await agent.resume_interaction(
        ctx,
        job_id="missing-job",
        decision="approve",
    )

    assert response["status"] == "expired"
    assert response["job_id"] == "missing-job"
    assert "start the request again" in response["content"]


async def test_resume_interaction_resolves_redis_backed_job_from_another_worker(
    monkeypatch,
) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()
    shared_store = create_pending_actions_store(None)

    def fake_create_pending_actions_store(redis_url: str | None) -> PendingActionsStore:
        return PendingActionsStore(shared_store.backend)

    monkeypatch.setattr(
        orchestrator_mcp,
        "create_pending_actions_store",
        fake_create_pending_actions_store,
    )

    async def fake_user_id_for_token(token: str) -> int:
        return 2

    monkeypatch.setattr(orchestrator_mcp, "_user_id_for_token", fake_user_id_for_token)

    store = fake_create_pending_actions_store(None)
    job = _McpChatJob(
        job_id="job-redis",
        token_hash=orchestrator_mcp._token_hash("cp-token"),
        prompt="Need approval",
        thread_id="thread-redis",
    )
    job.pending_store = store
    handled = _capture_connector_interrupt(
        job,
        user_id=2,
        event={
            "type": "scope_approval_required",
            "approval_id": "scope-redis",
            "reason": "Need read access.",
            "requested": {
                "mode": "read_only",
                "approval_timeout_seconds": 45,
            },
        },
    )
    assert handled is True
    assert job.interrupt is not None
    await orchestrator_mcp._persist_connector_job(job, store=store)

    # Simulate resume_interaction hitting a different uvicorn worker: the
    # running asyncio task is not in this process, but Redis has the interrupt.
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()

    agent = ConnectorControlPlaneOrchestratorAgent()
    ctx = SimpleNamespace(cp_jwt="cp-token")
    response = await agent.resume_interaction(
        ctx,
        job_id="job-redis",
        decision="approve",
    )

    assert response["status"] == "approval_pending"
    assert response["job_id"] == "job-redis"
    assert response["poll_tool"] == "chat_result"
    assert (
        await shared_store.wait(
            kind="scope",
            request_id="scope-redis",
            user_id=2,
            timeout_seconds=0.01,
            ttl_seconds=45,
        )
    ) == "approve"

    stored = await orchestrator_mcp._load_connector_job("job-redis")
    assert stored is not None
    assert stored["status"] == "approval_pending"
    assert stored["interrupt"] is None

    orchestrator_mcp._LOCAL_CHAT_JOBS["job-redis"] = job
    poll = await agent.chat_result(ctx, job_id="job-redis")
    assert poll["status"] == "approval_pending"
    assert poll["poll_tool"] == "chat_result"


async def test_chat_result_authorizes_redis_job_by_user_id(monkeypatch) -> None:
    orchestrator_mcp._LOCAL_CHAT_JOBS.clear()
    shared_store = create_pending_actions_store(None)

    def fake_create_pending_actions_store(redis_url: str | None) -> PendingActionsStore:
        return PendingActionsStore(shared_store.backend)

    async def fake_user_id_for_token(token: str) -> int:
        assert token == "refreshed-token"
        return 2

    monkeypatch.setattr(
        orchestrator_mcp,
        "create_pending_actions_store",
        fake_create_pending_actions_store,
    )
    monkeypatch.setattr(orchestrator_mcp, "_user_id_for_token", fake_user_id_for_token)

    store = fake_create_pending_actions_store(None)
    job = _McpChatJob(
        job_id="job-user-id",
        token_hash=orchestrator_mcp._token_hash("old-token"),
        prompt="Done",
        thread_id="thread-user-id",
        user_id=2,
        status="ok",
        content="finished",
    )
    await orchestrator_mcp._persist_connector_job(job, store=store)

    agent = ConnectorControlPlaneOrchestratorAgent()
    response = await agent.chat_result(
        SimpleNamespace(cp_jwt="refreshed-token"),
        job_id="job-user-id",
    )

    assert response["status"] == "ok"
    assert response["content"] == "finished"


async def test_connector_approval_interrupt_is_structured_and_resumable() -> None:
    store = create_pending_actions_store(None)
    job = _McpChatJob(
        job_id="job-1",
        token_hash="token-hash",
        prompt="Create an agent",
        thread_id="thread-1",
    )
    job.pending_store = store

    handled = _capture_connector_interrupt(
        job,
        user_id=2,
        event={
            "type": "scope_approval_required",
            "approval_id": "scope-1",
            "reason": "Need to write generated code.",
            "requested": {
                "mode": "read_write_overlay",
                "write_prefixes": ["agents/demo/"],
                "approval_timeout_seconds": 45,
            },
        },
    )

    assert handled is True
    assert job.status == "approval_required"
    assert job.interrupt is not None
    assert job.interrupt["approval_id"] == "scope-1"
    assert job.interrupt["approval_type"] == "a2a_grant_extension"
    assert "Do not tell the user to approve in the A2A dashboard" in job.interrupt["content"]
    assert job.interrupt["resume"]["approve_arguments"]["decision"] == "approve"
    assert job.interrupt["resume"]["deny_arguments"]["decision"] == "deny"
    assert job.interrupt["retry"]["tool"] == "resume_interaction"
    public = orchestrator_mcp._job_response(job)
    assert "approval_token" not in public
    assert "approval_token" not in public["resume"]["approve_arguments"]
    assert "approval_token" not in public["retry"]

    await _submit_connector_interaction(
        job,
        decision="approve",
        answer=None,
        value=None,
        grant_id="grt_123",
    )

    assert job.status == "approval_pending"
    assert job.interrupt is None
    assert (
        await store.wait(
            kind="scope",
            request_id="scope-1",
            user_id=2,
            timeout_seconds=0.01,
            ttl_seconds=45,
        )
    ) == "approve"


async def test_batch_approval_interrupts_resolve_with_one_decision() -> None:
    store = create_pending_actions_store(None)
    job = _McpChatJob(
        job_id="job-batch",
        token_hash="token-hash",
        prompt="Run a DAG",
        thread_id="thread-batch",
    )
    job.pending_store = store

    for approval_id, reason in (
        ("scope-1", "Allow code editor read."),
        ("scope-2", "Allow tasks write."),
    ):
        handled = _capture_connector_interrupt(
            job,
            user_id=2,
            event={
                "type": "scope_approval_required",
                "approval_id": approval_id,
                "reason": reason,
                "requested": {
                    "mode": "read_only",
                    "approval_timeout_seconds": 45,
                },
            },
        )
        assert handled is True

    public = orchestrator_mcp._job_response(job)
    assert public["status"] == "approval_required"
    assert public["approval_count"] == 2
    assert public["resume"]["approve_all_arguments"]["decision"] == "approve"

    await _submit_connector_interaction(
        job,
        decision="approve",
        answer=None,
        value=None,
        grant_id=None,
    )

    assert job.status == "approval_pending"
    assert job.interrupt is None
    assert job.interrupts == []
    for approval_id in ("scope-1", "scope-2"):
        assert (
            await store.wait(
                kind="scope",
                request_id=approval_id,
                user_id=2,
                timeout_seconds=0.01,
                ttl_seconds=45,
            )
        ) == "approve"


class _FakeMcpContext:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.collect_calls: list[dict] = []
        self.progress: list[str] = []

    async def collect(self, schema, *, title="", reason="", ui_schema=None, timeout=300):
        self.collect_calls.append(
            {
                "schema": schema,
                "title": title,
                "reason": reason,
                "ui_schema": ui_schema,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)

    async def emit_progress(self, message: str) -> None:
        self.progress.append(message)


async def test_approval_required_event_elicits_and_resolves_handoff() -> None:
    store = create_pending_actions_store(None)
    ctx = _FakeMcpContext([{"decision": "approve"}])

    handled = await _bridge_interaction_event(
        ctx,
        pending_store=store,
        user_id=2,
        event={
            "type": "approval_required",
            "approval_id": "appr-1",
            "handoff": {
                "to": "tasks-api",
                "skill": "create_task",
                "scopes": {"mode": "read_write_overlay", "allow_patterns": ["**"]},
                "timeouts": {"handoff_approval_timeout_seconds": 30},
            },
        },
    )

    assert handled is True
    assert ctx.collect_calls[0]["title"] == "Approve agent handoff"
    assert ctx.collect_calls[0]["timeout"] == 30
    assert (
        await store.wait(
            kind="handoff",
            request_id="appr-1",
            user_id=2,
            timeout_seconds=0.01,
            ttl_seconds=30,
        )
    ) == "approve"


async def test_scope_approval_event_elicits_and_resolves_scope() -> None:
    store = create_pending_actions_store(None)
    ctx = _FakeMcpContext([{"decision": "deny"}])

    handled = await _bridge_interaction_event(
        ctx,
        pending_store=store,
        user_id=2,
        event={
            "type": "scope_approval_required",
            "approval_id": "scope-1",
            "reason": "Need to write a generated report.",
            "requested": {
                "mode": "read_write_overlay",
                "write_prefixes": ["reports/"],
                "approval_timeout_seconds": 45,
            },
        },
    )

    assert handled is True
    assert ctx.collect_calls[0]["title"] == "Approve expanded agent access"
    assert (
        await store.wait(
            kind="scope",
            request_id="scope-1",
            user_id=2,
            timeout_seconds=0.01,
            ttl_seconds=45,
        )
    ) == "deny"
