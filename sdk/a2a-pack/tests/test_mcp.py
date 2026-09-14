from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from a2a_pack import (
    AccountAccess,
    A2AAgent,
    AgentEndpoint,
    ConsumerSetup,
    ConsumerSetupField,
    LLMProvisioning,
    MCP_PROTOCOL_VERSION,
    MCPServer,
    NoAuth,
    PackedFrontend,
    PlatformUserAuth,
    Resources,
    RunContext,
    skill,
    skills_to_tools,
)
import a2a_pack.mcp.http as mcp_http
import a2a_pack.mcp.server as mcp_server
import a2a_pack.consumer_setup_runtime as consumer_setup_runtime
from a2a_pack.grants import mint_grant
from a2a_pack.serve import build_app
from a2a_pack.workspace import WorkspaceMode


class _Greeter(A2AAgent):
    name = "greeter-mcp"
    description = "Says hi via MCP"

    @skill(description="Greet someone")
    async def greet(self, ctx: RunContext[NoAuth], who: str, loud: bool = False) -> str:
        out = f"hello {who}"
        return out.upper() if loud else out

    @skill(name="boom", description="fails")
    async def _boom(self, ctx: RunContext[NoAuth]) -> str:
        raise ValueError("nope")


class _AccountTrialMCPAgent(A2AAgent):
    name = "account-trial-mcp"
    account_access = AccountAccess(required=True, platform_skill_calls=2)

    @skill(description="Return success")
    async def run(self, ctx: RunContext[NoAuth]) -> dict[str, bool]:
        del ctx
        return {"ok": True}


class _SetupAgent(A2AAgent):
    name = "setup-agent"
    consumer_setup = ConsumerSetup.from_fields(
        ConsumerSetupField.secret("API_TOKEN"),
        ConsumerSetupField.config("DEFAULT_REPO", required=False),
    )

    @skill(description="Read caller setup")
    async def read_setup(self, ctx: RunContext[NoAuth]) -> dict[str, str]:
        return {
            "repo": str(ctx.consumer_config("DEFAULT_REPO", "none")),
            "token": ctx.consumer_secret("API_TOKEN"),
        }


class _RawOptionalSetupAgent(A2AAgent):
    name = "raw-optional-setup-agent"
    wants_cp_jwt = True
    consumer_setup = ConsumerSetup.from_fields(
        ConsumerSetupField.secret("BLOG_API_KEY", required=False),
        ConsumerSetupField.config("BLOG_API_BASE_URL", required=False),
    )
    endpoints = (
        AgentEndpoint(
            name="telegram",
            path="/telegram/webhook",
            skill="telegram_webhook",
            body_arg="update",
            headers_arg="headers",
            query_arg="query",
        ),
    )

    @skill(description="Handle a raw webhook and report runtime setup.")
    async def telegram_webhook(
        self,
        ctx: RunContext[NoAuth],
        update: dict[str, Any],
        headers: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "update": update,
            "headers_present": bool(headers),
            "query": query,
            "cp_jwt": ctx.cp_jwt,
            "cp_url": ctx.cp_url,
            "blog_base_url": ctx.consumer_config("BLOG_API_BASE_URL"),
            "blog_key": ctx.consumer_secret("BLOG_API_KEY"),
        }


class _PlatformLLMAgent(A2AAgent):
    name = "platform-llm-mcp"
    llm_provisioning = LLMProvisioning.PLATFORM
    resources = Resources(max_runtime_seconds=780)

    @skill(description="Read platform-routed LLM creds")
    async def read_llm(self, ctx: RunContext[NoAuth]) -> dict[str, str]:
        creds = ctx.llm
        return {
            "api_key": creds.api_key,
            "model": creds.model,
            "source": creds.source,
        }


