from __future__ import annotations

import json
import os

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from a2a_pack.grants import mint_grant
from a2a_pack.receipts import verify_receipt
from a2a_pack.replay import verify_replay_session

from control_plane.agent_ingress import (
    AgentConcurrencyBulkhead,
    AgentTarget,
    BodyCapture,
    EvidenceBundle,
    _build_evidence,
    _operation_from_request,
    _persist_evidence,
    _reservation,
    _resolve_public_agent,
    create_app,
)
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentCustomDomain,
    AgentReceipt,
    AgentSession,
    User,
)
from control_plane.object_store import (
    InMemoryReplayObjectStore,
    session_events_key,
    set_default_store,
)


@pytest.fixture
async def gateway_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        owner = User(email="gateway-owner@customer.test", password_hash="hash")
        session.add(owner)
        await session.flush()
        public = Agent(
            owner_id=owner.id,
            name="research-agent",
            description="",
            version="1.2.3",
            image="registry.example.com/agents/research-agent:latest",
            public=True,
            status="running",
            url="https://research-agent.example.com",
            card={
                "version": "1.2.3",
                "skills": [
                    {"name": "echo"},
                    {"name": "telegram_webhook"},
                    {"name": "report_csv"},
                ],
                "runtime": {
                    "endpoints": [
                        {
                            "name": "telegram",
                            "path": "/telegram/webhook",
                            "methods": ["POST"],
                            "skill": "telegram_webhook",
                            "body_arg": "update",
                            "headers_arg": "headers",
                            "query_arg": "query",
                        },
                        {
                            "name": "report",
                            "path": "/report.csv",
                            "methods": ["POST"],
                            "skill": "report_csv",
                            "body_arg": "spec",
                        },
                    ]
                },
            },
        )
        private = Agent(
            owner_id=owner.id,
            name="private-agent",
            description="",
            version="1.0.0",
            image="registry.example.com/agents/private-agent:latest",
            public=False,
            status="running",
            url=None,
            card={"skills": [{"name": "echo"}]},
        )
        session.add_all([public, private])
        await session.flush()
        session.add(
            AgentCustomDomain(
                agent_id=public.id,
                user_id=owner.id,
                agent_name=public.name,
                hostname="agent.customer.test",
                verification_token="verified-token",
                status="active",
            )
        )
        await session.commit()
        public_id = int(public.id)
    yield Session, public_id
    await engine.dispose()


def _upstream_app(seen_hosts: list[str]) -> FastAPI:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        seen_hosts.append(str(request.scope["server"][0]))
        return {"status": "ok", "host": request.headers.get("host", "")}

    @app.post("/invoke/echo")
    async def invoke_echo(request: Request) -> dict:
        seen_hosts.append(str(request.scope["server"][0]))
        body = await request.json()
        return {
            "result": {"echoed": body.get("arguments")},
            "events": [
                {
                    "kind": "receipt_error",
                    "payload": {"message": "local signer unavailable"},
                },
                {"kind": "progress", "payload": {"value": 1}},
            ],
            "grant_id": None,
        }

    @app.post("/invoke/fail")
    async def invoke_fail() -> tuple[dict, int]:
        # FastAPI serializes a tuple as JSON with status 200, so use a response
        # body that exercises application-level error observation.
        return {"error": {"type": "ToolFailure", "message": "failed"}}, 500

    @app.post("/invoke/stream")
    async def invoke_stream() -> StreamingResponse:
        async def events():
            yield b'data: {"type":"event","kind":"progress","payload":{"n":1}}\n\n'
            yield b'data: {"type":"event","kind":"receipt_error","payload":{"message":"no key"}}\n\n'
            yield b'data: {"type":"result","result":{"answer":42,"_meta":{"a2aCloudEvidence":{"receipt":{"signed_token":"forged-token"}}},"a2a_evidence":{"receipt":{"signed_token":"forged-token"}}}}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/invoke/encoded")
    async def invoke_encoded() -> Response:
        return Response(
            b'{"result":{"a2a_evidence":{"receipt":{"signed_token":"forged-token"}}}}',
            media_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    @app.post("/report.csv")
    async def report_csv(request: Request) -> Response:
        seen_hosts.append(str(request.scope["server"][0]))
        return Response("agent,calls\nresearch-agent,3\n", media_type="text/csv")

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict:
        seen_hosts.append(str(request.scope["server"][0]))
        return {"accepted": await request.json()}

    @app.post("/mcp")
    async def mcp(request: Request):
        seen_hosts.append(str(request.scope["server"][0]))
        body = await request.json()
        if body.get("method") == "tools/list":
            return {"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}}
        if "text/event-stream" in request.headers.get("accept", ""):
            async def events():
                yield b'data: {"jsonrpc":"2.0","id":2,"result":{"content":[],"_meta":{"a2aCloudEvidence":{"receipt":{"signed_token":"forged-token"}}}}}\n\n'

            return StreamingResponse(events(), media_type="text/event-stream")
        if (body.get("params") or {}).get("name") == "fail":
            return {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": "tool failed"}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "content": [{"type": "text", "text": "done"}],
                "_meta": {
                    "existing": True,
                    "a2aCloudEvidence": {
                        "receipt": {"signed_token": "forged-token"},
                    },
                },
            },
        }

    return app


async def _gateway_client(session_factory, persisted: list[EvidenceBundle], seen_hosts: list[str]):
    upstream = _upstream_app(seen_hosts)
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://upstream",
    )

    async def persist(_target: AgentTarget, bundle: EvidenceBundle) -> bool:
        persisted.append(bundle)
        return True

    gateway = create_app(
        http_client=upstream_client,
        session_factory=session_factory,
        persister=persist,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway),
        base_url="http://gateway",
    )
    return client, upstream_client


