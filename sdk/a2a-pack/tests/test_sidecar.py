from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2a_pack import (
    AccountAccess,
    AgentDsl,
    AgentDslAuth,
    AgentDslEntrypoint,
    AgentDslSkill,
    AgentEndpoint,
    ConsumerSetup,
    ConsumerSetupField,
    NoAuth,
    PlatformUserAuth,
    SidecarWorkerRequest,
    SidecarWorkerResponse,
    build_sidecar_app,
)
import a2a_pack.sidecar as sidecar_module
import a2a_pack.consumer_setup_runtime as consumer_setup_runtime
from a2a_pack.frontend import (
    PackedFrontend,
    export_frontend_env,
    packed_frontend_from_env,
    start_frontend_process,
)


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[SidecarWorkerRequest] = []
        self.callbacks: list[tuple[str, dict[str, Any]]] = []

    async def call(self, skill: AgentDslSkill, request: SidecarWorkerRequest) -> Any:
        self.calls.append(request)
        if skill.name == "sum":
            return SidecarWorkerResponse(
                result=request.arguments["left"] + request.arguments["right"]
            ).result
        return SidecarWorkerResponse(result={"echo": request.arguments}).result

    async def callback(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.callbacks.append((path, payload))
        return {"ok": True}


class StreamingWorker(FakeWorker):
    async def stream(self, skill: AgentDslSkill, request: SidecarWorkerRequest):
        self.calls.append(request)
        yield b'data: {"type":"started"}\n\n'
        yield (
            b'data: {"type":"event","kind":"render_progress",'
            b'"payload":{"stage":"bundle"}}\n\n'
        )
        yield b'data: {"type":"result","result":5,"events":[],"artifacts":[]}\n\n'
        yield b"data: [DONE]\n\n"


class FullResponseWorker(FakeWorker):
    async def call_full(
        self,
        skill: AgentDslSkill,
        request: SidecarWorkerRequest,
    ) -> SidecarWorkerResponse:
        self.calls.append(request)
        return SidecarWorkerResponse(
            result={"ok": True},
            events=[{"kind": "progress", "payload": {"message": "done"}}],
            artifacts=[{
                "name": "result.txt",
                "path": "outputs/task-1/result.txt",
                "uri": "s3://user-2-files/outputs/task-1/result.txt",
            }],
        )


def _dsl() -> AgentDsl:
    return AgentDsl(
        language="typescript",
        name="math-agent",
        description="Math sidecar fixture",
        version="0.1.0",
        entrypoint=AgentDslEntrypoint(command=("node", "dist/worker.js")),
        auth=AgentDslAuth(
            model="a2a_pack.auth.NoAuth",
            strategy="public",
            principal_schema=NoAuth.model_json_schema(),
            required=False,
        ),
        skills=(
            AgentDslSkill(
                name="sum",
                description="Add two integers",
                handler="sumHandler",
                input_schema={
                    "type": "object",
                    "properties": {
                        "left": {"type": "integer"},
                        "right": {"type": "integer"},
                    },
                    "required": ["left", "right"],
                    "additionalProperties": False,
                },
                output_schema={"type": "integer"},
            ),
            AgentDslSkill(
                name="echo",
                description="Echo payload",
                handler="echoHandler",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"echo": {"type": "object"}},
                    "required": ["echo"],
                },
            ),
        ),
    )


def _consumer_setup_dsl() -> AgentDsl:
    return _dsl().model_copy(
        update={
            "consumer_setup": ConsumerSetup.from_fields(
                ConsumerSetupField.secret("API_TOKEN"),
                ConsumerSetupField.config("PLAN", required=False),
            ),
        }
    )


def _account_access_dsl() -> AgentDsl:
    base = _dsl()
    return base.model_copy(
        update={
            "runtime": base.runtime.model_copy(
                update={
                    "account_access": AccountAccess(
                        required=True,
                        platform_skill_calls=2,
                    )
                }
            )
        }
    )