class _MetaProbeAgent(A2AAgent):
    name = "mcp-meta-probe"
    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.CALLER_PROVIDED

    @skill(description="Inspect privileged MCP caller metadata")
    async def inspect_meta(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        try:
            bucket = getattr(ctx.workspace, "bucket", None)
        except PermissionError:
            bucket = None
        llm_creds = getattr(ctx, "_llm_creds", None)
        return {
            "cp_jwt": ctx.cp_jwt,
            "cp_url": ctx.cp_url,
            "bucket": bucket,
            "repo": ctx.consumer_config("DEFAULT_REPO"),
            "llm_api_key": getattr(llm_creds, "api_key", None),
        }


class _PlatformAuthMetaProbeAgent(A2AAgent):
    name = "platform-auth-mcp-meta-probe"
    auth_model = PlatformUserAuth
    account_access = AccountAccess(required=True, platform_skill_calls=2)

    @skill(description="Inspect verified platform caller metadata")
    async def inspect_meta(self, ctx: RunContext[PlatformUserAuth]) -> dict[str, Any]:
        return {
            "user_id": ctx.auth.user_id,
            "cp_jwt": ctx.cp_jwt,
            "cp_url": ctx.cp_url,
            "bucket": getattr(ctx.workspace, "bucket", None),
        }


class _SlowConnectorAgent(A2AAgent):
    name = "slow-connector-mcp"
    description = "Slow connector test"

    @skill(description="Wait briefly")
    async def wait(self, ctx: RunContext[NoAuth]) -> str:
        await asyncio.sleep(0.05)
        return "done"


class _QuestionConnectorAgent(A2AAgent):
    name = "question-connector-mcp"
    description = "Question connector test"

    @skill(description="Ask a question")
    async def ask_name(self, ctx: RunContext[NoAuth]) -> str:
        answer = await ctx.ask("Name?", timeout=5.0)
        return f"hello {answer}"


class _ScopeConnectorAgent(A2AAgent):
    name = "scope-connector-mcp"
    description = "Scope connector test"

    @skill(description="Request more workspace scope", allow_scope_expansion=True)
    async def expand(self, ctx: RunContext[NoAuth]) -> dict[str, str]:
        grant = await ctx.request_scope(
            reason="Need reference files",
            read=["reference/**"],
            ttl_seconds=120,
            mode="read_only",
            timeout=5.0,
            approval_timeout=5.0,
        )
        return {"grant_id": grant.grant_id}


# --- in-process MCPServer -------------------------------------------------- #


@pytest.fixture
def server() -> MCPServer:
    return MCPServer(_Greeter())


async def _call(server: MCPServer, method: str, *, params: dict | None = None, msg_id: int = 1):
    return await server.handle(
        {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
    )


async def test_initialize_echoes_protocol_and_identity(server: MCPServer) -> None:
    resp = await _call(server, "initialize", params={"protocolVersion": MCP_PROTOCOL_VERSION})
    assert resp["result"]["serverInfo"] == {"name": "greeter-mcp", "version": "0.1.0"}
    assert resp["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert resp["result"]["protocolVersion"] == MCP_PROTOCOL_VERSION


async def test_tools_list_mirrors_skills(server: MCPServer) -> None:
    resp = await _call(server, "tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"greet", "boom"}
    greet = next(t for t in resp["result"]["tools"] if t["name"] == "greet")
    assert greet["inputSchema"]["properties"]["who"]["type"] == "string"
    assert "outputSchema" in greet  # scalar return is wrapped to object


async def test_tools_call_success_returns_structured_content(server: MCPServer) -> None:
    resp = await _call(
        server,
        "tools/call",
        params={"name": "greet", "arguments": {"who": "world", "loud": True}},
    )
    result = resp["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"result": "HELLO WORLD"}
    assert result["content"][0]["text"] == '"HELLO WORLD"'


async def test_tools_call_applies_inline_consumer_setup_meta() -> None:
    resp = await MCPServer(_SetupAgent()).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "read_setup",
                "arguments": {},
                "_meta": {
                    "consumer_config": {"DEFAULT_REPO": "acme/api"},
                    "consumer_secrets": {"API_TOKEN": "tok-inline"},
                },
            },
        }
    )

    assert resp["result"]["isError"] is False
    assert resp["result"]["structuredContent"] == {
        "repo": "acme/api",
        "token": "tok-inline",
    }


async def test_tools_call_resolves_consumer_setup_from_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        *,
        cp_url: str,
        cp_jwt: str,
        agent_name: str,
    ) -> dict[str, dict[str, str]]:
        assert cp_url == "https://api.example.test"
        assert cp_jwt == "jwt-123"
        assert agent_name == "setup-agent"
        return {
            "consumer_config": {"DEFAULT_REPO": "acme/api"},
            "consumer_secrets": {"API_TOKEN": "tok-control-plane"},
        }

    monkeypatch.setattr(mcp_server, "_fetch_consumer_setup_from_cp", fake_fetch)

    resp = await MCPServer(_SetupAgent()).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "read_setup",
                "arguments": {},
                "_meta": {
                    "cp_jwt": "jwt-123",
                    "cp_url": "https://api.example.test",
                },
            },
        }
    )

    assert resp["result"]["isError"] is False
    assert resp["result"]["structuredContent"] == {
        "repo": "acme/api",
        "token": "tok-control-plane",
    }