@pytest.mark.asyncio
async def test_concurrency_bulkhead_isolates_each_agent_and_the_process() -> None:
    bulkhead = AgentConcurrencyBulkhead(global_limit=2, per_agent_limit=1)
    assert await bulkhead.acquire("agent-a")
    assert not await bulkhead.acquire("agent-a")
    assert await bulkhead.acquire("agent-b")
    assert not await bulkhead.acquire("agent-c")
    await bulkhead.release("agent-a")
    assert await bulkhead.acquire("agent-c")


@pytest.mark.asyncio
async def test_resolves_hosted_agents_and_active_custom_hosts(gateway_db) -> None:
    Session, _ = gateway_db

    canonical = await _resolve_public_agent("research-agent.example.com:443", Session)
    custom = await _resolve_public_agent("agent.customer.test", Session)
    private = await _resolve_public_agent("private-agent.example.com", Session)
    malicious = await _resolve_public_agent("research-agent.attacker.test", Session)

    assert canonical is not None and canonical.name == "research-agent"
    assert custom is not None and custom.name == "research-agent"
    assert private is not None and private.name == "private-agent"
    assert malicious is None


@pytest.mark.asyncio
async def test_active_custom_domain_is_preserved_for_application_routing(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.get("/healthz", headers={"Host": "agent.customer.test"})
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    assert response.json()["host"] == "agent.customer.test"
    assert seen_hosts == ["research-agent.agents.svc.cluster.local"]


@pytest.mark.asyncio
async def test_runtime_declared_raw_endpoint_execution_is_signed(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/telegram/webhook?source=bot",
            headers={
                "Host": "research-agent.example.com",
                "Authorization": "Bearer webhook-secret",
            },
            json={"message": {"text": "hello"}},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    receipt = verify_receipt(response.json()["a2a_evidence"]["receipt"]["signed_token"])
    assert receipt.skill_name == "telegram_webhook"
    assert receipt.caller == "credential-present"
    assert receipt.grant_ids == ()
    assert '"message":{"text":"hello"}' in receipt.input_preview
    assert "webhook-secret" not in receipt.input_preview
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_direct_invoke_returns_platform_signed_redacted_evidence(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/invoke/echo",
            headers={
                "Host": "research-agent.example.com",
                "Authorization": "Bearer opaque-agent-key",
            },
            json={
                "arguments": {
                    "query": "owls",
                    "api_key": "must-not-leak",
                    "nested": {"password": "also-secret"},
                    "opaque": "eyJheader.payload.signature",
                },
                "cp_jwt": "platform-token-must-not-leak",
                "llm_creds": {"api_key": "llm-secret"},
            },
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["echoed"]["query"] == "owls"
    assert [event["kind"] for event in payload["events"]] == ["progress"]
    evidence = payload["a2a_evidence"]
    receipt_token = evidence["receipt"]["signed_token"]
    replay_token = evidence["replay"]["signed_token"]
    receipt = verify_receipt(receipt_token)
    replay = verify_replay_session(replay_token)

    assert receipt.receipt_id == response.headers["X-A2A-Receipt-ID"]
    assert response.headers["X-A2A-Receipt-Token"] == receipt_token
    assert receipt.agent_name == "research-agent"
    assert receipt.agent_version == "1.2.3"
    assert receipt.skill_name == "echo"
    assert receipt.caller == "credential-present"
    assert receipt.status == "ok"
    assert "must-not-leak" not in receipt.input_preview
    assert "also-secret" not in receipt.input_preview
    assert "eyJheader.payload.signature" not in receipt.input_preview
    assert "platform-token-must-not-leak" not in receipt.input_preview
    assert "llm-secret" not in receipt.input_preview
    assert "[redacted]" in receipt.input_preview
    assert replay.receipt_id == receipt.receipt_id
    assert [event.kind for event in replay.events] == ["skill_start", "skill_end"]
    assert len(persisted) == 1
    # The target is built from the resolved database name, never the incoming
    # Host value or a stored arbitrary URL.
    assert seen_hosts == ["research-agent.agents.svc.cluster.local"]


@pytest.mark.asyncio
async def test_non_json_response_still_hands_the_caller_its_signed_evidence(
    gateway_db,
) -> None:
    """A caller who cannot be attributed still receives the evidence inline.

    ``/v1/sessions/{id}`` and ``/v1/agents/{n}/receipts/{id}`` are gated by
    ``agent_authorization``, and the gateway records ``caller`` as
    ``"credential-present"`` because it cannot assert who authenticated. The
    evidence URLs in the response headers are therefore not openable by that
    caller — which is only acceptable because the same response carries the
    complete signed tokens. It cannot become a bare URL handout: JSON gets
    ``a2a_evidence``, SSE gets the ``a2a.evidence`` frame, and every other
    content type gets these two headers.
    """
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/report.csv",
            headers={"Host": "research-agent.example.com"},
            json={"window": "7d"},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text == "agent,calls\nresearch-agent,3\n"

    receipt = verify_receipt(response.headers["X-A2A-Receipt-Token"])
    replay = verify_replay_session(response.headers["X-A2A-Replay-Token"])
    assert receipt.receipt_id == response.headers["X-A2A-Receipt-ID"]
    assert replay.session_id == response.headers["X-A2A-Replay-Session-ID"]
    assert replay.receipt_id == receipt.receipt_id
    # Unattributable by construction, hence the inline hand-off above.
    assert receipt.caller == "anonymous"
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_mcp_tools_call_is_signed_but_discovery_is_not(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        listed = await client.post(
            "/mcp",
            headers={"Host": "research-agent.example.com"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        called = await client.post(
            "/mcp",
            headers={"Host": "research-agent.example.com"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"text": "hello"},
                },
            },
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert listed.status_code == 200
    assert "X-A2A-Receipt-ID" not in listed.headers
    assert "a2aCloudEvidence" not in listed.text
    assert called.status_code == 200
    metadata = called.json()["result"]["_meta"]
    assert metadata["existing"] is True
    evidence = metadata["a2aCloudEvidence"]
    receipt = verify_receipt(evidence["receipt"]["signed_token"])
    assert receipt.skill_name == "echo"
    assert receipt.input_preview == '{"text":"hello"}'
    assert "forged-token" not in called.text
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_mcp_sse_strips_nested_untrusted_evidence(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/mcp",
            headers={
                "Host": "research-agent.example.com",
                "Accept": "text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {}},
            },
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    assert "forged-token" not in response.text
    assert '"type":"a2a.evidence"' in response.text
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_mcp_tool_error_is_signed_as_error(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/mcp",
            headers={"Host": "research-agent.example.com"},
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "fail", "arguments": {}},
            },
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    receipt = verify_receipt(
        response.json()["result"]["_meta"]["a2aCloudEvidence"]["receipt"]["signed_token"]
    )
    assert receipt.status == "error"
    assert receipt.error_type == "MCPToolError"


@pytest.mark.asyncio
async def test_sse_stream_keeps_progress_and_replaces_untrusted_evidence(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/invoke/stream",
            headers={
                "Host": "research-agent.example.com",
                "Accept": "text/event-stream",
            },
            json={"arguments": {"topic": "proof"}},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 200
    assert '"kind":"progress"' in response.text
    assert "receipt_error" not in response.text
    assert response.text.rstrip().endswith("data: [DONE]")
    assert "forged-token" not in response.text
    data_values = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    evidence_event = next(
        json.loads(value) for value in data_values if json.loads(value).get("type") == "a2a.evidence"
    )
    receipt = verify_receipt(
        evidence_event["evidence"]["receipt"]["signed_token"]
    )
    assert receipt.result_preview == '{"answer":42}'
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_oversized_sse_frame_fails_closed_with_signed_error(
    gateway_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Session, _ = gateway_db
    monkeypatch.setattr("control_plane.agent_ingress._MAX_SSE_FRAME_BYTES", 32)
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/invoke/stream",
            headers={
                "Host": "research-agent.example.com",
                "Accept": "text/event-stream",
            },
            json={"arguments": {}},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert "agent SSE frame exceeds 1 MiB" in response.text
    assert "forged-token" not in response.text
    receipt = persisted[0].receipt
    assert receipt.status == "error"
    assert receipt.error_type == "HTTP502"


@pytest.mark.asyncio
async def test_encoded_structured_response_fails_closed_with_signed_evidence(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/invoke/encoded",
            headers={"Host": "research-agent.example.com"},
            json={"arguments": {}},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 502
    assert response.json()["detail"] == "encoded structured agent response is not supported"
    assert "forged-token" not in response.text
    assert verify_receipt(
        response.json()["a2a_evidence"]["receipt"]["signed_token"]
    ).status == "error"


@pytest.mark.asyncio
async def test_oversized_execution_response_fails_closed_with_signed_evidence(
    gateway_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Session, _ = gateway_db
    monkeypatch.setattr("control_plane.agent_ingress._MAX_INLINE_RESPONSE_BYTES", 64)
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/invoke/echo",
            headers={"Host": "research-agent.example.com"},
            json={"arguments": {"payload": "x" * 128}},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 502
    assert response.json()["detail"] == "agent response exceeds 4 MiB"
    assert verify_receipt(
        response.json()["a2a_evidence"]["receipt"]["signed_token"]
    ).status == "error"


@pytest.mark.asyncio
async def test_private_platform_host_routes_but_untrusted_host_does_not(gateway_db) -> None:
    Session, _ = gateway_db
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        private = await client.get(
            "/healthz", headers={"Host": "private-agent.example.com"}
        )
        attacker = await client.get(
            "/healthz", headers={"Host": "research-agent.attacker.test"}
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert private.status_code == 200
    assert attacker.status_code == 404
    assert seen_hosts == ["private-agent.agents.svc.cluster.local"]


@pytest.mark.asyncio
async def test_missing_signer_fails_before_execution(
    gateway_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Session, _ = gateway_db
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        readiness = await client.get("/__gateway/healthz")
        liveness = await client.get("/__gateway/livez")
        response = await client.post(
            "/invoke/echo",
            headers={"Host": "research-agent.example.com"},
            json={"arguments": {"text": "must not run"}},
        )
        raw_response = await client.post(
            "/telegram/webhook",
            headers={"Host": "research-agent.example.com"},
            json={"message": {"text": "must not run"}},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert readiness.status_code == 503
    assert liveness.status_code == 200
    assert response.status_code == 503
    assert raw_response.status_code == 503
    assert response.json()["detail"] == "execution evidence signer unavailable"
    assert seen_hosts == []
    assert persisted == []


@pytest.mark.asyncio
async def test_missing_signer_does_not_block_mcp_discovery(
    gateway_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Session, _ = gateway_db
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        listed = await client.post(
            "/mcp",
            headers={"Host": "research-agent.example.com"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        called = await client.post(
            "/mcp",
            headers={"Host": "research-agent.example.com"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {}},
            },
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert listed.status_code == 200
    assert called.status_code == 503
    assert seen_hosts == ["research-agent.agents.svc.cluster.local"]
    assert persisted == []


@pytest.mark.asyncio
async def test_mcp_does_not_claim_client_supplied_grant_provenance(
    gateway_db,
) -> None:
    Session, _ = gateway_db
    grant, token = mint_grant(
        issuer="planner-agent",
        audience="research-agent",
        bucket="user-1-files",
    )
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/mcp",
            headers={
                "Host": "research-agent.example.com",
                "Authorization": f"Bearer {token}",
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "echo",
                    "arguments": {"text": "hello"},
                    "_meta": {"grant": token},
                },
            },
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    receipt = verify_receipt(
        response.json()["result"]["_meta"]["a2aCloudEvidence"]["receipt"]["signed_token"]
    )
    assert receipt.caller == "credential-present"
    assert receipt.grant_ids == ()
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_invoke_records_grant_consumed_from_execution_body(gateway_db) -> None:
    Session, _ = gateway_db
    grant, token = mint_grant(
        issuer="planner-agent",
        audience="research-agent",
        bucket="user-1-files",
    )
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/invoke/echo",
            headers={"Host": "research-agent.example.com"},
            json={"arguments": {"text": "hello"}, "grant": token},
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    receipt = verify_receipt(response.json()["a2a_evidence"]["receipt"]["signed_token"])
    assert receipt.caller == "planner-agent"
    assert receipt.grant_ids == (grant.grant_id,)
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_oversized_mcp_envelope_is_rejected_before_it_can_evade_classification(
    gateway_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Session, _ = gateway_db
    monkeypatch.setattr("control_plane.agent_ingress._MAX_REQUEST_CAPTURE_BYTES", 1024)
    persisted: list[EvidenceBundle] = []
    seen_hosts: list[str] = []
    client, upstream_client = await _gateway_client(Session, persisted, seen_hosts)
    try:
        response = await client.post(
            "/mcp",
            headers={"Host": "research-agent.example.com"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "echo",
                        "arguments": {"payload": "x" * 2048},
                    },
                }
            ),
        )
    finally:
        await client.aclose()
        await upstream_client.aclose()

    assert response.status_code == 413
    assert seen_hosts == []
    assert persisted == []


@pytest.mark.asyncio
async def test_persistence_records_zero_economics_and_signed_replay(
    gateway_db,
) -> None:
    Session, public_id = gateway_db
    store = InMemoryReplayObjectStore()
    set_default_store(store)
    try:
        target = AgentTarget(id=public_id, name="research-agent", version="1.2.3")
        body = BodyCapture.with_limit(1024)
        body.add(b'{"arguments":{"q":"owls"}}')
        operation = _operation_from_request(
            method="POST",
            path="/invoke/echo",
            body=body,
            headers={},
            target=target,
        )
        assert operation is not None
        bundle = _build_evidence(
            target=target,
            operation=operation,
            reservation=_reservation(),
            response_body=b'{"result":{"hits":3}}',
            response_truncated=False,
            response_content_type="application/json",
            response_status_code=200,
        )

        assert await _persist_evidence(target, bundle, session_factory=Session)
        async with Session() as session:
            receipt = (
                await session.execute(
                    select(AgentReceipt).where(
                        AgentReceipt.receipt_id == bundle.receipt.receipt_id
                    )
                )
            ).scalar_one()
            replay = (
                await session.execute(
                    select(AgentSession).where(
                        AgentSession.session_id == bundle.replay.session_id
                    )
                )
            ).scalar_one()

        assert receipt.signed_token == bundle.receipt_token
        assert replay.signed_token == bundle.replay_token
        key = session_events_key(public_id, bundle.replay.session_id)
        assert [event["kind"] for event in store.get_all(key)] == [
            "skill_start",
            "skill_end",
        ]
    finally:
        set_default_store(None)
