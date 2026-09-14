"""Credentials store at ``~/.a2a/credentials.json``."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .platform import DEFAULT_PLATFORM_DOMAIN

#: API of the public hosted instance; see ``platform.py`` for derivation rules.
DEFAULT_API_URL = f"https://api.{DEFAULT_PLATFORM_DOMAIN}"


def _config_dir() -> Path:
    return Path.home() / ".a2a"


def _creds_path() -> Path:
    return _config_dir() / "credentials.json"


@dataclass
class Credentials:
    api_url: str
    token: str
    email: str
    user_id: int | None = None
    bucket: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    oauth_issuer: str | None = None
    client_id: str | None = None
    scope: str | None = None


@dataclass
class LocalLLMCredentials:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"


def _read() -> dict[str, object]:
    path = _creds_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, object]) -> Path:
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = _creds_path()
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


def save(
    api_url: str,
    token: str,
    email: str,
    *,
    user_id: int | None = None,
    bucket: str | None = None,
    refresh_token: str | None = None,
    expires_at: int | None = None,
    oauth_issuer: str | None = None,
    client_id: str | None = None,
    scope: str | None = None,
) -> Path:
    data = _read()
    data.update({"api_url": api_url, "token": token, "email": email})
    if user_id is not None:
        data["user_id"] = user_id
    if bucket is not None:
        data["bucket"] = bucket
    if refresh_token is not None:
        data["refresh_token"] = refresh_token
    if expires_at is not None:
        data["expires_at"] = expires_at
    if oauth_issuer is not None:
        data["oauth_issuer"] = oauth_issuer
    if client_id is not None:
        data["client_id"] = client_id
    if scope is not None:
        data["scope"] = scope
    return _write(data)


def save_credentials(creds: Credentials) -> Path:
    return save(
        creds.api_url,
        creds.token,
        creds.email,
        user_id=creds.user_id,
        bucket=creds.bucket,
        refresh_token=creds.refresh_token,
        expires_at=creds.expires_at,
        oauth_issuer=creds.oauth_issuer,
        client_id=creds.client_id,
        scope=creds.scope,
    )


def load() -> Credentials | None:
    data = _read()
    token = data.get("token")
    if not token:
        return None
    return Credentials(
        api_url=str(data.get("api_url") or DEFAULT_API_URL),
        token=str(token),
        email=str(data.get("email") or ""),
        user_id=data.get("user_id") if isinstance(data.get("user_id"), int) else None,
        bucket=str(data["bucket"]) if isinstance(data.get("bucket"), str) else None,
        refresh_token=(
            str(data["refresh_token"]) if isinstance(data.get("refresh_token"), str) else None
        ),
        expires_at=(
            int(data["expires_at"]) if isinstance(data.get("expires_at"), int) else None
        ),
        oauth_issuer=(
            str(data["oauth_issuer"]) if isinstance(data.get("oauth_issuer"), str) else None
        ),
        client_id=str(data["client_id"]) if isinstance(data.get("client_id"), str) else None,
        scope=str(data["scope"]) if isinstance(data.get("scope"), str) else None,
    )


def save_local_llm(
    *,
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
) -> Path:
    data = _read()
    data["local_llm"] = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    return _write(data)


def save_agent_setup(agent_name: str, values: dict[str, str]) -> Path:
    clean = {key: str(value) for key, value in values.items() if str(value)}
    data = _read()
    raw = data.get("local_agent_setup")
    setup = raw if isinstance(raw, dict) else {}
    current = setup.get(agent_name)
    agent_setup = current if isinstance(current, dict) else {}
    agent_setup.update(clean)
    setup[agent_name] = agent_setup
    data["local_agent_setup"] = setup
    return _write(data)


def load_agent_setup(agent_name: str) -> dict[str, str]:
    raw = _read().get("local_agent_setup")
    if not isinstance(raw, dict):
        return {}
    values = raw.get(agent_name)
    if not isinstance(values, dict):
        return {}
    return {str(key): str(value) for key, value in values.items()}


def load_agent_setup_into_env(agent_name: str, names: set[str]) -> dict[str, str]:
    values = {
        key: value
        for key, value in load_agent_setup(agent_name).items()
        if key in names
    }
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def load_local_llm() -> LocalLLMCredentials | None:
    raw = _read().get("local_llm")
    if not isinstance(raw, dict):
        return None
    api_key = str(raw.get("api_key") or "").strip()
    if not api_key:
        return None
    return LocalLLMCredentials(
        api_key=api_key,
        base_url=str(raw.get("base_url") or "https://api.openai.com/v1"),
        model=str(raw.get("model") or "gpt-4o"),
    )


def load_local_llm_into_env() -> LocalLLMCredentials | None:
    creds = load_local_llm()
    if creds is None:
        return None
    if not os.environ.get("AGENT_LLM_KEY") and not os.environ.get(
        "A2A_LITELLM_KEY"
    ):
        os.environ["AGENT_LLM_KEY"] = creds.api_key
    os.environ.setdefault("AGENT_LLM_URL", creds.base_url)
    os.environ.setdefault("AGENT_LLM_MODEL", creds.model)
    return creds


def clear() -> bool:
    path = _creds_path()
    if path.exists():
        path.unlink()
        return True
    return False


def resolve_api_url(override: str | None = None) -> str:
    if override:
        return override
    env = os.environ.get("A2A_API_URL")
    if env:
        return env
    creds = load()
    if creds is not None:
        return creds.api_url
    return DEFAULT_API_URL