def test_invoke_resolves_consumer_setup_from_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runtime uses its operator-configured control-plane origin; a public
    # caller cannot redirect a CP JWT to an arbitrary body-supplied URL.
    monkeypatch.setenv("A2A_CP_URL", "https://api.example.test")

    async def fake_fetch(
        *,
        cp_url: str,
        cp_jwt: str,
        agent_name: str,
    ) -> dict[str, dict[str, str]]:
        assert cp_url == "https://api.example.test"
        assert cp_jwt == "jwt-123"
        assert agent_name == "setup-agent"
        return {
            "consumer_config": {"DEFAULT_REPO": "acme/api"},
            "consumer_secrets": {"API_TOKEN": "tok-control-plane"},
        }

    monkeypatch.setattr(
        consumer_setup_runtime,
        "fetch_consumer_setup_from_cp",
        fake_fetch,
    )

    client = TestClient(build_app(_SetupAgent()))
    response = client.post(
        "/invoke/read_setup",
        json={
            "arguments": {},
            "cp_jwt": "jwt-123",
            "cp_url": "https://attacker.invalid",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "repo": "acme/api",
        "token": "tok-control-plane",
    }


def test_raw_endpoint_uses_env_cp_jwt_and_fetches_optional_consumer_setup(
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
        assert agent_name == "raw-optional-setup-agent"
        return {
            "consumer_config": {"BLOG_API_BASE_URL": "https://blog.example.test"},
            "consumer_secrets": {"BLOG_API_KEY": "blog-token"},
        }

    monkeypatch.setattr(
        consumer_setup_runtime,
        "fetch_consumer_setup_from_cp",
        fake_fetch,
    )

    client = TestClient(build_app(_RawOptionalSetupAgent()))
    response = client.post(
        "/telegram/webhook?delivery=test",
        json={"message": {"text": "/blog"}},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "update": {"message": {"text": "/blog"}},
        "headers_present": True,
        "query": {"delivery": "test"},
        "cp_jwt": "runtime-jwt",
        "cp_url": "https://api.example.test",
        "blog_base_url": "https://blog.example.test",
        "blog_key": "blog-token",
    }


async def test_tools_call_resolves_platform_llm_from_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(
        *,
        cp_url: str,
        cp_jwt: str,
        agent_name: str,
        ttl_seconds: int,
    ) -> dict:
        assert cp_url == "https://api.example.test"
        assert cp_jwt == "jwt-123"
        assert agent_name == "platform-llm-mcp"
        assert ttl_seconds == 810
        return {
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": "grant-token",
                "model": "platform-model",
                "temperature_mode": "omit",
                "extra_body": {"reasoning": {"enabled": False}},
            }
        }

    monkeypatch.setattr(mcp_server, "_fetch_platform_llm_creds_from_cp", fake_fetch)

    resp = await MCPServer(_PlatformLLMAgent()).handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "read_llm",
                "arguments": {},
                "_meta": {
                    "cp_jwt": "jwt-123",
                    "cp_url": "https://api.example.test",
                },
            },
        }
    )

    assert resp["result"]["isError"] is False
    assert resp["result"]["structuredContent"] == {
        "api_key": "grant-token",
        "model": "platform-model",
        "source": "user",
    }


