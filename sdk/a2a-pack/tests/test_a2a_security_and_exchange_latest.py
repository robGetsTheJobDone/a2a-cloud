from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from a2a_pack import A2AAgent, APIKeyAuth, RunContext, WorkspaceMode, skill
from a2a_pack.context import _ScopeRegistry
from a2a_pack.grants import mint_grant
from a2a_pack.serve import build_app


class _SpecWorkflowAgent(A2AAgent):
    name = "spec-workflow-agent"
    description = "A2A latest spec workflow fixture"


@pytest.fixture(autouse=True)
def _protocol_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "test-a2a-token")
    for name in (
        "A2A_ENABLE_PUSH_NOTIFICATIONS",
        "A2A_ENABLE_STREAMING",
        "A2A_ENABLE_EXTENDED_AGENT_CARD",
        "A2A_EXTENDED_AGENT_CARD_CONFIGURED",
        "A2A_REQUIRED_EXTENSIONS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(_SpecWorkflowAgent()))


def _auth(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer test-a2a-token"}
    if extra:
        headers.update(extra)
    return headers


def _rpc(client: TestClient, method: str, params: dict | None = None) -> dict:
    response = client.post(
        "/",
        headers=_auth(),
        json={"jsonrpc": "2.0", "id": "test-id", "method": method, "params": params or {}},
    )
    assert response.status_code == 200
    return response.json()


def _message(message_id: str, text: str, **extra: object) -> dict:
    message = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    message.update(extra)
    return message


def test_in_task_authorization_sets_auth_required_and_accepts_followup(client: TestClient) -> None:
    created = _rpc(
        client,
        "SendMessage",
        {"message": _message("auth-required-1", "Please read my Google Drive files.")},
    )
    task = created["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    assert task["status"]["message"]["role"] == "ROLE_AGENT"
    assert "authorization" in task["status"]["message"]["parts"][0]["text"].lower()
    assert task["authChallenge"]["schemes"][0]["scheme"] == "oauth2"

    follow_up = _rpc(
        client,
        "SendMessage",
        {"message": _message("auth-followup-1", "I will approve this out of band.", taskId=task["id"])},
    )

    assert follow_up["result"]["task"]["id"] == task["id"]
    assert follow_up["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING"
    assert len(follow_up["result"]["task"]["history"]) == 2


def test_file_upload_part_accepts_latest_raw_filename_media_type(client: TestClient) -> None:
    payload = {
        "message": {
            "messageId": "file-upload-1",
            "role": "ROLE_USER",
            "parts": [
                {"text": "Analyze this image and highlight any faces."},
                {
                    "raw": "iVBORw0KGgoAAAANSUhEUgAAAAUA",
                    "filename": "input_image.png",
                    "mediaType": "image/png",
                },
            ],
        }
    }

    response = client.post(
        "/message:send",
        headers=_auth({"Content-Type": "application/a2a+json"}),
        content=json.dumps(payload),
    )

    assert response.status_code == 200
    task = response.json()["task"]
    file_part = task["history"][0]["parts"][1]
    assert task["status"]["state"] == "TASK_STATE_WORKING"
    assert file_part["raw"] == "iVBORw0KGgoAAAANSUhEUgAAAAUA"
    assert file_part["filename"] == "input_image.png"
    assert file_part["mediaType"] == "image/png"


def test_structured_data_exchange_schema_metadata_and_data_part_are_accepted(client: TestClient) -> None:
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "ticketNumber": {"type": "string"},
                "description": {"type": "string"},
            },
        },
    }
    payload = {
        "message": {
            "messageId": "structured-data-1",
            "role": "ROLE_USER",
            "parts": [
                {
                    "text": "Show me a list of my open IT tickets",
                    "metadata": {"mediaType": "application/json", "schema": schema},
                },
                {
                    "data": [
                        {"ticketNumber": "REQ12312", "description": "request for VPN access"},
                        {"ticketNumber": "REQ23422", "description": "Add to DL"},
                    ],
                    "mediaType": "application/json",
                },
            ],
        }
    }

    response = client.post("/message:send", headers=_auth(), json=payload)

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_WORKING"
    assert task["history"][0]["parts"][0]["metadata"]["schema"] == schema
    assert task["history"][0]["parts"][1]["data"][0]["ticketNumber"] == "REQ12312"


def test_extended_agent_card_is_publicly_advertised_and_authenticated(client: TestClient) -> None:
    public_card = client.get("/.well-known/agent-card.json").json()

    assert public_card["capabilities"]["extendedAgentCard"] is True
    assert "securitySchemes" in public_card

    unauthenticated = client.get("/extendedAgentCard")
    assert unauthenticated.status_code == 401
    assert "Bearer" in unauthenticated.headers["www-authenticate"]

    authenticated = client.get("/extendedAgentCard", headers=_auth())
    assert authenticated.status_code == 200
    extended_card = authenticated.json()
    assert extended_card["metadata"]["card"] == "extended"
    assert extended_card["skills"]


def test_protocol_requests_require_declared_bearer_credentials(client: TestClient) -> None:
    missing = client.post(
        "/",
        json={"jsonrpc": "2.0", "id": "test-id", "method": "ListTasks", "params": {}},
    )
    invalid = client.post(
        "/",
        headers={"Authorization": "Bearer wrong"},
        json={"jsonrpc": "2.0", "id": "test-id", "method": "ListTasks", "params": {}},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_invoke_exposes_caller_and_grant_ids_on_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP /invoke adapter must wire resolver.principal_id + grant.grant_id
    into ``ctx.caller`` and ``ctx.grant_ids``."""
    monkeypatch.setenv("A2A_API_KEY", "probe-key")
    monkeypatch.setenv("A2A_API_KEY_ID", "probe-id-1234567890")

    class _CallerProbe(A2AAgent):
        name = "caller-probe"
        description = "Surfaces caller + grant_ids"
        auth_model = APIKeyAuth

        @skill(description="echo caller/grant_ids")
        async def echo(self, ctx: RunContext[APIKeyAuth]) -> dict:
            return {
                "caller": ctx.caller,
                "grant_ids": list(ctx.grant_ids),
            }

    grant, token = mint_grant(
        issuer="cp",
        audience="caller-probe",
        bucket="b",
        mode=WorkspaceMode.READ_ONLY,
    )

    http_client = TestClient(build_app(_CallerProbe()))

    no_grant = http_client.post(
        "/invoke/echo",
        headers={"Authorization": "Bearer probe-key"},
        json={"arguments": {}},
    )
    assert no_grant.status_code == 200, no_grant.text
    assert no_grant.json()["result"] == {
        "caller": "apikey:probe-id-123",
        "grant_ids": [],
    }

    with_grant = http_client.post(
        "/invoke/echo",
        headers={"Authorization": "Bearer probe-key"},
        json={"arguments": {}, "grant": token},
    )
    assert with_grant.status_code == 200, with_grant.text
    body = with_grant.json()["result"]
    assert body["caller"] == "apikey:probe-id-123"
    assert body["grant_ids"] == [grant.grant_id]


def test_invoke_context_attaches_discovery_backed_a2a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    monkeypatch.setenv("A2A_API_KEY", "probe-key")
    monkeypatch.setenv("A2A_API_KEY_ID", "probe-id-1234567890")
    monkeypatch.setenv("A2A_CP_URL", "http://cp.local")

    calls: list[tuple[str, str, dict]] = []

    def _card(name: str) -> dict:
        return {
            "name": name,
            "description": "remote fixture",
            "version": "0.1.0",
            "skills": [
                {
                    "id": "build",
                    "name": "build",
                    "description": "Build.",
                    "tags": [],
                    "scopes": [],
                    "stream": False,
                    "policy": {},
                    "input_schema": {"type": "object", "properties": {}},
                    "output_schema": {"type": "object", "properties": {}},
                }
            ],
            "capabilities": {},
            "input_modes": ["application/json"],
            "output_modes": ["application/json"],
            "required_secrets": [],
            "required_env": [],
            "runtime": {},
            "workspace_access": {},
            "mcp_endpoint": "/mcp",
        }

    class _Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self._payload

    class _AsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, **kwargs: object) -> _Response:
            calls.append(("GET", url, dict(kwargs)))
            headers = dict(kwargs.get("headers") or {})
            assert headers["authorization"] == "bearer user-cp-jwt"
            if url == "http://cp.local/v1/agents/agent-builder":
                return _Response(
                    {
                        "name": "agent-builder",
                        "url": "http://agent-builder.local",
                        "card": _card("agent-builder"),
                    }
                )
            raise AssertionError(f"unexpected URL: {url}")

        async def post(self, url: str, json: dict, **kwargs: object) -> _Response:
            if url == "http://cp.local/v1/me/subagent-runs/track":
                return _Response({"ok": True})
            calls.append(("POST", url, json, dict(kwargs)))
            headers = dict(kwargs.get("headers") or {})
            assert url == "http://agent-builder.local/invoke/build"
            assert headers["authorization"] == "bearer user-cp-jwt"
            assert json["arguments"] == {"name": "smoke-agent"}
            assert json["cp_jwt"] == "user-cp-jwt"
            assert json["cp_url"] == "http://cp.local"
            return _Response({"result": {"ok": True, "url": "https://agent.example"}})

    class _CallerProbe(A2AAgent):
        name = "caller-probe"
        description = "Calls another hosted agent by registry name"
        auth_model = APIKeyAuth
        wants_cp_jwt = True

        @skill(description="call agent-builder")
        async def go(self, ctx: RunContext[APIKeyAuth]) -> dict:
            assert ctx.discover is not None
            call = await ctx.call(
                "agent-builder",
                "build",
                args={"name": "smoke-agent"},
            )
            return call.result

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    http_client = TestClient(build_app(_CallerProbe()))

    response = http_client.post(
        "/invoke/go",
        headers={"Authorization": "Bearer probe-key"},
        json={"arguments": {}, "cp_jwt": "user-cp-jwt", "cp_url": "http://cp.local"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"ok": True, "url": "https://agent.example"}
    assert [call[:2] for call in calls] == [
        ("GET", "http://cp.local/v1/agents/agent-builder"),
        ("POST", "http://agent-builder.local/invoke/build"),
    ]


def test_invoke_rejects_grant_for_other_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_API_KEY", "probe-key")
    called = False

    class _CallerProbe(A2AAgent):
        name = "caller-probe"
        description = "Should not run for mismatched grants"
        auth_model = APIKeyAuth

        @skill(description="would expose workspace if called")
        async def echo(self, ctx: RunContext[APIKeyAuth]) -> dict:
            nonlocal called
            called = True
            return {"grant_ids": list(ctx.grant_ids)}

    _, token = mint_grant(
        issuer="cp",
        audience="other-agent",
        bucket="victim-files",
        mode=WorkspaceMode.READ_ONLY,
    )

    http_client = TestClient(build_app(_CallerProbe()))
    response = http_client.post(
        "/invoke/echo",
        headers={"Authorization": "Bearer probe-key"},
        json={"arguments": {}, "grant": token},
    )

    assert response.status_code == 403
    assert "grant audience mismatch" in response.json()["detail"]
    assert called is False


def test_scope_grant_rejects_other_audience_before_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CallerProbe(A2AAgent):
        name = "caller-probe"
        description = "Scope grant audience fixture"
        auth_model = APIKeyAuth

    _, token = mint_grant(
        issuer="cp",
        audience="other-agent",
        bucket="victim-files",
        mode=WorkspaceMode.READ_ONLY,
    )
    loop = asyncio.new_event_loop()
    fut: asyncio.Future = loop.create_future()
    _ScopeRegistry._waiters["sr_mismatch"] = fut
    try:
        http_client = TestClient(build_app(_CallerProbe()))
        response = http_client.post(
            "/scope-grants/sr_mismatch",
            headers=_auth(),
            json={"grant": token},
        )

        assert response.status_code == 403
        assert "grant audience mismatch" in response.json()["detail"]
        assert fut.done() is False
    finally:
        _ScopeRegistry._waiters.pop("sr_mismatch", None)
        loop.close()
