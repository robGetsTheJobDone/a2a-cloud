from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .imported_agent_auth import connection_public_dict
from .models import (
    Agent,
    AgentAuthConnection,
    AgentConsumerSetupValue,
    AgentInstall,
    Organization,
    OrganizationMember,
    User,
)
from .secret_crypto import decrypt_secret, encrypt_secret

ADMIN_ROLES = {"owner", "admin"}
SECRET_KEYS = {
    "value",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "secret",
    "password",
}
SUPPORTED_KINDS = {"config", "secret"}
SUPPORTED_INPUT_TYPES = {
    "text",
    "password",
    "url",
    "email",
    "textarea",
    "number",
    "boolean",
    "select",
}


@dataclass(frozen=True)
class ConsumerSetupResolution:
    declaration: dict[str, Any]
    values: list[dict[str, Any]]
    missing_required: list[str]
    consumer_config: dict[str, Any]
    consumer_secrets: dict[str, str]
    organization: dict[str, Any] | None
    can_manage_org: bool

    @property
    def complete(self) -> bool:
        return not self.missing_required

    def payload(self) -> dict[str, Any]:
        return {
            "declaration": self.declaration,
            "values": self.values,
            "missing_required": self.missing_required,
            "complete": self.complete,
            "organization": self.organization,
            "can_manage_org": self.can_manage_org,
        }

    def invocation_payload(self) -> dict[str, dict[str, Any]]:
        return {
            "consumer_config": self.consumer_config,
            "consumer_secrets": self.consumer_secrets,
        }


class ConsumerSetupRequired(RuntimeError):
    def __init__(self, resolution: ConsumerSetupResolution) -> None:
        self.resolution = resolution
        super().__init__(
            "consumer setup required: "
            + ", ".join(resolution.missing_required)
        )