async def test_tools_call_unknown_tool_is_soft_error(server: MCPServer) -> None:
    resp = await _call(
        server, "tools/call", params={"name": "nope", "arguments": {}}
    )
    assert resp["result"]["isError"] is True
    assert "unknown tool" in resp["result"]["content"][0]["text"]


async def test_tools_call_invalid_args_is_soft_error(server: MCPServer) -> None:
    resp = await _call(
        server, "tools/call", params={"name": "greet", "arguments": {"bogus": 1}}
    )
    assert resp["result"]["isError"] is True
    assert "invalid arguments" in resp["result"]["content"][0]["text"]


async def test_tool_handler_exception_surfaces_as_soft_error(server: MCPServer) -> None:
    resp = await _call(server, "tools/call", params={"name": "boom", "arguments": {}})
    assert resp["result"]["isError"] is True
    assert "nope" in resp["result"]["content"][0]["text"]


async def test_unknown_method_returns_jsonrpc_error(server: MCPServer) -> None:
    resp = await _call(server, "does/not/exist")
    assert resp["error"]["code"] == -32601


async def test_notifications_return_none(server: MCPServer) -> None:
    resp = await server.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert resp is None


# --- HTTP mount ------------------------------------------------------------ #


def test_serve_build_app_mounts_mcp_endpoint() -> None:
    client = TestClient(build_app(_Greeter()))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "greet", "arguments": {"who": "http"}},
    }
    r = client.post("/mcp", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["result"]["structuredContent"] == {"result": "hello http"}


def test_account_access_gates_mcp_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(build_app(_AccountTrialMCPAgent()))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "run", "arguments": {}},
    }

    denied = client.post("/mcp", json=body)
    assert denied.status_code == 401
    assert denied.json()["detail"]["error"] == "account_required"

    async def fake_resolve(*_args: object, **_kwargs: object) -> PlatformUserAuth:
        return PlatformUserAuth(sub="7", user_id=7)

    monkeypatch.setattr(mcp_http.PlatformUserAuthResolver, "resolve", fake_resolve)
    accepted = client.post(
        "/mcp",
        headers={"Authorization": "Bearer account-session"},
        json=body,
    )
    assert accepted.status_code == 200
    assert accepted.json()["result"]["structuredContent"] == {"ok": True}


def test_serve_build_app_mcp_notification_returns_202() -> None:
    client = TestClient(build_app(_Greeter()))
    r = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert r.status_code == 202


