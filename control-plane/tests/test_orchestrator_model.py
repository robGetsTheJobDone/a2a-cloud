from __future__ import annotations

from types import SimpleNamespace

from a2a_pack.deepagents import _a2a_deepagents_model_kwargs

from main_agent import orchestrator
from main_agent.config import Settings, load_settings


def _settings() -> Settings:
    return Settings(
        litellm_url="http://platform-litellm",
        litellm_key="platform-key",
        litellm_model="platform-model",
        sandbox_url="http://sandbox",
        sandbox_timeout_s=30.0,
        sandbox_token=None,
        minio_endpoint="http://minio",
        minio_access_key="minio",
        minio_secret_key="minio",
        cp_url="http://cp",
        agents_namespace_dns="{name}.agents.svc.cluster.local",
    )


def test_main_agent_graph_config_sets_recursion_limit() -> None:
    assert orchestrator.main_agent_graph_config() == {"recursion_limit": 300}
    assert orchestrator.main_agent_graph_config(thread_id="thread-1") == {
        "recursion_limit": 300,
        "configurable": {"thread_id": "thread-1"},
    }


def test_load_settings_prefers_internal_control_plane_url(monkeypatch) -> None:
    monkeypatch.setenv("A2A_CP_URL", "https://public.example.test")
    monkeypatch.setenv("A2A_CP_URL_INTERNAL", "http://control-plane.internal")

    assert load_settings().cp_url == "http://control-plane.internal"


def test_build_orchestrator_uses_per_request_llm_override(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_a2a_deep_agent(ctx, *, creds, **kwargs):
        captured["creds"] = creds
        captured["graph_kwargs"] = kwargs
        return kwargs

    monkeypatch.setattr(orchestrator, "create_a2a_deep_agent", fake_create_a2a_deep_agent)
    monkeypatch.setattr(orchestrator, "_build_workspace_backend", lambda ctx: None)

    ctx = orchestrator.OrchestratorContext.for_user(
        user_id=7,
        settings=_settings(),
        llm_base_url="https://user-llm.example/v1",
        llm_api_key="user-key",
        llm_model="user-model",
    )

    graph = orchestrator.build_orchestrator(ctx)

    assert "model" not in graph
    creds = captured["creds"]
    assert getattr(creds, "model") == "user-model"
    assert getattr(creds, "base_url") == "https://user-llm.example/v1"
    assert getattr(creds, "api_key") == "user-key"
    assert getattr(creds, "temperature_mode") == "default"
    assert getattr(creds, "temperature") is None
    assert getattr(creds, "extra_body") == {}


def test_build_orchestrator_can_omit_temperature(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_a2a_deep_agent(ctx, *, creds, **kwargs):
        captured["creds"] = creds
        return kwargs

    monkeypatch.setattr(orchestrator, "create_a2a_deep_agent", fake_create_a2a_deep_agent)
    monkeypatch.setattr(orchestrator, "_build_workspace_backend", lambda ctx: None)

    ctx = orchestrator.OrchestratorContext.for_user(
        user_id=7,
        settings=_settings(),
        llm_base_url="https://user-llm.example/v1",
        llm_api_key="user-key",
        llm_model="user-model",
        llm_temperature_enabled=False,
    )

    orchestrator.build_orchestrator(ctx)

    creds = captured["creds"]
    assert getattr(creds, "model") == "user-model"
    assert getattr(creds, "base_url") == "https://user-llm.example/v1"
    assert getattr(creds, "api_key") == "user-key"
    assert getattr(creds, "temperature_mode") == "omit"
    assert getattr(creds, "temperature") is None


def test_build_orchestrator_passes_extra_body(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_a2a_deep_agent(ctx, *, creds, **kwargs):
        captured["creds"] = creds
        return kwargs

    monkeypatch.setattr(orchestrator, "create_a2a_deep_agent", fake_create_a2a_deep_agent)
    monkeypatch.setattr(orchestrator, "_build_workspace_backend", lambda ctx: None)

    ctx = orchestrator.OrchestratorContext.for_user(
        user_id=7,
        settings=_settings(),
        llm_base_url="https://api.moonshot.ai/v1",
        llm_api_key="user-key",
        llm_model="kimi-k2.5",
        llm_temperature_enabled=False,
        llm_extra_body={"thinking": {"type": "disabled"}},
    )

    orchestrator.build_orchestrator(ctx)

    creds = captured["creds"]
    assert getattr(creds, "model") == "kimi-k2.5"
    assert getattr(creds, "base_url") == "https://api.moonshot.ai/v1"
    assert getattr(creds, "api_key") == "user-key"
    assert getattr(creds, "temperature_mode") == "omit"
    assert getattr(creds, "extra_body") == {"thinking": {"type": "disabled"}}


def test_installed_a2a_pack_forwards_litellm_metadata() -> None:
    creds = SimpleNamespace(
        base_url="http://litellm.llm.svc.cluster.local:4000/v1",
        api_key="scoped-key",
        model="gpt-5",
        temperature_mode="omit",
        temperature=None,
        extra_body={"thinking": {"type": "disabled"}},
        metadata={"session_id": "thread-1", "a2a_user_id": 7},
    )

    assert _a2a_deepagents_model_kwargs(creds)["extra_body"] == {
        "thinking": {"type": "disabled"},
        "metadata": {"session_id": "thread-1", "a2a_user_id": 7},
    }