def declaration_from_card(card: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(card, dict):
        return {"fields": []}
    raw = card.get("consumer_setup")
    if not isinstance(raw, dict):
        return {"fields": []}
    fields = raw.get("fields")
    if not isinstance(fields, list):
        return {"fields": []}
    normalized = [_normalize_field(field) for field in fields if isinstance(field, dict)]
    return {"fields": [field for field in normalized if field is not None]}


async def resolve_consumer_setup(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    organization_slug: str | None = None,
) -> ConsumerSetupResolution:
    declaration = declaration_from_card(
        agent.card if isinstance(agent.card, dict) else {}
    )
    fields = declaration.get("fields") or []
    org, member = await _resolve_user_org(session, user, organization_slug)
    rows = await _setup_rows(
        session,
        agent,
        user_id=user.id,
        organization_id=org.id if org else None,
    )
    row_by_scope: dict[tuple[str, str], AgentConsumerSetupValue] = {
        (row.scope, row.field_name): row for row in rows
    }

    values: list[dict[str, Any]] = []
    config: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    missing: list[str] = []

    for field in fields:
        name = str(field["name"])
        kind = str(field.get("kind") or "config")
        user_row = row_by_scope.get(("user", name))
        org_row = row_by_scope.get(("org", name))
        row = user_row or org_row
        source = row.scope if row is not None else None
        configured = row is not None
        if not configured and field.get("required", True):
            missing.append(name)
        if row is not None:
            if kind == "secret":
                if row.secret_ciphertext:
                    secrets[name] = decrypt_secret(row.secret_ciphertext)
            else:
                config[name] = row.value_json
        values.append(
            {
                "name": name,
                "kind": kind,
                "configured": configured,
                "source": source,
                "value_redacted": row.value_redacted if row is not None else None,
                "updated_at": row.updated_at if row is not None else None,
            }
        )

    return ConsumerSetupResolution(
        declaration=declaration,
        values=values,
        missing_required=missing,
        consumer_config=config,
        consumer_secrets=secrets,
        organization=_org_payload(org, member),
        can_manage_org=bool(member and member.role in ADMIN_ROLES),
    )


async def require_consumer_setup(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    organization_slug: str | None = None,
) -> ConsumerSetupResolution:
    resolution = await resolve_consumer_setup(
        agent=agent,
        user=user,
        session=session,
        organization_slug=organization_slug,
    )
    if not resolution.complete:
        raise ConsumerSetupRequired(resolution)
    return resolution


async def require_consumer_setup_invocation(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    organization_slug: str | None = None,
) -> ConsumerSetupResolution:
    resolution = await resolve_consumer_setup_invocation(
        agent=agent,
        user=user,
        session=session,
        organization_slug=organization_slug,
    )
    if not resolution.complete:
        raise ConsumerSetupRequired(resolution)
    return resolution


async def resolve_consumer_setup_invocation(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    organization_slug: str | None = None,
) -> ConsumerSetupResolution:
    declaration = declaration_from_card(
        agent.card if isinstance(agent.card, dict) else {}
    )
    fields = declaration.get("fields") or []
    if not fields and organization_slug is None:
        return ConsumerSetupResolution(
            declaration=declaration,
            values=[],
            missing_required=[],
            consumer_config={},
            consumer_secrets={},
            organization=None,
            can_manage_org=False,
        )

    org, member = await _resolve_user_org(session, user, organization_slug)
    if not fields:
        return ConsumerSetupResolution(
            declaration=declaration,
            values=[],
            missing_required=[],
            consumer_config={},
            consumer_secrets={},
            organization=_org_payload(org, member),
            can_manage_org=bool(member and member.role in ADMIN_ROLES),
        )

    rows = await _setup_rows(
        session,
        agent,
        user_id=user.id,
        organization_id=org.id if org else None,
    )
    row_by_scope: dict[tuple[str, str], AgentConsumerSetupValue] = {
        (row.scope, row.field_name): row for row in rows
    }

    config: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    missing: list[str] = []

    for field in fields:
        name = str(field["name"])
        kind = str(field.get("kind") or "config")
        row = row_by_scope.get(("user", name)) or row_by_scope.get(("org", name))
        if row is None:
            if field.get("required", True):
                missing.append(name)
            continue
        if kind == "secret":
            if row.secret_ciphertext:
                secrets[name] = decrypt_secret(row.secret_ciphertext)
        else:
            config[name] = row.value_json

    if missing:
        return await resolve_consumer_setup(
            agent=agent,
            user=user,
            session=session,
            organization_slug=organization_slug,
        )

    return ConsumerSetupResolution(
        declaration=declaration,
        values=[],
        missing_required=[],
        consumer_config=config,
        consumer_secrets=secrets,
        organization=_org_payload(org, member),
        can_manage_org=bool(member and member.role in ADMIN_ROLES),
    )


async def upsert_consumer_setup_values(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    values: dict[str, Any],
    scope: str,
    organization_slug: str | None = None,
) -> ConsumerSetupResolution:
    if scope not in {"user", "org"}:
        raise HTTPException(400, "scope must be 'user' or 'org'")
    org = None
    if scope == "org":
        org, member = await _require_org_admin(session, user, organization_slug)
        user_id = None
        organization_id = org.id
    else:
        if organization_slug:
            await _resolve_user_org(session, user, organization_slug)
        user_id = user.id
        organization_id = None

    fields = {
        str(field["name"]): field
        for field in declaration_from_card(agent.card if isinstance(agent.card, dict) else {}).get("fields", [])
    }
    if not fields:
        raise HTTPException(400, "agent does not declare consumer setup")

    for raw_name, value in values.items():
        name = str(raw_name).strip()
        field = fields.get(name)
        if field is None:
            raise HTTPException(400, f"unknown consumer setup field: {name}")
        kind = str(field.get("kind") or "config")
        if kind == "secret":
            if not isinstance(value, str) or value == "":
                raise HTTPException(400, f"{name} must be a non-empty string")
            value_json = None
            secret_ciphertext = encrypt_secret(value)
            redacted = _redact(value)
        else:
            value_json = value
            secret_ciphertext = None
            redacted = _redact_public(value)

        row = await _find_value_row(
            session,
            agent_id=agent.id,
            field_name=name,
            scope=scope,
            user_id=user_id,
            organization_id=organization_id,
        )
        if row is None:
            row = AgentConsumerSetupValue(
                agent_id=agent.id,
                agent_name=agent.name,
                field_name=name,
                field_kind=kind,
                scope=scope,
                user_id=user_id,
                organization_id=organization_id,
            )
            session.add(row)
        row.agent_name = agent.name
        row.field_kind = kind
        row.value_json = value_json
        row.secret_ciphertext = secret_ciphertext
        row.value_redacted = redacted

    await session.commit()
    return await resolve_consumer_setup(
        agent=agent,
        user=user,
        session=session,
        organization_slug=org.slug if org else organization_slug,
    )


async def delete_consumer_setup_value(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
    field_name: str,
    scope: str = "user",
    organization_slug: str | None = None,
) -> ConsumerSetupResolution:
    if scope == "org":
        org, _member = await _require_org_admin(session, user, organization_slug)
        user_id = None
        organization_id = org.id
    else:
        org = None
        user_id = user.id
        organization_id = None
    row = await _find_value_row(
        session,
        agent_id=agent.id,
        field_name=field_name,
        scope=scope,
        user_id=user_id,
        organization_id=organization_id,
    )
    if row is None:
        raise HTTPException(404, "consumer setup value not found")
    await session.delete(row)
    await session.commit()
    return await resolve_consumer_setup(
        agent=agent,
        user=user,
        session=session,
        organization_slug=org.slug if org else organization_slug,
    )


def setup_required_payload(
    *,
    agent: Agent,
    resolution: ConsumerSetupResolution,
) -> dict[str, Any]:
    payload = resolution.payload()
    return jsonable_encoder({
        "error": "agent_setup_required",
        "agent": agent.name,
        "setup": payload,
        "missing_required": resolution.missing_required,
    })


async def list_installed_agents(
    *,
    user: User,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    """Return agents where the user has saved setup or auth material.

    The payload is dashboard-facing and deliberately limited to redacted
    values plus public connection metadata. Secret ciphertext and decrypted
    values never leave this module's invocation paths.
    """

    org_rows = await _user_org_rows(session, user)
    org_by_id = {org.id: (org, member) for org, member in org_rows}
    org_ids = list(org_by_id)
    entries: dict[int, dict[str, Any]] = {}

    install_rows = (
        await session.execute(
            select(Agent, AgentInstall)
            .join(AgentInstall, AgentInstall.agent_id == Agent.id)
            .where(
                or_(Agent.public.is_(True), Agent.owner_id == user.id),
                AgentInstall.user_id == user.id,
            )
        )
    ).all()
    for agent, install in install_rows:
        entry = _installed_entry(entries, agent)
        _touch_installed_entry(entry, install.updated_at or install.created_at)

    setup_filters = [
        and_(
            AgentConsumerSetupValue.scope == "user",
            AgentConsumerSetupValue.user_id == user.id,
        )
    ]
    if org_ids:
        setup_filters.append(
            and_(
                AgentConsumerSetupValue.scope == "org",
                AgentConsumerSetupValue.organization_id.in_(org_ids),
            )
        )

    setup_rows = (
        await session.execute(
            select(Agent, AgentConsumerSetupValue)
            .join(
                AgentConsumerSetupValue,
                AgentConsumerSetupValue.agent_id == Agent.id,
            )
            .where(
                or_(Agent.public.is_(True), Agent.owner_id == user.id),
                or_(*setup_filters),
            )
        )
    ).all()
    for agent, row in setup_rows:
        entry = _installed_entry(entries, agent)
        field_by_name = entry["_field_by_name"]
        field = field_by_name.get(row.field_name)
        scope = row.scope if row.scope in {"user", "org"} else "user"
        org_payload = None
        if scope == "org" and row.organization_id is not None:
            org_pair = org_by_id.get(row.organization_id)
            if org_pair is not None:
                org_payload = _org_payload(*org_pair)
        entry["setup_values"].append(
            {
                "name": row.field_name,
                "kind": str((field or {}).get("kind") or row.field_kind or "config"),
                "label": str((field or {}).get("label") or row.field_name),
                "required": bool((field or {}).get("required", False)),
                "input_type": str(
                    (field or {}).get("input_type")
                    or ("password" if row.field_kind == "secret" else "text")
                ),
                "configured": True,
                "source": scope,
                "scope": scope,
                "organization": org_payload,
                "value_redacted": row.value_redacted,
                "updated_at": row.updated_at,
            }
        )
        entry["_configured_setup_names"].add(row.field_name)
        _touch_installed_entry(entry, row.updated_at)

    auth_rows = (
        await session.execute(
            select(Agent, AgentAuthConnection)
            .join(AgentAuthConnection, AgentAuthConnection.agent_id == Agent.id)
            .where(
                or_(Agent.public.is_(True), Agent.owner_id == user.id),
                AgentAuthConnection.user_id == user.id,
                or_(
                    AgentAuthConnection.status == "connected",
                    AgentAuthConnection.secret_ciphertext.is_not(None),
                ),
            )
        )
    ).all()
    for agent, connection in auth_rows:
        entry = _installed_entry(entries, agent)
        entry["auth_connections"].append(connection_public_dict(connection))
        _touch_installed_entry(entry, connection.updated_at)

    out: list[dict[str, Any]] = []
    for entry in entries.values():
        field_by_name = entry.pop("_field_by_name")
        configured_names = entry.pop("_configured_setup_names")
        entry["missing_required"] = [
            str(field["name"])
            for field in field_by_name.values()
            if field.get("required", True) and str(field["name"]) not in configured_names
        ]
        entry["setup_values"].sort(
            key=lambda item: (
                0 if item.get("scope") == "user" else 1,
                str(item.get("name") or ""),
            )
        )
        entry["auth_connections"].sort(
            key=lambda item: str(item.get("scheme_name") or "")
        )
        out.append(entry)

    out.sort(
        key=lambda item: (
            item.get("updated_at") is not None,
            item.get("updated_at"),
            str(item["agent"]["name"]),
        ),
        reverse=True,
    )
    return out


async def install_agent(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(AgentInstall).where(
                AgentInstall.agent_id == agent.id,
                AgentInstall.user_id == user.id,
            )
        )
    ).scalars().first()
    if row is None:
        row = AgentInstall(agent_id=agent.id, user_id=user.id, agent_name=agent.name)
        session.add(row)
    else:
        row.agent_name = agent.name
    await session.commit()
    return await _installed_agent(agent=agent, user=user, session=session)


async def _installed_agent(
    *,
    agent: Agent,
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    installed = await list_installed_agents(user=user, session=session)
    for item in installed:
        if item["agent"]["id"] == agent.id:
            return item
    raise HTTPException(500, "installed agent not found")


def _normalize_field(field: dict[str, Any]) -> dict[str, Any] | None:
    name = str(field.get("name") or "").strip()
    if not name:
        return None
    kind = str(field.get("kind") or "config").strip().lower()
    if kind not in SUPPORTED_KINDS:
        kind = "config"
    default_input_type = "password" if kind == "secret" else "text"
    input_type = str(field.get("input_type") or default_input_type)
    input_type = input_type.strip().lower()
    if input_type not in SUPPORTED_INPUT_TYPES:
        input_type = "password" if kind == "secret" else "text"
    options = field.get("options")
    return {
        "name": name,
        "kind": kind,
        "label": str(field.get("label") or name),
        "description": str(field.get("description") or ""),
        "required": bool(field.get("required", True)),
        "input_type": input_type,
        "options": [str(option) for option in options] if isinstance(options, list) else [],
    }


async def _setup_rows(
    session: AsyncSession,
    agent: Agent,
    *,
    user_id: int,
    organization_id: int | None,
) -> list[AgentConsumerSetupValue]:
    filters = [
        and_(
            AgentConsumerSetupValue.scope == "user",
            AgentConsumerSetupValue.user_id == user_id,
        )
    ]
    if organization_id is not None:
        filters.append(
            and_(
                AgentConsumerSetupValue.scope == "org",
                AgentConsumerSetupValue.organization_id == organization_id,
            )
        )
    rows = (
        await session.execute(
            select(AgentConsumerSetupValue).where(
                AgentConsumerSetupValue.agent_id == agent.id,
                or_(*filters),
            )
        )
    ).scalars().all()
    return list(rows)


async def _find_value_row(
    session: AsyncSession,
    *,
    agent_id: int,
    field_name: str,
    scope: str,
    user_id: int | None,
    organization_id: int | None,
) -> AgentConsumerSetupValue | None:
    stmt = select(AgentConsumerSetupValue).where(
        AgentConsumerSetupValue.agent_id == agent_id,
        AgentConsumerSetupValue.field_name == field_name,
        AgentConsumerSetupValue.scope == scope,
    )
    if scope == "user":
        stmt = stmt.where(AgentConsumerSetupValue.user_id == user_id)
    else:
        stmt = stmt.where(AgentConsumerSetupValue.organization_id == organization_id)
    return (await session.execute(stmt)).scalars().first()


async def _user_org_rows(
    session: AsyncSession,
    user: User,
) -> list[tuple[Organization, OrganizationMember]]:
    rows = (
        await session.execute(
            select(Organization, OrganizationMember)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.active == True,  # noqa: E712
            )
            .order_by(Organization.id.asc())
        )
    ).all()
    return [(org, member) for org, member in rows]


async def _resolve_user_org(
    session: AsyncSession,
    user: User,
    organization_slug: str | None,
) -> tuple[Organization | None, OrganizationMember | None]:
    stmt = (
        select(Organization, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.active == True,  # noqa: E712
        )
        .order_by(Organization.id.asc())
    )
    if organization_slug:
        stmt = stmt.where(Organization.slug == organization_slug)
    row = (await session.execute(stmt)).first()
    if row is None:
        if organization_slug:
            raise HTTPException(404, "organization not found")
        return None, None
    org, member = row
    return org, member


async def _require_org_admin(
    session: AsyncSession,
    user: User,
    organization_slug: str | None,
) -> tuple[Organization, OrganizationMember]:
    org, member = await _resolve_user_org(session, user, organization_slug)
    if org is None or member is None:
        raise HTTPException(400, "organization scope requires an organization")
    if member.role not in ADMIN_ROLES:
        raise HTTPException(403, "organization admin required")
    return org, member


def _installed_entry(
    entries: dict[int, dict[str, Any]],
    agent: Agent,
) -> dict[str, Any]:
    entry = entries.get(agent.id)
    if entry is not None:
        return entry
    field_by_name = {
        str(field["name"]): field
        for field in declaration_from_card(
            agent.card if isinstance(agent.card, dict) else {}
        ).get("fields", [])
    }
    entry = {
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "version": agent.version,
            "image": agent.image,
            "public": agent.public,
            "status": agent.status,
            "url": agent.url,
            "card": agent.card if isinstance(agent.card, dict) else {},
            "created_at": agent.created_at,
        },
        "setup_values": [],
        "auth_connections": [],
        "missing_required": [],
        "updated_at": None,
        "_field_by_name": field_by_name,
        "_configured_setup_names": set(),
    }
    entries[agent.id] = entry
    return entry


def _touch_installed_entry(
    entry: dict[str, Any],
    updated_at: datetime | None,
) -> None:
    if updated_at is None:
        return
    current = entry.get("updated_at")
    if current is None or updated_at > current:
        entry["updated_at"] = updated_at


def _org_payload(
    org: Organization | None,
    member: OrganizationMember | None,
) -> dict[str, Any] | None:
    if org is None or member is None:
        return None
    return {
        "id": org.id,
        "slug": org.slug,
        "name": org.name,
        "role": member.role,
    }


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _redact_public(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    text = str(value)
    if len(text) <= 32 and not _looks_sensitive(text):
        return text
    return _redact(text)


def _looks_sensitive(value: str) -> bool:
    lower = value.lower()
    return any(key in lower for key in SECRET_KEYS)