def test_serve_build_app_mounts_connector_mcp_endpoint() -> None:
    client = TestClient(build_app(_Greeter()))

    standard = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    connector = client.post(
        "/connector-mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )

    assert standard.status_code == 200
    assert connector.status_code == 200
    standard_names = {tool["name"] for tool in standard.json()["result"]["tools"]}
    connector_names = {tool["name"] for tool in connector.json()["result"]["tools"]}
    assert "job_result" not in standard_names
    assert {"greet", "boom", "job_result", "submit_interaction"} <= connector_names
    greet = next(
        tool for tool in connector.json()["result"]["tools"] if tool["name"] == "greet"
    )
    assert greet["outputSchema"]["required"] == ["status"]


def test_root_frontend_advertises_prefixed_agent_api(tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>app</main>", encoding="utf-8")
    frontend = PackedFrontend(dist_dir=dist, mount="/", auth="public")
    client = TestClient(build_app(_Greeter(), frontend=frontend))

    assert client.get("/").text == "<main>app</main>"
    config = client.get("/config.json").json()
    assert config["ui"]["url"] == "http://testserver/"
    assert config["endpoints"]["invoke"] == "http://testserver/_a2a/invoke"
    assert config["endpoints"]["mcp"] == "http://testserver/_a2a/mcp"
    assert (
        config["endpoints"]["agentCard"]
        == "http://testserver/_a2a/.well-known/agent-card.json"
    )

    card = client.get("/_a2a/.well-known/agent-card").json()
    assert card["mcp_endpoint"] == "/_a2a/mcp"
    assert card["connector_mcp_endpoint"] == "/_a2a/connector-mcp"
    assert card["capabilities"]["mcp"]["standard"]["path"] == "/_a2a/mcp"

    protocol_card = client.get("/_a2a/.well-known/agent-card.json").json()
    assert protocol_card["supportedInterfaces"][0]["url"] == "http://testserver/_a2a"

    mcp = client.post(
        "/_a2a/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert mcp.status_code == 200


def test_connector_mcp_inline_success_is_not_pollable() -> None:
    with TestClient(build_app(_Greeter())) as client:
        response = client.post(
            "/connector-mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "greet", "arguments": {"who": "inline"}},
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()["result"]["structuredContent"]
    assert payload == {"status": "ok", "result": "hello inline"}


def test_connector_mcp_returns_job_for_slow_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_CONNECTOR_MCP_ASYNC_AFTER_SECONDS", "0.01")
    with TestClient(build_app(_SlowConnectorAgent())) as client:
        started = client.post(
            "/connector-mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "wait", "arguments": {}},
            },
        )

        assert started.status_code == 200, started.text
        payload = started.json()["result"]["structuredContent"]
        assert payload["status"] == "running"
        job_id = payload["job_id"]

        for _ in range(20):
            polled = client.post(
                "/connector-mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "job_result", "arguments": {"job_id": job_id}},
                },
            )
            body = polled.json()["result"]["structuredContent"]
            if body["status"] == "ok":
                break
            time.sleep(0.01)

        assert body["status"] == "ok"
        assert body["result"] == "done"


def test_connector_mcp_structures_and_resumes_input_interrupt() -> None:
    with TestClient(build_app(_QuestionConnectorAgent())) as client:
        started = client.post(
            "/connector-mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ask_name", "arguments": {}},
            },
        )

        assert started.status_code == 200, started.text
        payload = started.json()["result"]["structuredContent"]
        assert payload["status"] == "input_required"
        assert payload["kind"] == "question"

        resumed = client.post(
            "/connector-mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "submit_interaction",
                    "arguments": {
                        "job_id": payload["job_id"],
                        "approval_token": payload["approval_token"],
                        "answer": "Ada",
                    },
                },
            },
        )

        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["result"]["structuredContent"]["status"] == "running"
        for _ in range(20):
            polled = client.post(
                "/connector-mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "job_result",
                        "arguments": {"job_id": payload["job_id"]},
                    },
                },
            )
            body = polled.json()["result"]["structuredContent"]
            if body["status"] == "ok":
                break
            time.sleep(0.01)
        assert body["status"] == "ok"
        assert body["result"] == "hello Ada"


def test_connector_mcp_structures_and_resumes_scope_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://api.example.test")

    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "cp-token"
        assert cp_url == "https://api.example.test"
        return 2

    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    with TestClient(build_app(_ScopeConnectorAgent())) as client:
        started = client.post(
            "/connector-mcp",
            headers={"Authorization": "Bearer cp-token"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "expand", "arguments": {}},
            },
        )

        assert started.status_code == 200, started.text
        payload = started.json()["result"]["structuredContent"]
        assert payload["status"] == "approval_required"
        assert payload["kind"] == "scope"
        assert payload["requested_scope"]["read_patterns"] == ["reference/**"]

        grant, grant_token = mint_grant(
            issuer="cp",
            audience="scope-connector-mcp",
            bucket="user-2-files",
            mode=WorkspaceMode.READ_ONLY,
            allow_patterns=("reference/**",),
            ttl_seconds=120,
        )
        resumed = client.post(
            "/connector-mcp",
            headers={"Authorization": "Bearer cp-token"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "submit_interaction",
                    "arguments": {
                        "job_id": payload["job_id"],
                        "approval_token": payload["approval_token"],
                        "decision": "approve",
                        "grant_token": grant_token,
                    },
                },
            },
        )

        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["result"]["structuredContent"]["status"] == "approval_pending"
        for _ in range(20):
            polled = client.post(
                "/connector-mcp",
                headers={"Authorization": "Bearer cp-token"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "job_result",
                        "arguments": {"job_id": payload["job_id"]},
                    },
                },
            )
            body = polled.json()["result"]["structuredContent"]
            if body["status"] == "ok":
                break
            time.sleep(0.01)
        assert body["status"] == "ok"
        assert body["result"] == {"grant_id": grant.grant_id}


