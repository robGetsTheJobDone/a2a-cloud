from __future__ import annotations

from types import SimpleNamespace

from a2a_pack.deepagents import (
    _a2a_deepagents_model_kwargs,
    _a2a_deepagents_model_ref,
)


def test_deepagents_model_ref_uses_openai_compatible_litellm_alias() -> None:
    creds = SimpleNamespace(model="a2a-user-7-default-abcd1234")

    assert _a2a_deepagents_model_ref(creds) == "openai:a2a-user-7-default-abcd1234"


def test_deepagents_model_ref_preserves_provider_model_string() -> None:
    creds = SimpleNamespace(model="google_genai:gemini-3.5-flash")

    assert _a2a_deepagents_model_ref(creds) == "google_genai:gemini-3.5-flash"


def test_deepagents_model_kwargs_preserve_runtime_llm_fields() -> None:
    creds = SimpleNamespace(
        base_url="http://litellm.test/v1",
        api_key="scoped-key",
        temperature_mode="custom",
        temperature=0.4,
        extra_body={"thinking": {"type": "enabled"}},
    )

    assert _a2a_deepagents_model_kwargs(creds) == {
        "base_url": "http://litellm.test/v1",
        "api_key": "scoped-key",
        "stream_usage": True,
        "temperature": 0.4,
        "extra_body": {"thinking": {"type": "enabled"}},
    }


def test_deepagents_model_kwargs_add_metadata_for_litellm() -> None:
    creds = SimpleNamespace(
        base_url="http://litellm.test/v1",
        api_key="scoped-key",
        temperature_mode="omit",
        temperature=None,
        extra_body={"thinking": {"type": "disabled"}},
        metadata={"a2a_grant_id": "grant-1", "session_id": "thread-1"},
    )

    assert _a2a_deepagents_model_kwargs(creds)["extra_body"] == {
        "thinking": {"type": "disabled"},
        "metadata": {"a2a_grant_id": "grant-1", "session_id": "thread-1"},
    }


def test_deepagents_model_kwargs_do_not_add_metadata_for_provider_url() -> None:
    creds = SimpleNamespace(
        base_url="https://api.moonshot.ai/v1",
        api_key="provider-key",
        temperature_mode="omit",
        temperature=None,
        extra_body={"thinking": {"type": "disabled"}},
        metadata={"a2a_grant_id": "grant-1"},
    )

    assert _a2a_deepagents_model_kwargs(creds)["extra_body"] == {
        "thinking": {"type": "disabled"},
    }


def test_deepagents_model_kwargs_can_omit_temperature() -> None:
    creds = SimpleNamespace(
        base_url="http://litellm.test/v1",
        api_key="scoped-key",
        temperature_mode="omit",
        temperature=1.0,
        extra_body={},
    )

    assert _a2a_deepagents_model_kwargs(creds) == {
        "base_url": "http://litellm.test/v1",
        "api_key": "scoped-key",
        "stream_usage": True,
    }


def test_deepagents_model_kwargs_do_not_inject_temperature_by_default() -> None:
    creds = SimpleNamespace(
        base_url="http://litellm.test/v1",
        api_key="scoped-key",
        temperature_mode="default",
        temperature=None,
        extra_body={},
    )

    assert _a2a_deepagents_model_kwargs(creds) == {
        "base_url": "http://litellm.test/v1",
        "api_key": "scoped-key",
        "stream_usage": True,
    }


def test_deepagents_model_kwargs_accept_default_temperature() -> None:
    creds = SimpleNamespace(
        base_url="http://litellm.test/v1",
        api_key="scoped-key",
        temperature_mode="default",
        temperature=None,
        extra_body={},
    )

    assert _a2a_deepagents_model_kwargs(creds, default_temperature=0.3) == {
        "base_url": "http://litellm.test/v1",
        "api_key": "scoped-key",
        "stream_usage": True,
        "temperature": 0.3,
    }
