"""Generate the litellm-derived BYOK provider catalog.

The dashboard "Add LLM key" form is driven entirely by what litellm itself
knows about each provider — there is no hand-maintained provider table. This
script imports the *pinned* litellm package (see requirements-providers.txt,
which must match the proxy image tag in runtime-llm/litellm.yaml) and emits
``control_plane/data/litellm_providers.json``.

Everything in the output is sourced from litellm:

* provider identity .......... ``litellm.provider_list`` / ``LlmProviders``
* openai-compatible flag ...... ``litellm.openai_compatible_providers``
* base_url ................... ``litellm.get_llm_provider`` (+ the native
                               openai/anthropic config defaults)
* key env var ................ litellm's ``<PROVIDER>_API_KEY`` convention,
                               confirmed against each provider config's
                               ``validate_environment`` source
* models / costs / caps ...... ``litellm.model_cost``

Regenerate on a litellm version bump:

    python -m venv .venv && .venv/bin/pip install -r requirements-providers.txt
    .venv/bin/python gen_litellm_providers.py

CI asserts the committed JSON matches a fresh run (see test_llm_creds.py).
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import Any

import litellm
from litellm import get_llm_provider, get_supported_openai_params
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager

# Request knobs we render as structured form controls. Each model only exposes
# the subset litellm reports it accepts (get_supported_openai_params), so the
# UI can show exactly the right controls per model instead of a free JSON box.
_UI_PARAMS = (
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "reasoning_effort",
    "thinking",
)

OUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "control_plane"
    / "data"
    / "litellm_providers.json"
)

# Native key-based chat providers litellm handles with a dedicated SDK path
# rather than the generic openai-compatible route. They are not in
# ``openai_compatible_providers`` but are still plain base_url + api_key from a
# BYOK perspective, so we surface them too.
_NATIVE_KEY_PROVIDERS = (
    "openai",
    "anthropic",
    "gemini",
    "cohere",
    "openrouter",
    "mistral",
    "ollama",
)

# Providers that are not a static base_url + api_key from a BYOK perspective:
# their litellm config performs interactive auth or network I/O on probe
# (e.g. github_copilot runs a GitHub device-code flow). Excluded so the
# generator stays offline and deterministic.
_EXCLUDE = frozenset({"github_copilot", "chatgpt"})

_CHAT_MODES = {"chat", "completion"}


def _provider_keys() -> list[str]:
    """All litellm provider keys we expose in the BYOK form: every
    openai-compatible provider plus the native key-based chat providers."""
    keys: list[str] = []
    seen: set[str] = set()
    for value in (*litellm.openai_compatible_providers, *_NATIVE_KEY_PROVIDERS):
        key = str(value)
        if key not in seen and key not in _EXCLUDE:
            seen.add(key)
            keys.append(key)
    return keys


def _base_url(provider: str) -> str:
    """litellm's known OpenAI-compatible base URL for ``provider``, or "".

    ``get_llm_provider`` returns the real base for openai-compatible providers
    and ``None`` for the native ones; for openai/anthropic we read the value
    their config class exposes (it is the genuine default, not a fallback)."""
    try:
        _model, _prov, _key, base = get_llm_provider(model=f"{provider}/_probe_")
    except Exception:
        base = None
    if base:
        return str(base).rstrip("/")
    # litellm's documented OpenAI-compatible endpoints for native providers it
    # routes through a dedicated SDK, so ``get_llm_provider`` returns no base.
    # (https://docs.litellm.ai/docs/providers/<provider>). Without these the
    # most popular BYOK providers would need a hand-typed base URL.
    native_default = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com/v1",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "cohere": "https://api.cohere.com/compatibility/v1",
        "openrouter": "https://openrouter.ai/api/v1",
    }
    return native_default.get(provider, "")


def _key_env(provider: str) -> str:
    """litellm's ``<PROVIDER>_API_KEY`` env var for ``provider``.

    We derive it from the provider key (litellm's own convention) and confirm
    it appears in the provider config's ``validate_environment`` source so we
    never emit an env var litellm doesn't actually read."""
    candidate = f"{provider.upper().replace('-', '_')}_API_KEY"
    found = _env_vars_in_config(provider)
    if candidate in found:
        return candidate
    # Pick the provider-specific *_API_KEY litellm reads, ignoring the generic
    # OPENAI_API_KEY fallback that every openai-compatible config inherits.
    specific = sorted(env for env in found if env != "OPENAI_API_KEY")
    if specific:
        return specific[0]
    return candidate


def _env_vars_in_config(provider: str) -> set[str]:
    try:
        config = ProviderConfigManager.get_provider_chat_config(
            model="_probe_", provider=LlmProviders(provider)
        )
    except Exception:
        return set()
    if config is None:
        return set()
    envs: set[str] = set()
    for meth in ("validate_environment", "get_api_key"):
        fn = getattr(config, meth, None)
        if fn is None:
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        envs |= set(re.findall(r"['\"]([A-Z0-9_]*API_KEY[A-Z0-9_]*)['\"]", src))
    return envs


def _label(provider: str) -> str:
    overrides = {
        "openai": "OpenAI",
        "xai": "xAI",
        "ai21": "AI21",
        "ai21_chat": "AI21",
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "moonshot": "Moonshot (Kimi)",
        "nvidia_nim": "NVIDIA NIM",
        "nscale": "Nscale",
        "v0": "v0",
        "aiml": "AI/ML API",
    }
    if provider in overrides:
        return overrides[provider]
    return " ".join(part for part in re.split(r"[_\-/]+", provider) if part).title()


def _provider_id(provider: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", provider.strip().lower()).strip("-")
    return value or "provider"


def _cost_per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _supported_params(model: str, provider: str) -> list[str]:
    """litellm's supported OpenAI request params for this model, filtered to
    the knobs we render as controls."""
    try:
        supported = get_supported_openai_params(
            model=model, custom_llm_provider=provider
        ) or []
    except Exception:
        return []
    present = {str(p) for p in supported}
    return [p for p in _UI_PARAMS if p in present]


def _is_deprecated(info: dict[str, Any]) -> bool:
    """Model past its litellm ``deprecation_date`` at generation time.

    Retired models 404 at the provider (a user picked the retired
    claude-4-opus-20250514 from the catalog and every call failed), so they
    must not be offered. litellm's field is incomplete — models it misses
    still appear — but the known fossils get dropped."""
    raw = str(info.get("deprecation_date") or "").strip()
    if not raw:
        return False
    try:
        return date.fromisoformat(raw) <= date.today()
    except ValueError:
        return False


def _models_by_provider() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for raw_model, info in litellm.model_cost.items():
        if raw_model == "sample_spec" or not isinstance(info, dict):
            continue
        mode = str(info.get("mode") or "")
        if mode not in _CHAT_MODES:
            continue
        provider = str(info.get("litellm_provider") or "").strip()
        if not provider:
            continue
        if _is_deprecated(info):
            continue
        prefix = f"{provider}/"
        display = raw_model[len(prefix):] if raw_model.startswith(prefix) else raw_model
        out.setdefault(provider, []).append({
            "model": display,
            "litellm_model": raw_model,
            "provider": provider,
            "mode": mode,
            "input_cost_per_million_tokens": _cost_per_million(info.get("input_cost_per_token")),
            "output_cost_per_million_tokens": _cost_per_million(info.get("output_cost_per_token")),
            "max_input_tokens": _optional_int(info.get("max_input_tokens")),
            "max_output_tokens": _optional_int(info.get("max_output_tokens")),
            "supports_vision": _optional_bool(info.get("supports_vision")),
            "supports_function_calling": _optional_bool(info.get("supports_function_calling")),
            "supports_reasoning": _optional_bool(info.get("supports_reasoning")),
            "supports_web_search": _optional_bool(info.get("supports_web_search")),
            "supported_params": _supported_params(display, provider),
        })
    for models in out.values():
        models.sort(key=lambda item: item["model"].lower())
    return out


def build() -> dict[str, Any]:
    models_by_provider = _models_by_provider()
    openai_compatible = {str(p) for p in litellm.openai_compatible_providers}
    providers: list[dict[str, Any]] = []
    for provider in _provider_keys():
        models = models_by_provider.get(provider, [])
        providers.append({
            "id": _provider_id(provider),
            "label": _label(provider),
            "provider": provider,
            "base_url": _base_url(provider),
            "openai_compatible": provider in openai_compatible,
            "key_env": _key_env(provider),
            "default_model": models[0]["model"] if models else "",
            "models": models,
        })
    # Real providers (with a known model catalog) first, then the rest
    # alphabetically; both groups stay deterministic for a clean diff.
    providers.sort(key=lambda p: (not p["models"], p["label"].lower()))
    return {
        "litellm_version": metadata.version("litellm"),
        "providers": providers,
    }


def main() -> None:
    catalog = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=False) + "\n")
    real = sum(1 for p in catalog["providers"] if p["models"])
    print(
        f"litellm {catalog['litellm_version']}: "
        f"{len(catalog['providers'])} providers ({real} with models) -> {OUT_PATH}"
    )


if __name__ == "__main__":
    main()