def test_mcp_http_strips_untrusted_privileged_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.delenv("A2A_CP_URL", raising=False)
    monkeypatch.delenv("A2A_MINIO_ENDPOINT", raising=False)
    monkeypatch.setenv("A2A_AGENT_PUBLIC", "true")
    client = TestClient(build_app(_MetaProbeAgent()))

    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "inspect_meta",
                "arguments": {},
                "_meta": {
                    "progressToken": "progress-1",
                    "cp_jwt": "forged-jwt",
                    "cp_url": "https://evil.example.test",
                    "bucket": "victim-files",
                    "consumer_config": {"DEFAULT_REPO": "victim/repo"},
                    "consumer_secrets": {"API_TOKEN": "victim-token"},
                    "llm_creds": {
                        "base_url": "https://llm.example.test/v1",
                        "api_key": "forged-llm-key",
                        "model": "forged-model",
                    },
                },
            },
        },
    )

    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "cp_jwt": None,
        "cp_url": None,
        "bucket": None,
        "repo": None,
        "llm_api_key": None,
    }


def test_mcp_http_injects_verified_cp_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "real-jwt"
        assert cp_url == "https://cp.example.test"
        return 42

    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.delenv("A2A_MINIO_ENDPOINT", raising=False)
    monkeypatch.setenv("A2A_AGENT_PUBLIC", "true")
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    client = TestClient(build_app(_MetaProbeAgent()))

    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer real-jwt"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "inspect_meta",
                "arguments": {},
                "_meta": {
                    "progressToken": "progress-1",
                    "cp_jwt": "forged-jwt",
                    "cp_url": "https://evil.example.test",
                    "bucket": "victim-files",
                },
            },
        },
    )

    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "cp_jwt": "real-jwt",
        "cp_url": "https://cp.example.test",
        "bucket": "user-42-files",
        "repo": None,
        "llm_api_key": None,
    }


def test_mcp_http_injects_cp_meta_for_platform_auth_account_trial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve(*_args: object, **_kwargs: object) -> PlatformUserAuth:
        return PlatformUserAuth(sub="42", user_id=42, email="user@example.test")

    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.delenv("A2A_MINIO_ENDPOINT", raising=False)
    monkeypatch.setenv("A2A_AGENT_PUBLIC", "true")
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setattr(
        mcp_http.PlatformUserAuthResolver,
        "resolve",
        fake_resolve,
    )
    client = TestClient(build_app(_PlatformAuthMetaProbeAgent()))

    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer real-jwt"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "inspect_meta", "arguments": {}},
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "user_id": 42,
        "cp_jwt": "real-jwt",
        "cp_url": "https://cp.example.test",
        "bucket": "user-42-files",
    }


def test_mcp_http_requires_agent_read_scope_for_keycloak_tools_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.setenv("A2A_AGENT_PUBLIC", "true")
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setattr(mcp_http._oauth, "is_keycloak_token", lambda token: True)
    monkeypatch.setattr(
        mcp_http._oauth,
        "validate_keycloak_token",
        lambda token, *, audience=None: {"scope": "mcp:invoke"},
    )

    client = TestClient(build_app(_Greeter()))
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer keycloak-token"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )

    assert response.status_code == 403
    assert "agent:read" in response.text


def test_mcp_http_requires_mcp_invoke_scope_for_keycloak_tools_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.setenv("A2A_AGENT_PUBLIC", "true")
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setattr(mcp_http._oauth, "is_keycloak_token", lambda token: True)
    monkeypatch.setattr(
        mcp_http._oauth,
        "validate_keycloak_token",
        lambda token, *, audience=None: {"scope": "agent:read"},
    )

    client = TestClient(build_app(_Greeter()))
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer keycloak-token"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "greet", "arguments": {"who": "scope"}},
        },
    )

    assert response.status_code == 403
    assert "mcp:invoke" in response.text