def _endpoint_dsl() -> AgentDsl:
    base = _consumer_setup_dsl()
    webhook_skill = AgentDslSkill(
        name="telegram_webhook",
        description="Handle Telegram webhook update",
        handler="telegramWebhook",
        input_schema={
            "type": "object",
            "properties": {
                "update": {"type": "object"},
                "headers": {"type": "object"},
                "query": {"type": "object"},
            },
            "required": ["update"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    runtime = base.runtime.model_copy(
        update={
            "endpoints": (
                AgentEndpoint(
                    name="telegram",
                    path="/telegram/webhook",
                    skill="telegram_webhook",
                    body_arg="update",
                    headers_arg="headers",
                    query_arg="query",
                ),
            )
        }
    )
    return base.model_copy(
        update={
            "skills": (*base.skills, webhook_skill),
            "runtime": runtime,
        }
    )


def _optional_setup_endpoint_dsl() -> AgentDsl:
    base = _dsl().model_copy(
        update={
            "consumer_setup": ConsumerSetup.from_fields(
                ConsumerSetupField.secret("BLOG_API_KEY", required=False),
                ConsumerSetupField.config("BLOG_API_BASE_URL", required=False),
            ),
        }
    )
    webhook_skill = AgentDslSkill(
        name="telegram_webhook",
        description="Handle Telegram webhook update",
        handler="telegramWebhook",
        input_schema={
            "type": "object",
            "properties": {
                "update": {"type": "object"},
                "headers": {"type": "object"},
                "query": {"type": "object"},
            },
            "required": ["update"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    runtime = base.runtime.model_copy(
        update={
            "wants_cp_jwt": True,
            "endpoints": (
                AgentEndpoint(
                    name="telegram",
                    path="/telegram/webhook",
                    skill="telegram_webhook",
                    body_arg="update",
                    headers_arg="headers",
                    query_arg="query",
                ),
            ),
        }
    )
    return base.model_copy(
        update={
            "skills": (*base.skills, webhook_skill),
            "runtime": runtime,
        }
    )


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_server_frontend_process_overrides_agent_port(monkeypatch, tmp_path: Path) -> None:
    app = FastAPI()
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode: int | None = None

        async def wait(self) -> int:
            while self.returncode is None:
                await anyio.sleep(0)
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = 1

    async def fake_create_subprocess_shell(command: str, *, cwd: str | None, env: dict[str, str]):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        return FakeProcess()

    import asyncio

    monkeypatch.setenv("PORT", "8000")
    monkeypatch.setenv("HOSTNAME", "agent-pod")
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    frontend = PackedFrontend(
        kind="server-rendered",
        start="node server.js",
        workdir=tmp_path,
        port=3000,
    )
    start_frontend_process(app, frontend)

    for handler in app.router.on_startup:
        await handler()

    assert captured["command"] == "node server.js"
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["PORT"] == "3000"
    assert captured["env"]["HOSTNAME"] == "127.0.0.1"

    for handler in app.router.on_shutdown:
        await handler()


def test_sidecar_account_access_gates_invoke_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_account_access_dsl(), worker=worker))
    invoke_body = {"arguments": {"left": 2, "right": 3}}
    mcp_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "sum", "arguments": {"left": 2, "right": 3}},
    }

    assert client.post("/invoke/sum", json=invoke_body).status_code == 401
    assert client.post("/mcp", json=mcp_body).status_code == 401

    async def fake_resolve(*_args: object, **_kwargs: object) -> PlatformUserAuth:
        return PlatformUserAuth(sub="7", user_id=7)

    monkeypatch.setattr(
        sidecar_module.PlatformUserAuthResolver,
        "resolve",
        fake_resolve,
    )
    accepted_invoke = client.post(
        "/invoke/sum",
        headers={"Authorization": "Bearer account-session"},
        json=invoke_body,
    )
    accepted_mcp = client.post(
        "/mcp",
        headers={"Authorization": "Bearer account-session"},
        json=mcp_body,
    )
    assert accepted_invoke.status_code == 200
    assert accepted_invoke.json()["result"] == 5
    assert accepted_mcp.status_code == 200
    assert accepted_mcp.json()["result"]["structuredContent"] == {"result": 5}


def test_sidecar_serves_health_card_and_skills() -> None:
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))

    assert client.get("/healthz").json() == {
        "ok": True,
        "agent": "math-agent",
        "version": "0.1.0",
        "sidecar": True,
    }
    card = client.get("/.well-known/agent-card").json()
    assert card["name"] == "math-agent"
    assert [skill["name"] for skill in card["skills"]] == ["sum", "echo"]
    assert card["capabilities"]["a2a_pack"]["language"] == "typescript"

    skills = client.get("/.well-known/a2a-skills.json").json()
    assert skills["agent"] == {"name": "math-agent", "version": "0.1.0"}
    assert skills["skills"][0]["input_schema"]["required"] == ["left", "right"]
    assert skills["invokeUrl"].endswith("/invoke")

    openapi = client.get("/.well-known/openapi.json").json()
    assert openapi["info"]["title"] == "math-agent Skills API"
    assert openapi["paths"]["/invoke/sum"]["post"]["operationId"] == "invokeSum"
    assert openapi["paths"]["/invoke/sum"]["post"]["requestBody"]


