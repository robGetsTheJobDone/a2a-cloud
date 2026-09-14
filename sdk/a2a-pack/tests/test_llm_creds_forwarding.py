from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import a2a_pack.serve.asgi as asgi_module
from a2a_pack import (
    AccountAccess,
    A2AAgent,
    AgentEndpoint,
    ArtifactRef,
    LLMProvisioning,
    Resources,
    RunContext,
    WorkspaceMode,
    skill,
)
from a2a_pack.a2a_client import A2AClient, CallResult
from a2a_pack.auth import NoAuth
from a2a_pack.auth import PlatformUserAuth
from a2a_pack.context import LLMCreds, LocalRunContext
from a2a_pack.grants import mint_grant
from a2a_pack.serve import build_app


class _CapturingClient(A2AClient):
    def __init__(self) -> None:
        self.llm_creds: dict[str, Any] | None = None

    async def call(
        self,
        target: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        cp_jwt: str | None = None,
        cp_url: str | None = None,
        llm_creds: dict[str, Any] | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
        timeout: float | None = None,
        composition: dict[str, Any] | None = None,
    ) -> CallResult:
        del consumer_config, consumer_secrets
        self.llm_creds = llm_creds
        return CallResult(result={"ok": True})


class _CallerProvidedLLMAgent(A2AAgent):
    name = "caller-llm-agent"
    description = "Inspects forwarded LLM credentials"
    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.CALLER_PROVIDED

    @skill(description="Inspect LLM credentials")
    async def inspect_llm(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        creds = ctx.llm
        return {"extra_body": creds.extra_body, "model": creds.model}


class _PlatformLLMAgent(A2AAgent):
    name = "platform-llm-agent"
    description = "Inspects platform-routed LLM credentials"
    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.PLATFORM

    @skill(description="Inspect LLM credentials")
    async def inspect_llm(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        creds = ctx.llm
        return {"api_key": creds.api_key, "model": creds.model, "source": creds.source}


class _AccountTrialAgent(A2AAgent):
    name = "account-trial-agent"
    account_access = AccountAccess(required=True, platform_skill_calls=2)

    @skill(description="Return a value")
    async def run(self, ctx: RunContext[NoAuth]) -> dict[str, bool]:
        del ctx
        return {"ok": True}


def test_account_access_rejects_anonymous_invoke_and_accepts_platform_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(build_app(_AccountTrialAgent()))

    denied = client.post("/invoke/run", json={"arguments": {}})
    assert denied.status_code == 401
    assert denied.json()["detail"]["error"] == "account_required"

    async def fake_resolve(*_args: object, **_kwargs: object) -> PlatformUserAuth:
        return PlatformUserAuth(sub="7", user_id=7, email="user@example.test")

    monkeypatch.setattr(asgi_module.PlatformUserAuthResolver, "resolve", fake_resolve)
    accepted = client.post(
        "/invoke/run",
        headers={"Authorization": "Bearer account-session"},
        json={"arguments": {}},
    )
    assert accepted.status_code == 200
    assert accepted.json()["result"] == {"ok": True}


class _DualLLMAgent(A2AAgent):
    name = "dual-llm-agent"
    description = "Inspects platform-or-caller LLM credentials"
    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED

    @skill(description="Inspect LLM credentials")
    async def inspect_llm(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        creds = ctx.llm
        return {"api_key": creds.api_key, "model": creds.model, "source": creds.source}


class _DirectPlatformLLMAgent(A2AAgent):
    name = "direct-platform-llm-agent"
    description = "Inspects direct invoke platform-routed LLM credentials"
    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.PLATFORM
    resources = Resources(max_runtime_seconds=840)

    @skill(description="Inspect direct invoke LLM credentials")
    async def inspect(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        creds = ctx.llm
        return {
            "api_key": creds.api_key,
            "model": creds.model,
            "source": creds.source,
            "workspace_bucket": ctx.workspace.bucket,
            "grant_ids": list(getattr(ctx, "grant_ids", ())),
        }


class _RawPlatformLLMAgent(A2AAgent):
    name = "raw-platform-llm-agent"
    description = "Inspects raw endpoint platform-routed LLM credentials"
    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.PLATFORM
    resources = Resources(max_runtime_seconds=840)
    endpoints = (
        AgentEndpoint(
            name="telegram",
            path="/telegram/webhook",
            skill="webhook",
            body_arg="update",
        ),
    )

    @skill(description="Inspect raw endpoint LLM credentials")
    async def webhook(
        self,
        ctx: RunContext[NoAuth],
        update: dict[str, Any],
    ) -> dict[str, Any]:
        creds = ctx.llm
        return {
            "text": str(update.get("message", {}).get("text")),
            "api_key": creds.api_key,
            "model": creds.model,
            "source": creds.source,
            "workspace_bucket": ctx.workspace.bucket,
            "grant_ids": list(getattr(ctx, "grant_ids", ())),
        }


class _UsageTrackingAgent(A2AAgent):
    name = "usage-tracking-agent"
    description = "Returns LangChain-style message usage metadata"
    auth_model = NoAuth

    @skill(description="Return usage metadata")
    async def inspect(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        del ctx
        return {
            "messages": [
                {
                    "content": "first",
                    "usage_metadata": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                    },
                    "response_metadata": {
                        "model_name": "gpt-5",
                        "provider": "openai",
                        "response_cost": 0.002,
                    },
                },
                {
                    "content": "second",
                    "usage_metadata": {
                        "input_tokens": 6,
                        "output_tokens": 5,
                        "total_tokens": 11,
                    },
                    "response_metadata": {
                        "model_name": "gpt-5",
                        "provider": "openai",
                        "response_cost": 0.001,
                    },
                },
            ],
        }


class _PartialResultAgent(A2AAgent):
    name = "partial-result-agent"
    description = "Returns a nested partial result"
    auth_model = NoAuth

    @skill(description="Return a partial result payload")
    async def inspect(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        del ctx
        return {
            "ok": False,
            "status": "partial",
            "stop_reason": "builder_failed",
            "agent_url": None,
        }


class _SlowTrackingAgent(A2AAgent):
    name = "slow-tracking-agent"
    description = "Runs long enough to emit invoke tracking heartbeats"
    auth_model = NoAuth

    @skill(description="Wait briefly")
    async def wait(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        del ctx
        await asyncio.sleep(0.25)
        return {"ok": True}


class _EventTrackingAgent(A2AAgent):
    name = "event-tracking-agent"
    description = "Emits progress and artifact events during invoke"
    auth_model = NoAuth

    @skill(description="Emit progress and artifact events")
    async def report(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        await ctx.emit_progress("created workspace repo")
        await ctx.emit_artifact(
            ArtifactRef(
                name="agent.py",
                uri="s3://user-files/agents/demo/agent.py",
                mime_type="text/x-python",
                size_bytes=123,
            )
        )
        return {"ok": True}


@pytest.mark.asyncio
async def test_call_forwards_llm_temperature_and_extra_body() -> None:
    client = _CapturingClient()
    ctx = LocalRunContext(auth=NoAuth(), a2a=client)
    object.__setattr__(
        ctx,
        "_llm_creds",
        LLMCreds(
            base_url="http://litellm.llm.svc.cluster.local:4000/v1",
            api_key="platform-key",
            model="a2a-user-2-kimi",
            source="caller",
            temperature_mode="omit",
            temperature=None,
            extra_body={"extra_body": {"thinking": {"type": "disabled"}}},
        ),
    )

    await ctx.call("agent", "skill")

    assert client.llm_creds == {
        "base_url": "http://litellm.llm.svc.cluster.local:4000/v1",
        "api_key": "platform-key",
        "model": "a2a-user-2-kimi",
        "source": "caller",
        "temperature_mode": "omit",
        "temperature": None,
        "extra_body": {"extra_body": {"thinking": {"type": "disabled"}}},
        "metadata": {},
    }


def test_invoke_accepts_null_llm_extra_body() -> None:
    client = TestClient(build_app(_CallerProvidedLLMAgent()))

    response = client.post(
        "/invoke/inspect_llm",
        json={
            "arguments": {},
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": "platform-key",
                "model": "a2a-user-2-kimi",
                "extra_body": None,
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "extra_body": {},
        "model": "a2a-user-2-kimi",
    }


def test_invoke_tracking_posts_aggregated_llm_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, Any], **_kwargs: Any) -> None:
            posted.append(json)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(build_app(_UsageTrackingAgent()))
    response = client.post(
        "/invoke/inspect",
        json={
            "arguments": {},
            "cp_jwt": "cp-token",
            "cp_url": "https://cp.example.test",
        },
    )

    assert response.status_code == 200, response.text
    complete = posted[-1]
    assert complete["type"] == "agent_invoke_complete"
    assert "llm_usage" in complete, json.dumps(complete, sort_keys=True)
    assert complete["llm_usage"]["prompt_tokens"] == 16
    assert complete["llm_usage"]["completion_tokens"] == 9
    assert complete["llm_usage"]["total_tokens"] == 25
    assert complete["llm_usage"]["cost_usd"] == 0.003
    assert complete["llm_usage"]["model"] == "gpt-5"
    assert complete["llm_usage"]["provider"] == "openai"
    assert complete["llm_usage"]["metadata"]["call_count"] == 2
    assert complete["llm_usage"]["metadata"]["transport_runtime"] == "a2a_pack"
    assert complete["llm_usage"]["metadata"]["usage_authority"] == "litellm_response"


def test_direct_invoke_posts_tracking_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, Any], **_kwargs: Any) -> None:
            posted.append(json)

    import httpx

    monkeypatch.setenv("A2A_INVOKE_TRACKING_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(build_app(_SlowTrackingAgent()))
    response = client.post(
        "/invoke/wait",
        json={
            "arguments": {},
            "cp_jwt": "cp-token",
            "cp_url": "https://cp.example.test",
        },
    )

    assert response.status_code == 200, response.text
    event_types = [event["type"] for event in posted]
    assert event_types[0] == "agent_invoke_started"
    assert "agent_invoke_heartbeat" in event_types
    assert event_types[-1] == "agent_invoke_complete"


def test_direct_invoke_posts_emitted_events_to_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, Any], **_kwargs: Any) -> None:
            posted.append(json)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(build_app(_EventTrackingAgent()))
    response = client.post(
        "/invoke/report",
        json={
            "arguments": {},
            "cp_jwt": "cp-token",
            "cp_url": "https://cp.example.test",
        },
    )

    assert response.status_code == 200, response.text
    event_types = [event["type"] for event in posted]
    assert event_types[0] == "agent_invoke_started"
    assert event_types[-1] == "agent_invoke_complete"
    assert "agent_progress" in event_types
    assert "agent_artifact" in event_types

    progress = next(event for event in posted if event["type"] == "agent_progress")
    assert progress["summary"] == "created workspace repo"
    assert progress["kind"] == "progress"
    assert progress["payload"] == {"message": "created workspace repo"}
    artifact = next(event for event in posted if event["type"] == "agent_artifact")
    assert artifact["summary"] == "agent.py"
    assert artifact["kind"] == "artifact"
    assert artifact["payload"]["uri"] == "s3://user-files/agents/demo/agent.py"


def test_direct_invoke_tracks_nested_ok_false_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[dict[str, Any]] = []

    class FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, _url: str, *, json: dict[str, Any], **_kwargs: Any) -> None:
            posted.append(json)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(build_app(_PartialResultAgent()))
    response = client.post(
        "/invoke/inspect",
        json={
            "arguments": {},
            "cp_jwt": "cp-token",
            "cp_url": "https://cp.example.test",
        },
    )

    assert response.status_code == 200, response.text
    complete = posted[-1]
    assert complete["type"] == "agent_invoke_error"
    assert complete["ok"] is False
    assert complete["summary"] == "error: builder_failed"
    assert complete["result"] == {
        "ok": False,
        "status": "partial",
        "stop_reason": "builder_failed",
    }


def test_platform_invoke_accepts_a2a_llm_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    _, token = mint_grant(
        issuer="control-plane",
        audience="platform-llm-agent",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_ONLY,
        llm_models=("platform-model",),
    )
    client = TestClient(build_app(_PlatformLLMAgent()))

    response = client.post(
        "/invoke/inspect_llm",
        json={
            "arguments": {},
            "grant": token,
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": token,
                "model": "platform-model",
                "extra_body": None,
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "api_key": token,
        "model": "platform-model",
        "source": "platform_grant",
    }


def test_direct_invoke_fetches_platform_llm_grant_from_cp_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setenv("A2A_SESSION_COOKIE_NAME", "a2a_session")
    grant, token = mint_grant(
        issuer="self:user-7",
        audience="direct-platform-llm-agent",
        bucket="user-7-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        outputs_prefix="outputs/",
        write_prefixes=("outputs/",),
        llm_models=("platform-model",),
    )

    async def fake_fetch(
        *,
        cp_url: str,
        cp_token: str,
        agent_name: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        assert cp_url == "https://cp.example.test"
        assert cp_token == "session-token"
        assert agent_name == "direct-platform-llm-agent"
        assert ttl_seconds == 870
        return {
            "grant": token,
            "grant_id": grant.grant_id,
            "expires_at": grant.expires_at,
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": token,
                "model": "platform-model",
                "temperature_mode": "omit",
                "extra_body": {},
            },
        }

    monkeypatch.setattr(asgi_module, "_fetch_platform_llm_grant_from_cp", fake_fetch)
    client = TestClient(build_app(_DirectPlatformLLMAgent()))
    client.cookies.set("a2a_session", "session-token")

    response = client.post(
        "/invoke/inspect",
        json={"arguments": {}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["grant_id"] == grant.grant_id
    assert body["result"] == {
        "api_key": token,
        "model": "platform-model",
        "source": "user",
        "workspace_bucket": "user-7-files",
        "grant_ids": [grant.grant_id],
    }


def test_direct_invoke_preserves_trial_exhaustion_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")

    async def exhausted(**_kwargs: object) -> dict[str, Any]:
        raise asgi_module._PlatformLLMGrantError(  # noqa: SLF001
            402,
            {
                "error": "llm_credentials_required",
                "reason": "platform_trial_exhausted",
                "setup_url": "https://app.a2acloud.io/llm-keys",
            },
        )

    monkeypatch.setattr(asgi_module, "_fetch_platform_llm_grant_from_cp", exhausted)
    client = TestClient(build_app(_DirectPlatformLLMAgent()))
    client.cookies.set("a2a_session", "session-token")

    response = client.post("/invoke/inspect", json={"arguments": {}})

    assert response.status_code == 402
    assert response.json()["detail"]["reason"] == "platform_trial_exhausted"
    assert response.json()["detail"]["setup_url"].endswith("/llm-keys")


def test_direct_invoke_ignores_body_cp_url_when_fetching_platform_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://cp-env.example.test")
    grant, token = mint_grant(
        issuer="self:user-9",
        audience="direct-platform-llm-agent",
        bucket="user-9-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        outputs_prefix="outputs/",
        write_prefixes=("outputs/",),
        llm_models=("platform-model",),
    )

    async def fake_fetch(
        *,
        cp_url: str,
        cp_token: str,
        agent_name: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        assert cp_url == "https://cp-env.example.test"
        assert cp_token == "body-cp-token"
        assert agent_name == "direct-platform-llm-agent"
        assert ttl_seconds == 870
        return {
            "grant": token,
            "grant_id": grant.grant_id,
            "expires_at": grant.expires_at,
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": token,
                "model": "platform-model",
                "temperature_mode": "omit",
                "extra_body": {},
            },
        }

    monkeypatch.setattr(asgi_module, "_fetch_platform_llm_grant_from_cp", fake_fetch)
    client = TestClient(build_app(_DirectPlatformLLMAgent()))

    response = client.post(
        "/invoke/inspect",
        json={
            "arguments": {},
            "cp_jwt": "body-cp-token",
            "cp_url": "http://169.254.169.254",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["grant_id"] == grant.grant_id
    assert body["result"] == {
        "api_key": token,
        "model": "platform-model",
        "source": "user",
        "workspace_bucket": "user-9-files",
        "grant_ids": [grant.grant_id],
    }


def test_raw_endpoint_fetches_platform_llm_grant_from_runtime_cp_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://cp.example.test")
    monkeypatch.setenv("A2A_CP_JWT", "runtime-cp-token")
    grant, token = mint_grant(
        issuer="self:user-11",
        audience="raw-platform-llm-agent",
        bucket="user-11-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        outputs_prefix="outputs/",
        write_prefixes=("outputs/",),
        llm_models=("platform-model",),
    )

    async def fake_fetch(
        *,
        cp_url: str,
        cp_token: str,
        agent_name: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        assert cp_url == "https://cp.example.test"
        assert cp_token == "runtime-cp-token"
        assert agent_name == "raw-platform-llm-agent"
        assert ttl_seconds == 870
        return {
            "grant": token,
            "grant_id": grant.grant_id,
            "expires_at": grant.expires_at,
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": token,
                "model": "platform-model",
                "temperature_mode": "omit",
                "extra_body": {},
            },
        }

    monkeypatch.setattr(asgi_module, "_fetch_platform_llm_grant_from_cp", fake_fetch)
    client = TestClient(build_app(_RawPlatformLLMAgent()))

    response = client.post(
        "/telegram/webhook",
        json={"message": {"text": "status"}},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "text": "status",
        "api_key": token,
        "model": "platform-model",
        "source": "user",
        "workspace_bucket": "user-11-files",
        "grant_ids": [grant.grant_id],
    }


def test_dual_invoke_accepts_caller_llm_creds() -> None:
    client = TestClient(build_app(_DualLLMAgent()))

    response = client.post(
        "/invoke/inspect_llm",
        json={
            "arguments": {},
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": "user-provider-key",
                "model": "user-selected-model",
                "extra_body": None,
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "api_key": "user-provider-key",
        "model": "user-selected-model",
        "source": "caller",
    }


def test_dual_invoke_accepts_platform_llm_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    _, token = mint_grant(
        issuer="control-plane",
        audience="dual-llm-agent",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_ONLY,
        llm_models=("platform-model",),
    )
    client = TestClient(build_app(_DualLLMAgent()))

    response = client.post(
        "/invoke/inspect_llm",
        json={
            "arguments": {},
            "grant": token,
            "llm_creds": {
                "base_url": "http://litellm:4000/v1",
                "api_key": token,
                "model": "platform-model",
                "extra_body": None,
            },
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "api_key": token,
        "model": "platform-model",
        "source": "caller",
    }