def test_mcp_http_allows_keycloak_token_with_required_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_verify_cp_jwt(token: str, cp_url: str) -> int | None:
        assert token == "keycloak-token"
        assert cp_url == "https://cp.example.test"
        return 42

    monkeypatch.delenv("A2A_API_KEY", raising=False)
    monkeypatch.setenv("A2A_AGENT_PUBLIC", "true")
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setattr(mcp_http, "_verify_cp_jwt", fake_verify_cp_jwt)
    monkeypatch.setattr(mcp_http._oauth, "is_keycloak_token", lambda token: True)
    monkeypatch.setattr(
        mcp_http._oauth,
        "validate_keycloak_token",
        lambda token, *, audience=None: {"scope": "mcp:invoke agent:read"},
    )

    client = TestClient(build_app(_Greeter()))
    response = client.post(
        "/mcp",
        headers={"Authorization": "Bearer keycloak-token"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "greet", "arguments": {"who": "scope"}},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"] == {"result": "hello scope"}


async def test_verify_cp_jwt_caches_cp_me_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    calls = 0

    class _Response:
        status_code = 200

        def json(self) -> dict[str, int]:
            return {"id": 7}

    class _AsyncClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == 5.0

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _Response:
            nonlocal calls
            calls += 1
            assert url == "https://cp.example.test/v1/me"
            assert headers["authorization"].startswith("bearer ")
            return _Response()

    monkeypatch.delenv("A2A_CP_ME_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    mcp_http._ME_CACHE.clear()
    try:
        token = jwt.encode(
            {"sub": "u", "iat": int(time.time()), "exp": int(time.time()) + 300},
            "test-secret-key-that-is-long-enough",
            algorithm="HS256",
        )
        assert await mcp_http._verify_cp_jwt(token, "https://cp.example.test") == 7
        assert await mcp_http._verify_cp_jwt(token, "https://cp.example.test") == 7
        assert calls == 1
    finally:
        mcp_http._ME_CACHE.clear()


async def test_verify_cp_jwt_cache_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls = 0

    class _Response:
        status_code = 200

        def json(self) -> dict[str, int]:
            return {"id": 7}

    class _AsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _Response:
            nonlocal calls
            calls += 1
            return _Response()

    monkeypatch.setenv("A2A_CP_ME_CACHE_TTL_SECONDS", "0")
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    mcp_http._ME_CACHE.clear()
    try:
        assert await mcp_http._verify_cp_jwt("cp-token", "https://cp.example.test") == 7
        assert await mcp_http._verify_cp_jwt("cp-token", "https://cp.example.test") == 7
        assert calls == 2
    finally:
        mcp_http._ME_CACHE.clear()


async def test_verify_cp_jwt_cache_does_not_outlive_jwt_exp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls = 0

    class _Response:
        status_code = 200

        def json(self) -> dict[str, int]:
            return {"id": 7}

    class _AsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _Response:
            nonlocal calls
            calls += 1
            return _Response()

    monkeypatch.delenv("A2A_CP_ME_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    mcp_http._ME_CACHE.clear()
    try:
        now = int(time.time())
        token = jwt.encode(
            {"sub": "u", "iat": now - 30, "exp": now - 1},
            "test-secret-key-that-is-long-enough",
            algorithm="HS256",
        )
        assert await mcp_http._verify_cp_jwt(token, "https://cp.example.test") == 7
        assert await mcp_http._verify_cp_jwt(token, "https://cp.example.test") == 7
        assert calls == 2
        assert mcp_http._ME_CACHE == {}
    finally:
        mcp_http._ME_CACHE.clear()


def test_skills_to_tools_uses_skill_metadata() -> None:
    tools = skills_to_tools(_Greeter())
    greet = next(t for t in tools if t["name"] == "greet")
    assert greet["description"] == "Greet someone"
    assert "who" in greet["inputSchema"]["required"]


def test_agent_card_advertises_mcp_endpoint() -> None:
    card = _Greeter().card()
    assert card.mcp_endpoint == "/mcp"
    assert card.connector_mcp_endpoint == "/connector-mcp"
    assert card.mcp_endpoints["connector"]["poll_tool"] == "job_result"