def test_sidecar_worker_protocol_schema_includes_runtime_context() -> None:
    request_schema = SidecarWorkerRequest.model_json_schema()
    props = request_schema["properties"]

    assert request_schema["required"] == ["agent", "skill", "handler"]
    assert props["arguments"]["type"] == "object"
    assert props["task_id"]["anyOf"][0]["type"] == "string"
    assert props["caller"]["anyOf"][0]["type"] == "object"
    assert props["grant_ids"]["anyOf"][0]["items"]["type"] == "string"
    assert props["random_seed"]["anyOf"][0]["type"] == "string"
    assert props["grant"]["anyOf"][0]["type"] == "string"
    assert props["llm_creds"]["anyOf"][0]["type"] == "object"
    assert props["composition"]["anyOf"][0]["type"] == "object"
    assert props["consumer_config"]["anyOf"][0]["type"] == "object"
    assert props["consumer_secrets"]["anyOf"][0]["additionalProperties"]["type"] == "string"
    assert props["cp_jwt"]["anyOf"][0]["type"] == "string"
    assert props["cp_url"]["anyOf"][0]["type"] == "string"
    assert props["auth"]["anyOf"][0]["type"] == "object"
    assert props["scope_expansion_allowed"]["anyOf"][0]["type"] == "boolean"

    response_schema = SidecarWorkerResponse.model_json_schema()
    assert response_schema["required"] == ["result"]
    assert response_schema["properties"]["events"]["type"] == "array"
    assert response_schema["properties"]["artifacts"]["type"] == "array"


def test_sidecar_invoke_validates_and_forwards_to_worker() -> None:
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))

    response = client.post(
        "/invoke/sum",
        json={
            "arguments": {"left": 2, "right": 3},
            "task_id": "task-1",
            "caller": {"agent": "main-agent"},
            "grant_ids": ["grant-1"],
            "random_seed": "seed-1",
            "grant": "grant-token",
            "consumer_config": {"PLAN": "pro"},
            "auth": {"sub": "caller"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"result": 5}
    assert len(worker.calls) == 1
    call = worker.calls[0]
    assert call.agent == "math-agent"
    assert call.skill == "sum"
    assert call.handler == "sumHandler"
    assert call.arguments == {"left": 2, "right": 3}
    assert call.task_id == "task-1"
    assert call.caller == {"agent": "main-agent"}
    assert call.grant_ids == ["grant-1"]
    assert call.random_seed == "seed-1"
    assert call.grant == "grant-token"
    assert call.consumer_config == {"PLAN": "pro"}
    assert call.auth == {"sub": "caller"}
    assert call.scope_expansion_allowed is False


def test_sidecar_invoke_resolves_consumer_setup_from_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://api.example.test")
    async def fake_fetch(
        *,
        cp_url: str,
        cp_jwt: str,
        agent_name: str,
    ) -> dict[str, dict[str, str]]:
        assert cp_url == "https://api.example.test"
        assert cp_jwt == "jwt-123"
        assert agent_name == "math-agent"
        return {
            "consumer_config": {"PLAN": "pro"},
            "consumer_secrets": {"API_TOKEN": "tok-control-plane"},
        }

    monkeypatch.setattr(
        consumer_setup_runtime,
        "fetch_consumer_setup_from_cp",
        fake_fetch,
    )
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_consumer_setup_dsl(), worker=worker))

    response = client.post(
        "/invoke/sum",
        json={
            "arguments": {"left": 2, "right": 3},
            "cp_jwt": "jwt-123",
            "cp_url": "http://169.254.169.254",
        },
    )

    assert response.status_code == 200, response.text
    call = worker.calls[0]
    assert call.consumer_config == {"PLAN": "pro"}
    assert call.consumer_secrets == {"API_TOKEN": "tok-control-plane"}


