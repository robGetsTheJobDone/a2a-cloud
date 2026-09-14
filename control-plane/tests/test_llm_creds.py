from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import json
import re
from pathlib import Path

from control_plane.routes import llm_creds as llm_creds_module
from control_plane.routes.llm_creds import (
    _GENERATED_PROVIDERS,
    _LITELLM_VERSION,
    _PROVIDERS_DATA_PATH,
    _build_catalog,
    _decrypt_api_key,
    _encrypt_api_key,
    _extra_body,
    _normalize_base_url,
    _require_allowed_llm_base_url,
    _require_allowed_llm_selection,
    _normalize_temperature,
    _verify_model_available,
)
from control_plane.safe_http import SafeHTTPError, SafeHTTPResponse

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_normalize_base_url_strips_openai_compatible_endpoint_suffixes() -> None:
    assert (
        _normalize_base_url("https://api.inceptionlabs.ai/v1/chat/completions")
        == "https://api.inceptionlabs.ai/v1"
    )
    assert (
        _normalize_base_url("https://api.example.com/v1/responses/")
        == "https://api.example.com/v1"
    )


def test_normalize_base_url_upgrades_bare_anthropic_host() -> None:
    # Rows saved before the catalog carried /v1 (and any bare-host input)
    # must come back as Anthropic's OpenAI-compatible endpoint.
    assert (
        _normalize_base_url("https://api.anthropic.com")
        == "https://api.anthropic.com/v1"
    )
    assert (
        _normalize_base_url("https://api.anthropic.com/")
        == "https://api.anthropic.com/v1"
    )
    assert (
        _normalize_base_url("https://api.anthropic.com/v1")
        == "https://api.anthropic.com/v1"
    )
    # Non-anthropic hosts keep their (stripped) URL untouched.
    assert _normalize_base_url("https://api.example.com") == "https://api.example.com"


def test_custom_llm_origins_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setattr(llm_creds_module.settings, "allow_custom_llm_base_urls", False)
    _require_allowed_llm_base_url("https://api.openai.com/v1")
    assert (
        _require_allowed_llm_selection("https://api.openai.com/v1", "gpt-4o")
        == "openai/gpt-4o"
    )

    with pytest.raises(HTTPException, match="curated HTTPS provider"):
        _require_allowed_llm_base_url("https://attacker.example/v1")
    with pytest.raises(HTTPException, match="curated HTTPS provider"):
        _require_allowed_llm_base_url("http://localhost:11434/v1")
    with pytest.raises(HTTPException, match="curated HTTPS provider"):
        _require_allowed_llm_base_url(
            "https://generativelanguage.googleapis.com/attacker-controlled"
        )
    with pytest.raises(HTTPException, match="curated catalog"):
        _require_allowed_llm_selection(
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "ollama/llama3",
        )


def test_custom_llm_origins_require_explicit_operator_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(llm_creds_module.settings, "allow_custom_llm_base_urls", True)
    _require_allowed_llm_base_url("https://self-hosted.example/v1")


def test_normalize_temperature_modes() -> None:
    assert _normalize_temperature("omit", 1.0) == ("omit", None)
    assert _normalize_temperature("default", 1.0) == ("default", None)
    assert _normalize_temperature("custom", 1.0) == ("custom", 1.0)

    with pytest.raises(HTTPException):
        _normalize_temperature("custom", None)


def test_extra_body_requires_object() -> None:
    assert _extra_body(None) == {}
    assert _extra_body({"thinking": {"type": "disabled"}}) == {
        "thinking": {"type": "disabled"}
    }

    with pytest.raises(HTTPException):
        _extra_body(["not", "an", "object"])


@pytest.mark.parametrize(
    "field",
    [
        "api_base",
        "base_url",
        "api_key",
        "model",
        "custom_llm_provider",
        "headers",
        "extra_headers",
    ],
)
def test_extra_body_rejects_routing_auth_and_header_overrides(field: str) -> None:
    with pytest.raises(HTTPException, match="unsupported fields") as exc_info:
        _extra_body({field: "attacker-controlled"})

    assert exc_info.value.status_code == 400


def test_api_key_encryption_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))

    stored = _encrypt_api_key("sk-test-secret")

    assert stored.startswith("fernet:")
    assert "sk-test-secret" not in stored
    assert _decrypt_api_key(stored) == "sk-test-secret"


def test_encrypt_requires_configured_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_CP_LLM_CREDS_KEY", raising=False)

    with pytest.raises(HTTPException):
        _encrypt_api_key("sk-test-secret")


def test_decrypt_accepts_legacy_plaintext_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_CP_LLM_CREDS_KEY", raising=False)

    assert _decrypt_api_key("legacy-key") == "legacy-key"


class _FakeModelsFetch:
    def __init__(self, *, status_code=200, ids=(), error=None):
        self._status = status_code
        self._ids = ids
        self._error = error
        self.requested: list[tuple[str, dict]] = []

    async def __call__(self, url, **kwargs):
        self.requested.append((url, kwargs))
        if self._error is not None:
            raise self._error
        return SafeHTTPResponse(
            status_code=self._status,
            url=url,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {"data": [{"id": model_id} for model_id in self._ids]}
            ).encode(),
        )


