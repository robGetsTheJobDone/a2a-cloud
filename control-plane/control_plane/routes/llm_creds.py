"""User-managed LLM credentials.

Caller-provided agents (``Card.runtime.llm_provisioning=caller_provided``)
need the user's LLM endpoint + key. This endpoint manages them per-user.

API keys are encrypted at rest with a Fernet key from
``A2A_CP_LLM_CREDS_KEY``. Legacy plaintext rows are still readable so a
rolling deploy can migrate naturally on the next update.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..config import settings
from ..db import get_session
from ..models import User, UserLLMCreds
from ..safe_http import (
    SafeHTTPError,
    UnsafeURLError,
    safe_fetch_url,
    validate_public_url,
)
from ..secret_crypto import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/v1/me/llm-creds", tags=["llm-creds"])

_CRED_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_MAX_MODELS_RESPONSE_BYTES = 2 * 1024 * 1024
_ALLOWED_EXTRA_BODY_KEYS = frozenset(
    {"max_tokens", "top_p", "reasoning_effort", "thinking"}
)


# BYOK provider catalog is generated from the litellm package itself — see
# tools/gen_litellm_providers.py. Provider identity, base_url, key env var,
# the openai-compatible flag, and the snapshot model list all come from
# litellm; nothing here is hand-curated. The live cost map (when the proxy is
# reachable) refreshes the per-provider model lists on top of the snapshot.
_PROVIDERS_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "litellm_providers.json"
)

# The free-form "Custom / LiteLLM" entry is not a litellm provider — it lets a
# user point at any OpenAI-compatible endpoint and type a model name.
_CUSTOM_PROVIDER: dict[str, Any] = {
    "id": "custom",
    "label": "Custom / OpenAI-compatible",
    "provider": "custom",
    "base_url": "",
    "openai_compatible": True,
    "key_env": "",
    "default_model": "",
    "models": [],
}


def _load_generated_providers() -> tuple[str, list[dict[str, Any]]]:
    try:
        raw = json.loads(_PROVIDERS_DATA_PATH.read_text())
    except (OSError, ValueError):
        return "", []
    providers = raw.get("providers")
    if not isinstance(providers, list):
        return "", []
    return str(raw.get("litellm_version") or ""), providers


_LITELLM_VERSION, _GENERATED_PROVIDERS = _load_generated_providers()


def _curated_provider_base_urls() -> frozenset[str]:
    base_urls: set[str] = set()
    for provider in _GENERATED_PROVIDERS:
        base_url = str(provider.get("base_url") or "").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme == "https" and parsed.hostname and parsed.port in {None, 443}:
            base_urls.add(base_url)
    return frozenset(base_urls)


_CURATED_PROVIDER_BASE_URLS = _curated_provider_base_urls()


class _CredsIn(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=64)
    base_url: str = Field(..., min_length=4)
    api_key: str = Field(..., min_length=4)
    model: str = Field(..., min_length=1)
    temperature_mode: Literal["omit", "default", "custom"] = "omit"
    temperature: float | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)


class CredsOut(BaseModel):
    id: int
    name: str
    base_url: str
    model: str
    temperature_mode: str
    temperature: float | None
    extra_body: dict[str, Any]
    api_key_redacted: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LLMModelOption(BaseModel):
    model: str
    litellm_model: str
    provider: str
    mode: str
    input_cost_per_million_tokens: float | None = None
    output_cost_per_million_tokens: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    supports_function_calling: bool | None = None
    supports_reasoning: bool | None = None
    supports_web_search: bool | None = None
    # litellm's supported request knobs for this model, filtered to the ones the
    # form renders as controls (temperature, max_tokens, top_p, reasoning_effort,
    # thinking). Drives which option widgets appear when the model is selected.
    supported_params: list[str] = Field(default_factory=list)


class LLMProviderOption(BaseModel):
    id: str
    label: str
    provider: str
    base_url: str
    default_model: str
    key_env: str = ""
    openai_compatible: bool = False
    extra_body: dict[str, Any] = Field(default_factory=dict)
    models: list[LLMModelOption] = Field(default_factory=list)


class LLMModelCatalogOut(BaseModel):
    source: str
    providers: list[LLMProviderOption]
    total_models: int
    generated_at: datetime


def _redact(key: str) -> str:
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def _encrypt_api_key(api_key: str) -> str:
    return encrypt_secret(api_key)


def _decrypt_api_key(stored: str) -> str:
    try:
        return decrypt_secret(stored)
    except HTTPException as exc:
        if exc.status_code == 500 and "stored secret" in str(exc.detail):
            raise HTTPException(
                500, "stored LLM credential could not be decrypted"
            ) from exc
        raise


def _to_out(row: UserLLMCreds) -> CredsOut:
    return CredsOut(
        id=row.id,
        name=row.name,
        base_url=_normalize_base_url(row.base_url),
        model=row.model,
        temperature_mode=_temperature_mode(getattr(row, "temperature_mode", None)),
        temperature=getattr(row, "temperature", None),
        extra_body=_extra_body(getattr(row, "extra_body", None)),
        api_key_redacted=_redact(_decrypt_api_key(row.api_key)),
        created_at=row.created_at, updated_at=row.updated_at,
    )


@router.get("", response_model=list[CredsOut])
async def list_creds(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CredsOut]:
    rows = (
        await session.execute(
            select(UserLLMCreds).where(UserLLMCreds.user_id == user.id)
            .order_by(UserLLMCreds.id)
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/catalog", response_model=LLMModelCatalogOut)
async def llm_model_catalog(
    _user: User = Depends(current_user),
) -> LLMModelCatalogOut:
    return _catalog()


@router.post("", response_model=CredsOut, status_code=201)
async def upsert_creds(
    body: _CredsIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CredsOut:
    """Create OR replace by ``name``. Most users only have one entry
    named ``"default"``; opinionated devs can have several (e.g.
    one each for ``openai`` / ``anthropic`` / ``ollama``)."""
    name = _validate_cred_name(body.name)
    base_url = _normalize_base_url(body.base_url)
    _require_allowed_llm_base_url(base_url)
    model = body.model.strip()
    api_key = body.api_key.strip()
    temperature_mode, temperature = _normalize_temperature(
        body.temperature_mode, body.temperature,
    )
    extra_body = _extra_body(body.extra_body)
    if not model:
        raise HTTPException(400, "model is required")
    _require_allowed_llm_selection(base_url, model)
    if not api_key:
        raise HTTPException(400, "api_key is required")
    await _verify_model_available(base_url, api_key, model)

    existing = (
        await session.execute(
            select(UserLLMCreds).where(
                UserLLMCreds.user_id == user.id,
                UserLLMCreds.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.base_url = base_url
        existing.api_key = _encrypt_api_key(api_key)
        existing.model = model
        existing.temperature_mode = temperature_mode
        existing.temperature = temperature
        existing.extra_body = extra_body
        row = existing
    else:
        row = UserLLMCreds(
            user_id=user.id, name=name,
            base_url=base_url, api_key=_encrypt_api_key(api_key), model=model,
            temperature_mode=temperature_mode, temperature=temperature,
            extra_body=extra_body,
        )
        session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(400, f"db error: {exc}") from exc
    await session.refresh(row)
    return _to_out(row)


@router.delete("/{name}", status_code=204)
async def delete_creds(
    name: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    name = _validate_cred_name(name)
    row = (
        await session.execute(
            select(UserLLMCreds).where(
                UserLLMCreds.user_id == user.id, UserLLMCreds.name == name,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "no such creds")
    await session.delete(row)
    await session.commit()


# Internal helper used by the orchestrator's call_agent tool — NOT a route.
# Returns the user's default LLM creds for forwarding to a callee, or None
# if they haven't registered any.
async def get_creds_for_user(
    user_id: int, session: AsyncSession, *, name: str = "default",
) -> dict[str, Any] | None:
    """Look up the named creds row for ``user_id``. Returns the raw dict
    with the bare api_key — for use ONLY by trusted in-process callers
    (the orchestrator's handoff hook), never serialized to the dashboard.
    """
    row = (
        await session.execute(
            select(UserLLMCreds).where(
                UserLLMCreds.user_id == user_id,
                UserLLMCreds.name == name,
            )
        )
    ).scalar_one_or_none()
    if row is None and name != "default":
        # Asked-for name doesn't exist — fall back to the user's default so
        # an orchestrator request doesn't silently drop creds because of a
        # stale dashboard selection.
        return await get_creds_for_user(user_id, session, name="default")
    if row is None:
        return None
    base_url = _normalize_base_url(row.base_url)
    _require_allowed_llm_base_url(base_url)
    litellm_model = _require_allowed_llm_selection(base_url, row.model)
    try:
        await validate_public_url(base_url)
    except UnsafeURLError as exc:
        raise HTTPException(
            400, "stored LLM credential base_url is not publicly routable"
        ) from exc
    except SafeHTTPError as exc:
        raise HTTPException(
            503, "stored LLM credential base_url could not be safely resolved"
        ) from exc
    return {
        "base_url": base_url,
        "api_key": _decrypt_api_key(row.api_key),
        "model": row.model,
        "litellm_model": litellm_model,
        "temperature_mode": _temperature_mode(getattr(row, "temperature_mode", None)),
        "temperature": getattr(row, "temperature", None),
        "extra_body": _extra_body(getattr(row, "extra_body", None)),
    }


# Back-compat alias used by older imports.
get_default_for_user = get_creds_for_user


async def _verify_model_available(base_url: str, api_key: str, model: str) -> None:
    """Reject creds whose key or model the provider itself rejects.

    The catalog is litellm-derived and litellm's retirement data is
    incomplete (it offered the retired claude-4-opus-20250514; every call
    then 404ed at Anthropic). The provider's own ``GET {base_url}/models``
    is the ground truth and free, so check the picked model against it and
    surface auth failures immediately. Providers that don't implement the
    endpoint (or any network hiccup) skip the check — this must never
    block saving creds for a working-but-nonstandard endpoint.
    """
    try:
        response = await safe_fetch_url(
            f"{base_url}/models",
            headers={"accept": "application/json"},
            sensitive_headers={"Authorization": f"Bearer {api_key}"},
            max_response_bytes=_MAX_MODELS_RESPONSE_BYTES,
            timeout_seconds=8.0,
        )
    except UnsafeURLError as exc:
        raise HTTPException(400, "base_url must resolve to a public IP address") from exc
    except SafeHTTPError:  # unreachable endpoint != invalid credentials
        return
    if response.status_code in (401, 403):
        raise HTTPException(400, "the provider rejected this API key")
    if response.status_code != 200:
        return
    try:
        listed = json.loads(response.text).get("data") or []
    except ValueError:
        return
    ids = {str(entry.get("id") or "") for entry in listed if isinstance(entry, dict)}
    if not ids or model in ids:
        return
    raise HTTPException(
        400,
        f"model {model!r} is not available for this key — the provider lists: "
        + ", ".join(sorted(ids)[:20]),
    )


def _validate_cred_name(raw: str) -> str:
    name = raw.strip()
    if not _CRED_NAME_RE.match(name):
        raise HTTPException(
            400,
            "credential name must use only letters, numbers, dots, dashes, or underscores",
        )
    return name


def _normalize_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(400, "base_url must not include credentials")
    if parsed.params or parsed.query or parsed.fragment:
        raise HTTPException(400, "base_url must not include params, query, or fragment")
    for suffix in (
        "/chat/completions",
        "/completions",
        "/responses",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    # Anthropic's OpenAI-compatible endpoint lives under /v1; a bare host (the
    # pre-fix catalog default, still present in stored rows) 404s every
    # OpenAI-style consumer, so upgrade it here on both write and read.
    stripped = urlparse(value)
    if stripped.hostname == "api.anthropic.com" and stripped.path.rstrip("/") == "":
        return f"{stripped.scheme}://{stripped.netloc}/v1"
    return value


def _require_allowed_llm_base_url(base_url: str) -> None:
    if settings.allow_custom_llm_base_urls:
        return
    if base_url not in _CURATED_PROVIDER_BASE_URLS:
        raise HTTPException(
            400,
            "custom LLM base URLs are disabled; choose a curated HTTPS provider",
        )


def _require_allowed_llm_selection(base_url: str, model: str) -> str:
    """Return the catalog-owned LiteLLM route for an exact provider/model pair."""
    if settings.allow_custom_llm_base_urls:
        return model
    for provider in _GENERATED_PROVIDERS:
        if str(provider.get("base_url") or "").rstrip("/") != base_url:
            continue
        provider_name = str(provider.get("provider") or "").strip()
        raw_models = provider.get("models")
        if not provider_name or not isinstance(raw_models, list):
            break
        for raw_model in raw_models:
            if not isinstance(raw_model, dict) or str(raw_model.get("model") or "") != model:
                continue
            litellm_model = str(raw_model.get("litellm_model") or model).strip()
            if not litellm_model.startswith(f"{provider_name}/"):
                litellm_model = f"{provider_name}/{litellm_model}"
            return litellm_model
        break
    raise HTTPException(
        400,
        "model is not in the curated catalog for the selected provider",
    )


def _temperature_mode(raw: str | None) -> str:
    return raw if raw in {"omit", "default", "custom"} else "omit"


def _extra_body(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "extra_body must be a JSON object")
    rejected = sorted(str(key) for key in raw if key not in _ALLOWED_EXTRA_BODY_KEYS)
    if rejected:
        raise HTTPException(
            400,
            "extra_body contains unsupported fields: " + ", ".join(rejected),
        )
    return dict(raw)


_CATALOG: LLMModelCatalogOut | None = None


def _catalog() -> LLMModelCatalogOut:
    """The litellm-generated provider catalog. Built once from the committed
    snapshot (tools/gen_litellm_providers.py), which is pinned to the proxy's
    litellm version — so it is authoritative and needs no live refresh."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _build_catalog()
    return _CATALOG


def _build_catalog() -> LLMModelCatalogOut:
    providers: list[LLMProviderOption] = []
    candidates = list(_GENERATED_PROVIDERS)
    if settings.allow_custom_llm_base_urls:
        candidates.append(_CUSTOM_PROVIDER)
    for raw in candidates:
        raw_base_url = str(raw.get("base_url") or "")
        if not settings.allow_custom_llm_base_urls:
            if raw_base_url.rstrip("/") not in _CURATED_PROVIDER_BASE_URLS:
                continue
        provider = str(raw.get("provider") or "")
        models = _dedupe_models(_models_from_snapshot(raw.get("models")))
        models.sort(key=lambda item: item.model.lower())
        default_model = str(raw.get("default_model") or "")
        if not default_model and models:
            default_model = models[0].model
        providers.append(LLMProviderOption(
            id=str(raw.get("id") or _provider_id(provider)),
            label=str(raw.get("label") or _provider_label(provider)),
            provider=provider,
            base_url=raw_base_url,
            default_model=default_model,
            key_env=str(raw.get("key_env") or ""),
            openai_compatible=bool(raw.get("openai_compatible")),
            extra_body={},
            models=models,
        ))
    return LLMModelCatalogOut(
        source="litellm",
        providers=providers,
        total_models=sum(len(provider.models) for provider in providers),
        generated_at=datetime.utcnow(),
    )


def _models_from_snapshot(raw_models: Any) -> list[LLMModelOption]:
    if not isinstance(raw_models, list):
        return []
    fields = set(LLMModelOption.model_fields)
    out: list[LLMModelOption] = []
    for item in raw_models:
        if isinstance(item, dict):
            out.append(LLMModelOption(**{k: v for k, v in item.items() if k in fields}))
    return out


def _provider_id(provider: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", provider.strip().lower()).strip("-")
    return value or "provider"


def _provider_label(provider: str) -> str:
    return " ".join(part for part in re.split(r"[_\-/]+", provider) if part).title()


def _dedupe_models(models: list[LLMModelOption]) -> list[LLMModelOption]:
    seen: set[str] = set()
    out: list[LLMModelOption] = []
    for model in models:
        key = model.model.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(model)
    return out


def _normalize_temperature(
    mode: str,
    temperature: float | None,
) -> tuple[str, float | None]:
    normalized = _temperature_mode(mode)
    if normalized == "omit":
        return normalized, None
    if normalized == "default":
        return normalized, None
    if temperature is None:
        raise HTTPException(400, "temperature is required for custom mode")
    value = float(temperature)
    if value < 0 or value > 2:
        raise HTTPException(400, "temperature must be between 0 and 2")
    return normalized, value