def test_sidecar_raw_endpoint_adapts_request_and_env_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_TOKEN", "tok-env")
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_endpoint_dsl(), worker=worker))

    response = client.post(
        "/telegram/webhook?delivery=test",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={"message": {"text": "/status"}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["echo"]["update"] == {"message": {"text": "/status"}}
    call = worker.calls[0]
    assert call.skill == "telegram_webhook"
    assert call.handler == "telegramWebhook"
    assert call.task_id == "http-endpoint:telegram"
    assert call.arguments["query"] == {"delivery": "test"}
    assert call.arguments["headers"]["x-telegram-bot-api-secret-token"] == "secret"
    assert call.consumer_secrets == {"API_TOKEN": "tok-env"}


def test_sidecar_raw_endpoint_uses_env_cp_jwt_and_fetches_optional_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://api.example.test")
    monkeypatch.setenv("A2A_CP_JWT", "runtime-jwt")

    async def fake_fetch(
        *,
        cp_url: str,
        cp_jwt: str,
        agent_name: str,
    ) -> dict[str, dict[str, str]]:
        assert cp_url == "https://api.example.test"
        assert cp_jwt == "runtime-jwt"
        assert agent_name == "math-agent"
        return {
            "consumer_config": {"BLOG_API_BASE_URL": "https://blog.example.test"},
            "consumer_secrets": {"BLOG_API_KEY": "blog-token"},
        }

    monkeypatch.setattr(
        consumer_setup_runtime,
        "fetch_consumer_setup_from_cp",
        fake_fetch,
    )

    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_optional_setup_endpoint_dsl(), worker=worker))

    response = client.post(
        "/telegram/webhook?delivery=test",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={"message": {"text": "/blog"}},
    )

    assert response.status_code == 200, response.text
    call = worker.calls[0]
    assert call.consumer_config == {"BLOG_API_BASE_URL": "https://blog.example.test"}
    assert call.consumer_secrets == {"BLOG_API_KEY": "blog-token"}
    assert call.cp_jwt == "runtime-jwt"
    assert call.cp_url == "https://api.example.test"


def test_sidecar_forwards_waiting_callback_routes_to_worker() -> None:
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))

    assert client.post("/answers/q_1", json={"answer": "yes"}).json() == {"ok": True}
    assert client.post("/input-requests/ir_1", json={"value": {"topic": "A2A"}}).json() == {"ok": True}
    assert client.post("/scope-grants/sr_1", json={"grant": "grant-token"}).json() == {"ok": True}
    assert client.post("/scope-denials/sr_2", json={"reason": "nope"}).json() == {"ok": True}

    assert worker.callbacks == [
        ("/_a2a/answers/q_1", {"answer": "yes"}),
        ("/_a2a/input-requests/ir_1", {"value": {"topic": "A2A"}}),
        ("/_a2a/scope-grants/sr_1", {"grant": "grant-token"}),
        ("/_a2a/scope-denials/sr_2", {"reason": "nope"}),
    ]


def test_sidecar_invoke_preserves_worker_events_and_artifacts() -> None:
    worker = FullResponseWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))

    response = client.post("/invoke/echo", json={"arguments": {"text": "ok"}})

    assert response.status_code == 200
    assert response.json() == {
        "result": {"ok": True},
        "events": [{"kind": "progress", "payload": {"message": "done"}}],
        "artifacts": [{
            "name": "result.txt",
            "path": "outputs/task-1/result.txt",
            "uri": "s3://user-2-files/outputs/task-1/result.txt",
        }],
    }
    assert len(worker.calls) == 1


def test_sidecar_invoke_streams_worker_events() -> None:
    worker = StreamingWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))

    with client.stream(
        "POST",
        "/invoke/sum",
        headers={"accept": "text/event-stream"},
        json={"arguments": {"left": 2, "right": 3}},
        ) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            frames = _sse_frames(response.read().decode())

    assert frames == [
        {"type": "started"},
        {"type": "event", "kind": "render_progress", "payload": {"stage": "bundle"}},
        {"type": "result", "result": 5, "events": [], "artifacts": []},
        "[DONE]",
    ]
    assert len(worker.calls) == 1
    assert worker.calls[0].handler == "sumHandler"


