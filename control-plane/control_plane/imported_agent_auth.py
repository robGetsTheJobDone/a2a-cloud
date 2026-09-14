from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Agent, AgentAuthConnection, User
from .safe_http import (
    InsecureTransportError,
    SafeHTTPError,
    UnsafeURLError,
    safe_request_url,
)
from .secret_crypto import decrypt_secret, encrypt_secret

SUPPORTED_SCHEME_TYPES = {"api_key", "http", "oauth2", "oidc", "mtls"}
STATIC_TOKEN_SCHEME_TYPES = {"http", "oauth2", "oidc"}
REFRESH_SKEW = timedelta(seconds=60)
MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ExternalRequestAuth:
    headers: dict[str, str]
    params: dict[str, str]
    cert_pem: str | None = None
    key_pem: str | None = None
    ca_pem: str | None = None


class ImportedAgentAuthError(RuntimeError):
    pass


def detect_auth_requirements(card: dict[str, Any]) -> list[dict[str, Any]]:
    schemes = _security_schemes(card)
    required_names = _required_scheme_names(card)
    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()

    for name, scheme in schemes.items():
        if name in seen:
            continue
        req = _requirement_from_scheme(name, scheme)
        if req is None:
            continue
        req["required"] = not required_names or name in required_names
        req["supported"] = req["scheme_type"] in SUPPORTED_SCHEME_TYPES
        requirements.append(req)
        seen.add(name)

    legacy = card.get("authentication")
    if not requirements and _declaration_present(legacy):
        requirements.append(
            {
                "scheme_name": "legacy",
                "scheme_type": "http",
                "required": True,
                "supported": True,
                "description": str(legacy),
            }
        )
    return requirements


def auth_required(card: dict[str, Any]) -> bool:
    return bool(detect_auth_requirements(card))


def sanitize_auth_metadata(connection: AgentAuthConnection) -> dict[str, Any]:
    return {
        "scheme_name": connection.scheme_name,
        "scheme_type": connection.scheme_type,
        "credential_scope": connection.credential_scope,
        "status": connection.status,
        "expires_at": connection.expires_at.isoformat() if connection.expires_at else None,
        "last_verified_at": (
            connection.last_verified_at.isoformat()
            if connection.last_verified_at
            else None
        ),
        "metadata": _public_metadata(connection.metadata_json),
    }


