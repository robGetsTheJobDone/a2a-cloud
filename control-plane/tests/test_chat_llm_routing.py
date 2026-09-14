from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import main_agent.config
from control_plane.routes import chat


@pytest.fixture(autouse=True)
def clear_litellm_ensure_cache() -> None:
    chat._litellm_ensured.clear()
    chat._litellm_ensure_locks.clear()
    yield
    chat._litellm_ensured.clear()
    chat._litellm_ensure_locks.clear()


@pytest.mark.asyncio
async def test_user_main_llm_routes_through_litellm(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_ensure_litellm_model(
        litellm_url: str,
        litellm_key: str,
        *,
        model_id: str,
        payload: dict[str, object],
    ) -> None:
        captured.update({
            "litellm_url": litellm_url,
            "litellm_key": litellm_key,
            "model_id": model_id,
            "payload": payload,
        })

    monkeypatch.setattr(chat, "_ensure_litellm_model", fake_ensure_litellm_model)
    monkeypatch.setattr(
        main_agent.config,
        "load_settings",
        lambda: SimpleNamespace(
            litellm_url="http://litellm.llm.svc.cluster.local:4000",
            litellm_key="platform-key",
        ),
    )

    runtime = await chat._main_llm_runtime_creds(
        {
            "base_url": "https://api.moonshot.ai/v1",
            "api_key": "user-key",
            "model": "kimi-k2.6",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        user_id=2,
        llm_creds_name="kimi-k2.6",
        runtime_litellm_key="org-litellm-key",
        litellm_metadata={"a2a_org_slug": "acme", "session_id": "thread-1"},
    )

    assert runtime is not None
    assert runtime["base_url"] == "http://litellm.llm.svc.cluster.local:4000/v1"
    assert runtime["api_key"] == "org-litellm-key"
    assert runtime["model"] == captured["model_id"]
    assert runtime["litellm_model_alias"] == captured["model_id"]
    assert runtime["extra_body"] == {"thinking": {"type": "disabled"}}
    assert runtime["metadata"] == {"a2a_org_slug": "acme", "session_id": "thread-1"}

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model_name"] == captured["model_id"]
    assert payload["litellm_params"] == {
        "model": "moonshot/kimi-k2.6",
        "api_base": "https://api.moonshot.ai/v1",
        "api_key": "user-key",
        "allowed_openai_params": ["thinking"],
    }


@pytest.mark.asyncio
async def test_user_main_llm_without_extra_body_routes_through_litellm(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_ensure_litellm_model(
        litellm_url: str,
        litellm_key: str,
        *,
        model_id: str,
        payload: dict[str, object],
    ) -> None:
        captured.update({"model_id": model_id, "payload": payload})

    monkeypatch.setattr(chat, "_ensure_litellm_model", fake_ensure_litellm_model)
    monkeypatch.setattr(
        main_agent.config,
        "load_settings",
        lambda: SimpleNamespace(litellm_url="http://litellm:4000", litellm_key="key"),
    )

    runtime = await chat._main_llm_runtime_creds(
        {
            "base_url": "https://api.example.com/v1",
            "api_key": "user-key",
            "model": "third-party-model",
            "extra_body": {},
        },
        user_id=2,
        llm_creds_name="third-party",
    )

    assert runtime is not None
    assert runtime["base_url"] == "http://litellm:4000/v1"
    assert runtime["api_key"] == "key"
    assert runtime["model"] == captured["model_id"]
    assert runtime["extra_body"] is None
    assert captured["payload"]["litellm_params"] == {
        "model": "openai/third-party-model",
        "api_base": "https://api.example.com/v1",
        "api_key": "user-key",
    }


@pytest.mark.asyncio
async def test_user_main_llm_anthropic_omits_api_base_for_native_route(monkeypatch) -> None:
    """LiteLLM appends /v1/messages to an anthropic api_base, so the stored
    OpenAI-compatible base_url (…/v1) must be omitted from the deployment."""
    captured: dict[str, object] = {}

    async def fake_ensure_litellm_model(
        litellm_url: str,
        litellm_key: str,
        *,
        model_id: str,
        payload: dict[str, object],
    ) -> None:
        captured.update({"model_id": model_id, "payload": payload})

    monkeypatch.setattr(chat, "_ensure_litellm_model", fake_ensure_litellm_model)
    monkeypatch.setattr(
        main_agent.config,
        "load_settings",
        lambda: SimpleNamespace(litellm_url="http://litellm:4000", litellm_key="key"),
    )

    runtime = await chat._main_llm_runtime_creds(
        {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "sk-ant-user",
            "model": "claude-sonnet-4-5",
            "extra_body": {},
        },
        user_id=2,
        llm_creds_name="claude",
    )

    assert runtime is not None
    # api_base must be explicitly present: LiteLLM's PATCH merges params, so
    # an omitted key would keep a stale api_base from a previous provider.
    # Anthropic gets the bare host — LiteLLM appends /v1/messages itself.
    assert captured["payload"]["litellm_params"] == {
        "model": "anthropic/claude-sonnet-4-5",
        "api_key": "sk-ant-user",
        "api_base": "https://api.anthropic.com",
    }


def test_litellm_deployment_payload_replaces_api_base_for_native_providers() -> None:
    for base_url, model, expected_model, expected_api_base in (
        ("https://api.anthropic.com", "claude-sonnet-4-5", "anthropic/claude-sonnet-4-5", "https://api.anthropic.com"),
        ("https://api.anthropic.com/v1", "claude-opus-4-8", "anthropic/claude-opus-4-8", "https://api.anthropic.com"),
        ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-pro", "gemini/gemini-2.5-pro", None),
        ("https://api.cohere.com/compatibility/v1", "command-a-03-2025", "cohere/command-a-03-2025", None),
    ):
        payload = chat._litellm_deployment_payload(
            "alias",
            {"base_url": base_url, "api_key": "k", "model": model},
            user_id=1,
            llm_creds_name="default",
        )
        assert payload["litellm_params"] == {
            "model": expected_model,
            "api_key": "k",
            "api_base": expected_api_base,
        }


def test_litellm_deployment_payload_uses_catalog_owned_provider_route() -> None:
    payload = chat._litellm_deployment_payload(
        "alias",
        {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key": "k",
            "model": "ollama/llama3",
            "litellm_model": "gemini/gemini-2.5-flash",
        },
        user_id=1,
        llm_creds_name="default",
    )

    assert payload["litellm_params"]["model"] == "gemini/gemini-2.5-flash"
    assert payload["litellm_params"]["api_base"] is None


def test_litellm_deployment_payload_drops_request_routing_overrides() -> None:
    creds = {
        "base_url": "https://api.openai.com/v1",
        "api_key": "curated-key",
        "model": "gpt-4o",
        "litellm_model": "openai/gpt-4o",
        "extra_body": {
            "model": "ollama/llama3",
            "api_base": "http://169.254.169.254",
            "api_key": "attacker-key",
            "custom_llm_provider": "ollama",
        },
    }

    payload = chat._litellm_deployment_payload(
        "alias",
        creds,
        user_id=1,
        llm_creds_name="default",
    )

    assert payload["litellm_params"] == {
        "model": "openai/gpt-4o",
        "api_key": "curated-key",
        "api_base": "https://api.openai.com/v1",
    }
    assert chat._main_llm_extra_body(creds) is None


def test_user_llm_audit_keeps_provider_model_name() -> None:
    usage = chat._main_llm_usage_context(
        {"model": "a2a-user-2-kimi-k2-6", "total_tokens": 42},
        {
            "base_url": "https://api.moonshot.ai/v1",
            "model": "kimi-k2.6",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        llm_creds_name="kimi-k2.6",
        litellm_model_alias="a2a-user-2-kimi-k2-6",
        run_id="run-1",
        status="complete",
        error=None,
    )

    assert usage["model"] == "kimi-k2.6"
    assert usage["provider"] == "moonshot"
    assert usage["metadata"]["litellm_model_alias"] == "a2a-user-2-kimi-k2-6"
    assert usage["metadata"]["llm_source"] == "user"


def test_litellm_provider_model_infers_known_openai_compatible_hosts() -> None:
    assert (
        chat._litellm_provider_model("grok-4", api_base="https://api.x.ai/v1")
        == "xai/grok-4"
    )
    assert (
        chat._litellm_provider_model(
            "llama-3.3-70b-versatile",
            api_base="https://api.groq.com/openai/v1",
        )
        == "groq/llama-3.3-70b-versatile"
    )
    assert (
        chat._litellm_provider_model(
            "openrouter/auto",
            api_base="https://openrouter.ai/api/v1",
        )
        == "openrouter/auto"
    )


@pytest.mark.asyncio
async def test_litellm_registration_updates_existing_model_before_create(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            assert timeout == 10.0

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> FakeResponse:
            calls.append(("post", url))
            assert headers == {"Authorization": "Bearer litellm-key"}
            assert json["model_name"] == "model-alias"
            return FakeResponse(200)

        async def patch(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> FakeResponse:
            calls.append(("patch", url))
            assert headers == {"Authorization": "Bearer litellm-key"}
            assert json["model_name"] == "model-alias"
            return FakeResponse(200)

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)

    await chat._ensure_litellm_model(
        "http://litellm.test",
        "litellm-key",
        model_id="model-alias",
        payload={"model_name": "model-alias"},
    )

    assert calls == [
        ("patch", "http://litellm.test/model/model-alias/update"),
    ]


@pytest.mark.asyncio
async def test_litellm_registration_singleflights_concurrent_model_ensure(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            assert timeout == 10.0

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> FakeResponse:
            calls.append(("post", url))
            return FakeResponse()

        async def patch(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> FakeResponse:
            calls.append(("patch", url))
            assert headers == {"Authorization": "Bearer litellm-key"}
            assert json["model_name"] == "model-alias"
            await asyncio.sleep(0)
            return FakeResponse()

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)

    await asyncio.gather(
        chat._ensure_litellm_model(
            "http://litellm.test",
            "litellm-key",
            model_id="model-alias",
            payload={"model_name": "model-alias"},
        ),
        chat._ensure_litellm_model(
            "http://litellm.test",
            "litellm-key",
            model_id="model-alias",
            payload={"model_name": "model-alias"},
        ),
    )

    assert calls == [
        ("patch", "http://litellm.test/model/model-alias/update"),
    ]


@pytest.mark.asyncio
async def test_litellm_registration_creates_model_when_updates_fail(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            assert timeout == 10.0

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> FakeResponse:
            calls.append(("post", url))
            assert headers == {"Authorization": "Bearer litellm-key"}
            assert json["model_name"] == "model-alias"
            return FakeResponse(200 if url.endswith("/model/new") else 404)

        async def patch(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> FakeResponse:
            calls.append(("patch", url))
            assert headers == {"Authorization": "Bearer litellm-key"}
            assert json["model_name"] == "model-alias"
            return FakeResponse(404)

    monkeypatch.setattr(chat.httpx, "AsyncClient", FakeClient)

    await chat._ensure_litellm_model(
        "http://litellm.test",
        "litellm-key",
        model_id="model-alias",
        payload={"model_name": "model-alias"},
    )

    assert calls == [
        ("patch", "http://litellm.test/model/model-alias/update"),
        ("post", "http://litellm.test/model/update"),
        ("post", "http://litellm.test/model/new"),
    ]