def test_sidecar_stream_replays_duplicate_invocation_without_rerunning_worker() -> None:
    worker = StreamingWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))
    payload = {"arguments": {"left": 2, "right": 3}}
    headers = {"accept": "text/event-stream"}

    first = client.post("/invoke/sum", headers=headers, json=payload)
    second = client.post("/invoke/sum", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert _sse_frames(first.text) == _sse_frames(second.text)
    assert len(worker.calls) == 1


def test_sidecar_invoke_rejects_unknown_missing_and_extra_arguments() -> None:
    client = TestClient(build_sidecar_app(_dsl(), worker=FakeWorker()))

    assert client.post("/invoke/nope", json={"arguments": {}}).status_code == 404

    missing = client.post("/invoke/sum", json={"arguments": {"left": 2}})
    assert missing.status_code == 422
    assert "missing required arguments" in missing.json()["detail"]

    extra = client.post(
        "/invoke/sum",
        json={"arguments": {"left": 2, "right": 3, "other": 4}},
    )
    assert extra.status_code == 422
    assert "unknown arguments" in extra.json()["detail"]


def test_sidecar_mcp_lists_and_calls_tools() -> None:
    worker = FakeWorker()
    client = TestClient(build_sidecar_app(_dsl(), worker=worker))

    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    ).json()
    assert initialized["result"]["serverInfo"] == {
        "name": "math-agent",
        "version": "0.1.0",
    }

    listed = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ).json()
    assert [tool["name"] for tool in listed["result"]["tools"]] == ["sum", "echo"]
    assert listed["result"]["tools"][0]["outputSchema"] == {
        "type": "object",
        "properties": {"result": {"type": "integer"}},
        "required": ["result"],
    }

    called = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "sum", "arguments": {"left": 7, "right": 8}},
        },
    ).json()
    assert called["result"]["structuredContent"] == {"result": 15}
    assert called["result"]["isError"] is False
    assert worker.calls[-1].handler == "sumHandler"