@pytest.mark.asyncio
async def test_verify_model_rejects_retired_model(monkeypatch) -> None:
    fake = _FakeModelsFetch(ids=("claude-opus-4-8", "claude-fable-5"))
    monkeypatch.setattr(llm_creds_module, "safe_fetch_url", fake)

    with pytest.raises(HTTPException) as err:
        await _verify_model_available(
            "https://api.anthropic.com/v1", "sk-ant-x", "claude-4-opus-20250514"
        )
    assert err.value.status_code == 400
    assert "claude-4-opus-20250514" in err.value.detail
    assert fake.requested[0][0] == "https://api.anthropic.com/v1/models"


@pytest.mark.asyncio
async def test_verify_model_accepts_listed_model(monkeypatch) -> None:
    fake = _FakeModelsFetch(ids=("claude-opus-4-8",))
    monkeypatch.setattr(llm_creds_module, "safe_fetch_url", fake)

    await _verify_model_available(
        "https://api.anthropic.com/v1", "sk-ant-x", "claude-opus-4-8"
    )


@pytest.mark.asyncio
async def test_verify_model_rejects_bad_key(monkeypatch) -> None:
    fake = _FakeModelsFetch(status_code=401)
    monkeypatch.setattr(llm_creds_module, "safe_fetch_url", fake)

    with pytest.raises(HTTPException) as err:
        await _verify_model_available("https://api.openai.com/v1", "bad", "gpt-5")
    assert err.value.status_code == 400
    assert "rejected" in err.value.detail


@pytest.mark.asyncio
async def test_verify_model_skips_when_endpoint_missing_or_down(monkeypatch) -> None:
    # 404: provider has no /models endpoint — never block the save.
    monkeypatch.setattr(
        llm_creds_module, "safe_fetch_url", _FakeModelsFetch(status_code=404)
    )
    await _verify_model_available("https://api.example.com/v1", "k", "some-model")

    # Network error: same.
    monkeypatch.setattr(
        llm_creds_module,
        "safe_fetch_url",
        _FakeModelsFetch(error=SafeHTTPError("connect timeout")),
    )
    await _verify_model_available("https://api.example.com/v1", "k", "some-model")


def test_generated_provider_catalog_is_litellm_derived() -> None:
    """The committed provider snapshot is generated from the litellm package
    (tools/gen_litellm_providers.py) and pinned to the proxy version."""
    raw = json.loads(_PROVIDERS_DATA_PATH.read_text())
    pin = re.search(
        r"litellm==([0-9][0-9.]*)",
        (_REPO_ROOT / "tools" / "requirements-providers.txt").read_text(),
    )
    assert pin is not None
    assert raw["litellm_version"] == pin.group(1) == _LITELLM_VERSION

    by_provider = {p["provider"]: p for p in _GENERATED_PROVIDERS}
    # Connection metadata for the popular providers comes straight from litellm.
    assert by_provider["moonshot"]["base_url"] == "https://api.moonshot.ai/v1"
    assert by_provider["moonshot"]["key_env"] == "MOONSHOT_API_KEY"
    assert by_provider["moonshot"]["openai_compatible"] is True
    assert by_provider["openai"]["base_url"] == "https://api.openai.com/v1"
    assert by_provider["anthropic"]["key_env"] == "ANTHROPIC_API_KEY"
    # Must be the OpenAI-compatible endpoint — the bare host 404s every
    # OpenAI-style consumer (agents, meta planner).
    assert by_provider["anthropic"]["base_url"] == "https://api.anthropic.com/v1"
    assert by_provider["groq"]["base_url"] == "https://api.groq.com/openai/v1"


def test_catalog_is_built_from_litellm_snapshot() -> None:
    catalog = _build_catalog()
    openai = next(p for p in catalog.providers if p.id == "openai")

    assert catalog.source == "litellm"
    # Connection metadata + models all come from the litellm-generated snapshot.
    assert openai.base_url == "https://api.openai.com/v1"
    assert openai.key_env == "OPENAI_API_KEY"
    assert len(openai.models) > 0
    assert not any(provider.id == "custom" for provider in catalog.providers)


def test_catalog_exposes_custom_provider_only_with_operator_opt_in(monkeypatch) -> None:
    monkeypatch.setattr(llm_creds_module.settings, "allow_custom_llm_base_urls", True)
    catalog = _build_catalog()
    custom = next(provider for provider in catalog.providers if provider.id == "custom")
    assert custom.base_url == ""
    assert catalog.providers[-1].id == "custom"


def test_catalog_models_carry_litellm_supported_params() -> None:
    catalog = _build_catalog()
    openai = next(p for p in catalog.providers if p.id == "openai")
    gpt4o = next(m for m in openai.models if m.model == "gpt-4o")
    o3 = next(m for m in openai.models if m.model == "o3")

    # Per-model request knobs come straight from litellm.
    assert "temperature" in gpt4o.supported_params
    assert "reasoning_effort" not in gpt4o.supported_params
    assert "reasoning_effort" in o3.supported_params
    assert o3.supports_reasoning is True

    anthropic = next(p for p in catalog.providers if p.provider == "anthropic")
    sonnet = next(m for m in anthropic.models if m.model == "claude-sonnet-4-5")
    assert "thinking" in sonnet.supported_params
    # Models past litellm's deprecation_date are filtered out of the catalog.
    assert not any(
        m.model == "claude-3-7-sonnet-20250219" for m in anthropic.models
    )