def connection_public_dict(connection: AgentAuthConnection) -> dict[str, Any]:
    return {
        "id": connection.id,
        "agent_name": connection.agent_name,
        **sanitize_auth_metadata(connection),
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


async def list_connections(
    session: AsyncSession,
    agent: Agent,
    *,
    user: User | None = None,
) -> list[AgentAuthConnection]:
    stmt = select(AgentAuthConnection).where(AgentAuthConnection.agent_id == agent.id)
    if user is not None:
        stmt = stmt.where(AgentAuthConnection.user_id == user.id)
    rows = (
        await session.execute(
            stmt.order_by(AgentAuthConnection.id)
        )
    ).scalars().all()
    return list(rows)


async def upsert_connection(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    scheme_name: str,
    scheme_type: str,
    credential_scope: str,
    secret_payload: dict[str, Any],
    metadata: dict[str, Any],
    expires_at: datetime | None = None,
    status: str = "connected",
) -> AgentAuthConnection:
    clean_scheme_name = _clean_scheme_name(scheme_name)
    clean_scheme_type = normalize_scheme_type(scheme_type)
    if clean_scheme_type not in SUPPORTED_SCHEME_TYPES:
        raise HTTPException(400, f"unsupported auth scheme type: {scheme_type}")
    if credential_scope not in {"agent", "user"}:
        raise HTTPException(400, "credential_scope must be 'agent' or 'user'")
    row = (
        await session.execute(
            select(AgentAuthConnection).where(
                AgentAuthConnection.agent_id == agent.id,
                AgentAuthConnection.user_id == user.id,
                AgentAuthConnection.scheme_name == clean_scheme_name,
            )
        )
    ).scalar_one_or_none()
    encrypted = encrypt_secret(json.dumps(secret_payload))
    now = datetime.now(timezone.utc)
    if row is None:
        row = AgentAuthConnection(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            scheme_name=clean_scheme_name,
            scheme_type=clean_scheme_type,
            credential_scope=credential_scope,
            status=status,
            secret_ciphertext=encrypted,
            metadata_json=_public_metadata(metadata),
            expires_at=expires_at,
            last_verified_at=now,
        )
        session.add(row)
    else:
        row.user_id = user.id
        row.agent_name = agent.name
        row.scheme_type = clean_scheme_type
        row.credential_scope = credential_scope
        row.status = status
        row.secret_ciphertext = encrypted
        row.metadata_json = _public_metadata(metadata)
        row.expires_at = expires_at
        row.last_verified_at = now
    await session.flush()
    return row


async def create_needs_setup_connections(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    requirements: list[dict[str, Any]],
) -> list[AgentAuthConnection]:
    created: list[AgentAuthConnection] = []
    for requirement in requirements:
        if not requirement.get("supported", False):
            continue
        scheme_name = _clean_scheme_name(str(requirement.get("scheme_name") or "default"))
        existing = (
            await session.execute(
                select(AgentAuthConnection).where(
                    AgentAuthConnection.agent_id == agent.id,
                    AgentAuthConnection.user_id == user.id,
                    AgentAuthConnection.scheme_name == scheme_name,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            created.append(existing)
            continue
        row = AgentAuthConnection(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            scheme_name=scheme_name,
            scheme_type=normalize_scheme_type(str(requirement["scheme_type"])),
            credential_scope="agent",
            status="needs_setup",
            secret_ciphertext=None,
            metadata_json=_public_metadata(requirement),
            expires_at=None,
            last_verified_at=None,
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def resolve_request_auth(
    session: AsyncSession,
    agent: Agent,
    *,
    user: User | None = None,
) -> ExternalRequestAuth:
    connections = await list_connections(session, agent, user=user)
    connected = [row for row in connections if row.status == "connected"]
    if not connected:
        if auth_required(agent.card if isinstance(agent.card, dict) else {}):
            raise ImportedAgentAuthError(
                "imported agent auth is not connected; configure agent auth first"
            )
        return ExternalRequestAuth(headers={}, params={})

    connection = _choose_connection(
        connected,
        detect_auth_requirements(agent.card if isinstance(agent.card, dict) else {}),
    )
    if connection is None:
        raise ImportedAgentAuthError(
            "no compatible imported-agent auth connection is connected"
        )
    if connection.scheme_type in {"oauth2", "oidc"}:
        connection = await refresh_oauth_if_needed(session, connection)
    payload = _decrypt_payload(connection)
    return _request_auth_from_payload(connection, payload)


async def refresh_oauth_if_needed(
    session: AsyncSession,
    connection: AgentAuthConnection,
) -> AgentAuthConnection:
    expires_at = connection.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is None or expires_at > datetime.now(timezone.utc) + REFRESH_SKEW:
        return connection

    payload = _decrypt_payload(connection)
    refresh_token = str(payload.get("refresh_token") or "").strip()
    token_url = str(payload.get("token_url") or connection.metadata_json.get("token_url") or "").strip()
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if not refresh_token or not token_url or not client_id:
        connection.status = "expired"
        await session.flush()
        raise ImportedAgentAuthError(
            "imported agent OAuth credential expired and cannot be refreshed"
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        response = await safe_request_url(
            "POST",
            token_url,
            headers={"accept": "application/json"},
            form_data=data,
            max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
            timeout_seconds=20.0,
            max_redirects=0,
            require_https=True,
        )
    except InsecureTransportError as exc:
        connection.status = "failed"
        connection.metadata_json = {
            **(connection.metadata_json or {}),
            "last_error": "OAuth token URL must use HTTPS",
        }
        await session.flush()
        raise ImportedAgentAuthError(
            "imported agent OAuth token URL must use HTTPS"
        ) from exc
    except UnsafeURLError as exc:
        connection.status = "failed"
        connection.metadata_json = {
            **(connection.metadata_json or {}),
            "last_error": "OAuth token URL is not public",
        }
        await session.flush()
        raise ImportedAgentAuthError(
            "imported agent OAuth token URL is not public"
        ) from exc
    except SafeHTTPError as exc:
        raise ImportedAgentAuthError(
            "imported agent OAuth refresh request failed"
        ) from exc
    if response.status_code >= 400:
        connection.status = "failed"
        connection.metadata_json = {
            **(connection.metadata_json or {}),
            "last_error": f"refresh failed with HTTP {response.status_code}",
        }
        await session.flush()
        raise ImportedAgentAuthError(
            f"imported agent OAuth refresh failed with HTTP {response.status_code}"
        )
    try:
        body = json.loads(response.text)
    except ValueError as exc:
        raise ImportedAgentAuthError(
            "imported agent OAuth refresh returned invalid JSON"
        ) from exc
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        connection.status = "failed"
        await session.flush()
        raise ImportedAgentAuthError("imported agent OAuth refresh returned no access token")
    payload["access_token"] = access_token
    payload["refresh_token"] = str(body.get("refresh_token") or refresh_token)
    if "expires_in" in body:
        try:
            connection.expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(body["expires_in"])
            )
        except Exception:  # noqa: BLE001
            connection.expires_at = None
    connection.secret_ciphertext = encrypt_secret(json.dumps(payload))
    connection.status = "connected"
    connection.last_verified_at = datetime.now(timezone.utc)
    await session.flush()
    return connection


def select_requirement_for_setup(
    requirements: list[dict[str, Any]],
    *,
    scheme_type: str,
    scheme_name: str | None,
    api_key_location: str | None = None,
    api_key_name: str | None = None,
) -> dict[str, Any] | None:
    wanted_type = normalize_scheme_type(scheme_type)
    candidates = [
        item
        for item in requirements
        if normalize_scheme_type(str(item.get("scheme_type") or "")) == wanted_type
    ]
    if scheme_name:
        named = [
            item
            for item in candidates
            if str(item.get("scheme_name") or "") == scheme_name
        ]
        if not named:
            raise HTTPException(400, "auth scheme does not match the Agent Card")
        candidates = named
    if wanted_type == "api_key":
        candidates = [
            item
            for item in candidates
            if _api_key_matches(item, api_key_location, api_key_name)
        ]
    if not candidates:
        return None
    if len(candidates) > 1 and not scheme_name:
        raise HTTPException(400, "scheme_name is required; Agent Card has multiple matching auth schemes")
    return candidates[0]


def normalize_scheme_type(value: str) -> str:
    clean = value.strip().lower().replace("-", "_")
    if clean in {"bearer", "basic"}:
        return "http"
    if clean in {"apikey", "api_key"}:
        return "api_key"
    if clean in {"openidconnect", "open_id_connect", "oidc"}:
        return "oidc"
    if clean in {"mutualtls", "mutual_tls", "mtls"}:
        return "mtls"
    return clean


def _security_schemes(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = card.get("securitySchemes")
    if not isinstance(value, dict):
        value = card.get("security_schemes")
    if not isinstance(value, dict):
        return {}
    return {
        str(name): scheme
        for name, scheme in value.items()
        if isinstance(scheme, dict)
    }


def _required_scheme_names(card: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for source in (card, *(s for s in card.get("skills") or [] if isinstance(s, dict))):
        for key in ("securityRequirements", "security", "security_requirements"):
            value = source.get(key)
            if isinstance(value, dict):
                names.update(str(name) for name in value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        names.update(str(name) for name in item)
    return names


def _requirement_from_scheme(name: str, scheme: dict[str, Any]) -> dict[str, Any] | None:
    if "apiKeySecurityScheme" in scheme and isinstance(scheme["apiKeySecurityScheme"], dict):
        inner = scheme["apiKeySecurityScheme"]
        return {
            "scheme_name": name,
            "scheme_type": "api_key",
            "location": inner.get("location") or inner.get("in") or "header",
            "name": inner.get("name"),
            "description": inner.get("description") or "",
        }
    if "httpAuthSecurityScheme" in scheme and isinstance(scheme["httpAuthSecurityScheme"], dict):
        inner = scheme["httpAuthSecurityScheme"]
        return {
            "scheme_name": name,
            "scheme_type": "http",
            "scheme": inner.get("scheme") or "Bearer",
            "bearer_format": inner.get("bearerFormat"),
            "description": inner.get("description") or "",
        }
    if "oauth2SecurityScheme" in scheme and isinstance(scheme["oauth2SecurityScheme"], dict):
        return _oauth_requirement(name, scheme["oauth2SecurityScheme"], "oauth2")
    if (
        "openIdConnectSecurityScheme" in scheme
        and isinstance(scheme["openIdConnectSecurityScheme"], dict)
    ):
        inner = scheme["openIdConnectSecurityScheme"]
        return {
            "scheme_name": name,
            "scheme_type": "oidc",
            "open_id_connect_url": inner.get("openIdConnectUrl") or inner.get("open_id_connect_url"),
            "description": inner.get("description") or "",
        }
    if "mtlsSecurityScheme" in scheme:
        inner = scheme["mtlsSecurityScheme"]
        if not isinstance(inner, dict):
            inner = {}
        return {
            "scheme_name": name,
            "scheme_type": "mtls",
            "description": inner.get("description") or "",
        }

    openapi_type = normalize_scheme_type(str(scheme.get("type") or ""))
    if openapi_type == "api_key":
        return {
            "scheme_name": name,
            "scheme_type": "api_key",
            "location": scheme.get("in") or scheme.get("location") or "header",
            "name": scheme.get("name"),
            "description": scheme.get("description") or "",
        }
    if openapi_type == "http":
        return {
            "scheme_name": name,
            "scheme_type": "http",
            "scheme": scheme.get("scheme") or "Bearer",
            "bearer_format": scheme.get("bearerFormat"),
            "description": scheme.get("description") or "",
        }
    if openapi_type == "oauth2":
        return _oauth_requirement(name, scheme, "oauth2")
    if openapi_type == "oidc":
        return {
            "scheme_name": name,
            "scheme_type": "oidc",
            "open_id_connect_url": scheme.get("openIdConnectUrl") or scheme.get("open_id_connect_url"),
            "description": scheme.get("description") or "",
        }
    if openapi_type == "mtls":
        return {
            "scheme_name": name,
            "scheme_type": "mtls",
            "description": scheme.get("description") or "",
        }
    return None


def _oauth_requirement(name: str, scheme: dict[str, Any], scheme_type: str) -> dict[str, Any]:
    flows = scheme.get("flows") if isinstance(scheme.get("flows"), dict) else scheme
    authorization_url = None
    token_url = None
    scopes: list[str] = []
    if isinstance(flows, dict):
        for flow_name in ("authorizationCode", "clientCredentials", "deviceCode", "implicit", "password"):
            flow = flows.get(flow_name)
            if not isinstance(flow, dict):
                continue
            authorization_url = authorization_url or flow.get("authorizationUrl")
            token_url = token_url or flow.get("tokenUrl")
            flow_scopes = flow.get("scopes")
            if isinstance(flow_scopes, dict):
                scopes.extend(str(scope) for scope in flow_scopes)
    return {
        "scheme_name": name,
        "scheme_type": scheme_type,
        "authorization_url": authorization_url,
        "token_url": token_url,
        "scopes": sorted(set(scopes)),
        "description": scheme.get("description") or "",
    }


def _request_auth_from_payload(
    connection: AgentAuthConnection,
    payload: dict[str, Any],
) -> ExternalRequestAuth:
    metadata = connection.metadata_json or {}
    if connection.scheme_type == "api_key":
        value = str(payload.get("value") or payload.get("api_key") or "").strip()
        if not value:
            raise ImportedAgentAuthError("imported agent API key is missing")
        name = str(metadata.get("name") or payload.get("name") or "").strip()
        location = str(metadata.get("location") or payload.get("location") or "header").lower()
        if not name:
            raise ImportedAgentAuthError("imported agent API-key name is missing")
        if location == "query":
            return ExternalRequestAuth(headers={}, params={name: value})
        if location == "header":
            return ExternalRequestAuth(headers={name: value}, params={})
        raise ImportedAgentAuthError("imported agent API-key location is unsupported")

    if connection.scheme_type == "http":
        token = str(payload.get("token") or payload.get("value") or "").strip()
        scheme = str(metadata.get("scheme") or payload.get("scheme") or "Bearer").strip()
        if not token:
            raise ImportedAgentAuthError("imported agent HTTP credential is missing")
        return ExternalRequestAuth(headers={"authorization": f"{scheme} {token}"}, params={})

    if connection.scheme_type in {"oauth2", "oidc"}:
        access_token = str(payload.get("access_token") or "").strip()
        token_type = str(payload.get("token_type") or "Bearer").strip() or "Bearer"
        if not access_token:
            raise ImportedAgentAuthError("imported agent OAuth access token is missing")
        return ExternalRequestAuth(
            headers={"authorization": f"{token_type} {access_token}"},
            params={},
        )

    if connection.scheme_type == "mtls":
        cert_pem = str(payload.get("cert_pem") or "").strip()
        key_pem = str(payload.get("key_pem") or "").strip()
        ca_pem = str(payload.get("ca_pem") or "").strip() or None
        if not cert_pem or not key_pem:
            raise ImportedAgentAuthError("imported agent mTLS certificate or key is missing")
        return ExternalRequestAuth(
            headers={},
            params={},
            cert_pem=cert_pem,
            key_pem=key_pem,
            ca_pem=ca_pem,
        )

    raise ImportedAgentAuthError("imported agent auth type is unsupported")


def _choose_connection(
    connections: list[AgentAuthConnection],
    requirements: list[dict[str, Any]],
) -> AgentAuthConnection | None:
    required_names = [str(req.get("scheme_name")) for req in requirements if req.get("required")]
    for name in required_names:
        for connection in connections:
            if connection.scheme_name == name:
                return connection
    return connections[0] if connections else None


def _decrypt_payload(connection: AgentAuthConnection) -> dict[str, Any]:
    if not connection.secret_ciphertext:
        raise ImportedAgentAuthError("imported agent auth credential is not configured")
    try:
        value = decrypt_secret(connection.secret_ciphertext)
    except HTTPException as exc:
        raise ImportedAgentAuthError("imported agent auth credential could not be decrypted") from exc
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ImportedAgentAuthError("imported agent auth credential is invalid") from exc
    if not isinstance(payload, dict):
        raise ImportedAgentAuthError("imported agent auth credential is invalid")
    return payload


def _api_key_matches(
    requirement: dict[str, Any],
    location: str | None,
    name: str | None,
) -> bool:
    req_location = str(requirement.get("location") or "header").lower()
    req_name = str(requirement.get("name") or "")
    if location and req_location != location.lower():
        return False
    if req_name and name and req_name.lower() != name.lower():
        return False
    return True


def _public_metadata(value: dict[str, Any] | None) -> dict[str, Any]:
    metadata = dict(value or {})
    for key in (
        "value",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
        "api_key",
        "cert_pem",
        "key_pem",
        "ca_pem",
    ):
        metadata.pop(key, None)
    return metadata


def _clean_scheme_name(value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 128:
        raise HTTPException(400, "scheme_name is required and must be <= 128 characters")
    return clean


def _declaration_present(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return False