def test_sidecar_serves_packed_frontend_from_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("<main>next app</main>", encoding="utf-8")
    (dist / "asset.txt").write_text("asset ok", encoding="utf-8")
    monkeypatch.setenv("A2A_FRONTEND_DIST", str(dist))
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/app")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "public")

    client = TestClient(build_sidecar_app(_dsl(), worker=FakeWorker()))

    redirect = client.get("/app", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/app/"
    assert client.get("/app/").text == "<main>next app</main>"
    assert client.get("/app/asset.txt").text == "asset ok"
    config = client.get("/app/config.json").json()
    assert config["agent"]["name"] == "math-agent"
    assert config["ui"]["url"].endswith("/app")
    assert config["auth"]["invokeRequiresSession"] is False
    assert config["endpoints"]["invoke"].endswith("/invoke")
    assert [skill["name"] for skill in config["skills"]] == ["sum", "echo"]
    client_js = client.get("/app/a2a-client.js").text
    assert "createA2AClient" in client_js
    assert "unwrapInvokeResponse" in client_js
    assert "callSkillEnvelope" in client_js


def test_export_frontend_env_clears_server_rendered_values(
    tmp_path: Path,
) -> None:
    try:
        server = PackedFrontend(
            mount="/",
            kind="server-rendered",
            framework="nextjs",
            proxy_url="http://127.0.0.1:3000",
            start="node server.js",
            workdir=tmp_path,
            port=3000,
        )
        export_frontend_env(server)

        dist = tmp_path / "dist"
        dist.mkdir()
        export_frontend_env(PackedFrontend(dist_dir=dist, mount="/app", auth="public"))

        resolved = packed_frontend_from_env()
        assert resolved is not None
        assert resolved.is_static
        assert os.environ["A2A_FRONTEND_DIST"] == str(dist)
        assert "A2A_FRONTEND_PROXY_URL" not in os.environ
        assert "A2A_FRONTEND_START" not in os.environ
        assert "A2A_FRONTEND_WORKDIR" not in os.environ
    finally:
        export_frontend_env(None)


def test_sidecar_root_frontend_uses_prefixed_agent_api(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("<main>root app</main>", encoding="utf-8")
    monkeypatch.setenv("A2A_FRONTEND_DIST", str(dist))
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "public")

    client = TestClient(build_sidecar_app(_dsl(), worker=FakeWorker()))

    assert client.get("/").text == "<main>root app</main>"
    config = client.get("/config.json").json()
    assert config["endpoints"]["invoke"].endswith("/_a2a/invoke")
    assert config["endpoints"]["mcp"].endswith("/_a2a/mcp")
    assert config["endpoints"]["agentCard"].endswith(
        "/_a2a/.well-known/agent-card.json"
    )
    card = client.get("/_a2a/.well-known/agent-card").json()
    assert card["mcp_endpoint"] == "/_a2a/mcp"

    invoke = client.post(
        "/_a2a/invoke/sum",
        json={"arguments": {"left": 1, "right": 2}},
    )
    assert invoke.status_code == 200
    assert invoke.json()["result"] == 3
    mcp = client.post(
        "/_a2a/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert mcp.status_code == 200


def test_sidecar_platform_frontend_auth_blocks_static_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("<main>private app</main>", encoding="utf-8")
    monkeypatch.setenv("A2A_FRONTEND_DIST", str(dist))
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/app")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "platform")
    monkeypatch.delenv("A2A_LOCAL_DEV", raising=False)
    monkeypatch.delenv("A2A_TRUST_PLATFORM_HEADERS", raising=False)

    client = TestClient(build_sidecar_app(_dsl(), worker=FakeWorker()))

    response = client.get("/app/")
    assert response.status_code == 401
    assert response.json() == {"detail": "platform session required"}
    assert client.get("/app/config.json").status_code == 401
    assert client.get("/app/a2a-client.js").status_code == 401


def test_sidecar_platform_frontend_auth_allows_trusted_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("<main>private app</main>", encoding="utf-8")
    monkeypatch.setenv("A2A_FRONTEND_DIST", str(dist))
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/app")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "platform")
    monkeypatch.setenv("A2A_TRUST_PLATFORM_HEADERS", "1")
    monkeypatch.delenv("A2A_LOCAL_DEV", raising=False)

    client = TestClient(build_sidecar_app(_dsl(), worker=FakeWorker()))
    headers = {
        "x-a2a-user-id": "42",
        "x-a2a-user-email": "user@example.com",
        "x-a2a-org": "acme",
        "x-a2a-scopes": "agent:invoke files:read",
    }

    assert client.get("/app/", headers=headers).text == "<main>private app</main>"
    config = client.get("/app/config.json", headers=headers).json()
    assert config["ui"]["requiresAuth"] is True
    assert config["ui"]["auth"]["requiresSession"] is True
    session = client.get("/auth/session", headers=headers).json()
    assert session["authenticated"] is True
    assert session["user"]["email"] == "user@example.com"


def test_sidecar_platform_auth_gates_server_rendered_frontend(monkeypatch) -> None:
    monkeypatch.setenv("A2A_FRONTEND_KIND", "server-rendered")
    monkeypatch.setenv("A2A_FRONTEND_FRAMEWORK", "nextjs")
    monkeypatch.setenv("A2A_FRONTEND_PROXY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/app")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "platform")
    monkeypatch.setenv("A2A_TRUST_PLATFORM_HEADERS", "1")
    monkeypatch.delenv("A2A_LOCAL_DEV", raising=False)

    client = TestClient(build_sidecar_app(_dsl(), worker=FakeWorker()))

    denied = client.get("/app/")
    assert denied.status_code == 401
    assert denied.json()["detail"] == "platform session required"

    config = client.get(
        "/app/config.json",
        headers={"x-a2a-user-email": "user@example.com"},
    )
    assert config.status_code == 200
    body = config.json()
    assert body["ui"]["type"] == "server-rendered"
    assert body["ui"]["framework"] == "nextjs"
    assert body["ui"]["requiresAuth"] is True


def _sse_frames(text: str) -> list[Any]:
    frames: list[Any] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line.removeprefix("data: ")
        frames.append(raw if raw == "[DONE]" else json.loads(raw))
    return frames
