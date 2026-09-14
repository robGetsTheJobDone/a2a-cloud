"""Admin-panel-only endpoints (shared-secret auth, no user identity).

These routes back the toggle/configuration surface in ``apps/admin``.
Authorization is :func:`require_admin` from :mod:`control_plane.admin_auth`
— an HMAC-compared shared secret, not a user JWT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


# hi
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..admin_auth import require_admin
from ..auth import issue_token
from ..config import settings
from ..control_room import get_or_create_policy, policy_dict
from ..db import Base, get_session
from ..keycloak_auth import provision_user_from_claims, verify_keycloak_id_token
from ..models import (
    Agent,
    AgentDeploymentEvent,
    AgentProofRun,
    AgentReviewRun,
    ChatThreadEvent,
    DagRun,
    DagRunNode,
    GiteaTokenAudit,
    GrantAudit,
    LLMUsageEvent,
    MetaAgentRun,
    Organization,
    OrganizationAuditLog,
    OrganizationDomain,
    OrganizationGiteaWorkspace,
    OrganizationLangfuseWorkspace,
    OrganizationMember,
    OrganizationScimToken,
    PlatformSetting,
    SubagentRun,
    SubagentRunEvent,
    TrialRun,
    User,
    UserControlPolicy,
    UserFeatureFlag,
    UserLangfuseAccount,
    WorkEvent,
)
from .agents import _cleanup_agent_resources, _cleanup_external_agent_resources, _is_external_agent
from ..platform_settings import KNOWN_SETTINGS, set_setting

# Operator surface, gated by the shared admin token. Kept out of the published
# OpenAPI document: no customer can call it, and it is not something to hand a
# stranger a client for (``DELETE /v1/admin/users`` truncates tables).
router = APIRouter(
    prefix="/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    include_in_schema=False,
)


class SettingOut(BaseModel):
    key: str
    value: Any
    description: str | None
    updated_by: str | None
    updated_at: datetime | None

    class Config:
        from_attributes = True


class SettingUpdateIn(BaseModel):
    value: Any
    actor: str | None = None


class AdminKeycloakLoginIn(BaseModel):
    id_token: str


class AdminKeycloakUserOut(BaseModel):
    id: int
    email: str
    is_admin: bool


class AdminUserOut(BaseModel):
    id: int
    email: str
    is_admin: bool
    created_at: datetime
    agent_count: int
    repo_count: int
    control_policy: dict[str, Any] | None = None
    feature_flags: list[str] = Field(default_factory=list)


class AdminInventoryTotalsOut(BaseModel):
    users: int
    agents: int
    repos: int


class AdminSignupTotalsOut(BaseModel):
    last_24h: int
    last_7d: int
    last_30d: int


class AdminInventoryOut(BaseModel):
    totals: AdminInventoryTotalsOut
    signups: AdminSignupTotalsOut
    users: list[AdminUserOut]


class AdminPurgeOut(BaseModel):
    deleted_users: int
    deleted_agents: int


class AdminControlPolicyUpdateIn(BaseModel):
    monthly_budget_cents: int | None = None
    run_budget_cents: int | None = None
    max_agent_calls_per_run: int | None = None
    require_approval_for_file_writes: bool | None = None
    deny_external_network: bool | None = None
    only_approved_agents: bool | None = None
    pii_safe_mode: bool | None = None
    approved_agents: list[str] | None = None


class AdminControlPolicyOut(BaseModel):
    user_id: int
    email: str
    policy: dict[str, Any]


class AdminPlatformTokenCreateIn(BaseModel):
    ttl_seconds: int = Field(default=86400, ge=60, le=2_592_000)
    label: str | None = Field(default=None, max_length=160)


class AdminPlatformTokenCreatedOut(BaseModel):
    user_id: int
    email: str
    token_type: str = "Bearer"
    token: str
    expires_at: datetime
    ttl_seconds: int
    label: str | None = None


def _managed_repo_count_expr():
    return func.coalesce(
        func.sum(
            case(
                (Agent.gitea_owner.isnot(None), 1),
                else_=0,
            )
        ),
        0,
    )


async def _delete_tables(session: AsyncSession, tables: tuple[Any, ...]) -> None:
    for table in tables:
        await session.execute(delete(table))


ORG_TABLES = (
    OrganizationAuditLog.__table__,
    OrganizationScimToken.__table__,
    OrganizationMember.__table__,
    OrganizationDomain.__table__,
    OrganizationLangfuseWorkspace.__table__,
    UserLangfuseAccount.__table__,
    OrganizationGiteaWorkspace.__table__,
    Organization.__table__,
)

HISTORY_TABLES = (
    AgentDeploymentEvent.__table__,
    AgentProofRun.__table__,
    AgentReviewRun.__table__,
    GiteaTokenAudit.__table__,
    GrantAudit.__table__,
    ChatThreadEvent.__table__,
    LLMUsageEvent.__table__,
    SubagentRunEvent.__table__,
    SubagentRun.__table__,
    DagRunNode.__table__,
    DagRun.__table__,
    MetaAgentRun.__table__,
    TrialRun.__table__,
    WorkEvent.__table__,
)


@router.post("/auth/keycloak", response_model=AdminKeycloakUserOut)
async def authenticate_keycloak_admin(
    body: AdminKeycloakLoginIn,
    session: AsyncSession = Depends(get_session),
) -> AdminKeycloakUserOut:
    claims = verify_keycloak_id_token(
        body.id_token,
        audience=settings.keycloak_admin_client_id,
    )
    user = await provision_user_from_claims(session, claims)
    if not user.is_admin:
        raise HTTPException(403, "admin access required")
    return AdminKeycloakUserOut(id=user.id, email=user.email, is_admin=True)


@router.get("/settings", response_model=list[SettingOut])
async def list_settings(
    session: AsyncSession = Depends(get_session),
) -> list[SettingOut]:
    """List every known setting, falling back to the platform default when no
    row exists. Always includes the full :data:`KNOWN_SETTINGS` keyset so the
    admin UI can render the toggle even before an operator has touched it.
    """
    rows = (await session.execute(select(PlatformSetting))).scalars().all()
    by_key: dict[str, PlatformSetting] = {row.key: row for row in rows}
    out: list[SettingOut] = []
    for key, meta in KNOWN_SETTINGS.items():
        row = by_key.get(key)
        if row is not None:
            out.append(SettingOut.model_validate(row))
        else:
            out.append(
                SettingOut(
                    key=key,
                    value=meta.get("default"),
                    description=meta.get("description"),
                    updated_by=None,
                    updated_at=None,
                )
            )
    return out


@router.get("/settings/{key}", response_model=SettingOut)
async def read_setting(
    key: str = Path(...),
    session: AsyncSession = Depends(get_session),
) -> SettingOut:
    if key not in KNOWN_SETTINGS:
        raise HTTPException(404, f"unknown setting {key!r}")
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == key)
        )
    ).scalar_one_or_none()
    if row is not None:
        return SettingOut.model_validate(row)
    meta = KNOWN_SETTINGS[key]
    return SettingOut(
        key=key,
        value=meta.get("default"),
        description=meta.get("description"),
        updated_by=None,
        updated_at=None,
    )


@router.put("/settings/{key}", response_model=SettingOut)
async def write_setting(
    body: SettingUpdateIn,
    key: str = Path(...),
    session: AsyncSession = Depends(get_session),
) -> SettingOut:
    if key not in KNOWN_SETTINGS:
        raise HTTPException(404, f"unknown setting {key!r}")
    expected_type = KNOWN_SETTINGS[key].get("type")
    if expected_type == "bool" and not isinstance(body.value, bool):
        raise HTTPException(400, f"setting {key!r} expects a bool")
    row = await set_setting(
        session,
        key,
        body.value,
        actor=body.actor or "admin",
    )
    return SettingOut.model_validate(row)


@router.get("/users", response_model=AdminInventoryOut)
async def list_users(
    session: AsyncSession = Depends(get_session),
) -> AdminInventoryOut:
    repo_count_expr = _managed_repo_count_expr()
    rows = (
        await session.execute(
            select(
                User,
                func.count(Agent.id).label("agent_count"),
                repo_count_expr.label("repo_count"),
                UserControlPolicy,
            )
            .outerjoin(Agent, Agent.owner_id == User.id)
            .outerjoin(UserControlPolicy, UserControlPolicy.user_id == User.id)
            .group_by(User.id, UserControlPolicy.id)
            .order_by(User.email.asc(), User.id.asc())
        )
    ).all()
    flag_rows = (
        await session.execute(
            select(UserFeatureFlag.user_id, UserFeatureFlag.flag_key).where(
                UserFeatureFlag.enabled.is_(True)
            )
        )
    ).all()
    flags_by_user: dict[int, list[str]] = {}
    for user_id, flag_key in flag_rows:
        flags_by_user.setdefault(int(user_id), []).append(str(flag_key))
    users = [
        AdminUserOut(
            id=user.id,
            email=user.email,
            is_admin=user.is_admin,
            created_at=user.created_at,
            agent_count=int(agent_count or 0),
            repo_count=int(repo_count or 0),
            control_policy=policy_dict(policy) if policy is not None else None,
            feature_flags=sorted(flags_by_user.get(user.id, [])),
        )
        for user, agent_count, repo_count, policy in rows
    ]
    total_users = int(await session.scalar(select(func.count(User.id))) or 0)
    total_agents = int(await session.scalar(select(func.count(Agent.id))) or 0)
    total_repos = int(
        await session.scalar(select(repo_count_expr).select_from(Agent)) or 0
    )
    now = datetime.now(timezone.utc)
    signups_24h = await _signup_count(session, now - timedelta(days=1))
    signups_7d = await _signup_count(session, now - timedelta(days=7))
    signups_30d = await _signup_count(session, now - timedelta(days=30))
    return AdminInventoryOut(
        totals=AdminInventoryTotalsOut(
            users=total_users,
            agents=total_agents,
            repos=total_repos,
        ),
        signups=AdminSignupTotalsOut(
            last_24h=signups_24h,
            last_7d=signups_7d,
            last_30d=signups_30d,
        ),
        users=users,
    )


async def _signup_count(session: AsyncSession, since: datetime) -> int:
    return int(
        await session.scalar(
            select(func.count(User.id)).where(User.created_at >= since)
        )
        or 0
    )


def _bounded_int(value: int | None, *, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    return max(minimum, min(int(value), maximum))


@router.get("/users/{user_id}/control-policy", response_model=AdminControlPolicyOut)
async def read_user_control_policy(
    user_id: int,
    session: AsyncSession = Depends(get_session),
) -> AdminControlPolicyOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    policy = await get_or_create_policy(session, user.id)
    return AdminControlPolicyOut(
        user_id=user.id,
        email=user.email,
        policy=policy_dict(policy),
    )


@router.put("/users/{user_id}/control-policy", response_model=AdminControlPolicyOut)
async def write_user_control_policy(
    user_id: int,
    body: AdminControlPolicyUpdateIn,
    session: AsyncSession = Depends(get_session),
) -> AdminControlPolicyOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    policy = await get_or_create_policy(session, user.id)
    if body.monthly_budget_cents is not None:
        policy.monthly_budget_cents = _bounded_int(
            body.monthly_budget_cents, minimum=0, maximum=10_000_000,
        ) or 0
    if body.run_budget_cents is not None:
        policy.run_budget_cents = _bounded_int(
            body.run_budget_cents, minimum=0, maximum=1_000_000,
        ) or 0
    if body.max_agent_calls_per_run is not None:
        policy.max_agent_calls_per_run = _bounded_int(
            body.max_agent_calls_per_run, minimum=1, maximum=100,
        ) or 1
    for field in (
        "require_approval_for_file_writes",
        "deny_external_network",
        "only_approved_agents",
        "pii_safe_mode",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(policy, field, bool(value))
    if body.approved_agents is not None:
        policy.approved_agents = [
            str(item).strip()
            for item in body.approved_agents[:200]
            if str(item).strip()
        ]
    await session.commit()
    await session.refresh(policy)
    return AdminControlPolicyOut(
        user_id=user.id,
        email=user.email,
        policy=policy_dict(policy),
    )


@router.post(
    "/users/{user_id}/platform-token",
    response_model=AdminPlatformTokenCreatedOut,
)
async def create_user_platform_token(
    user_id: int,
    body: AdminPlatformTokenCreateIn,
    session: AsyncSession = Depends(get_session),
) -> AdminPlatformTokenCreatedOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    ttl_seconds = _bounded_int(body.ttl_seconds, minimum=60, maximum=2_592_000) or 86400
    token = issue_token(user.id, ttl_seconds=ttl_seconds)
    return AdminPlatformTokenCreatedOut(
        user_id=user.id,
        email=user.email,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        ttl_seconds=ttl_seconds,
        label=(body.label or "").strip()[:160] or None,
    )


@router.delete("/users", response_model=AdminPurgeOut)
async def purge_users_and_agents(
    session: AsyncSession = Depends(get_session),
) -> AdminPurgeOut:
    agents = (await session.execute(select(Agent).order_by(Agent.id.asc()))).scalars().all()
    cleanup_failures: list[str] = []
    for agent in agents:
        failures = (
            _cleanup_external_agent_resources(agent.name)
            if _is_external_agent(agent)
            else _cleanup_agent_resources(agent.name, repo_owner=agent.gitea_owner)
        )
        if failures:
            cleanup_failures.extend(f"{agent.name}: {msg}" for msg in failures)
    if cleanup_failures:
        await session.rollback()
        raise HTTPException(
            502,
            "managed repo cleanup failed; database was not purged: "
            + "; ".join(cleanup_failures),
        )

    deleted_users = int(await session.scalar(select(func.count(User.id))) or 0)
    deleted_agents = int(await session.scalar(select(func.count(Agent.id))) or 0)

    protected_tables = {"platform_settings", "feature_flags", "alembic_version"}
    explicit_tables = set(ORG_TABLES) | set(HISTORY_TABLES)
    await _delete_tables(session, ORG_TABLES)
    await _delete_tables(session, HISTORY_TABLES)
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in protected_tables or table in explicit_tables:
            continue
        await session.execute(delete(table))
    await session.commit()
    return AdminPurgeOut(
        deleted_users=deleted_users,
        deleted_agents=deleted_agents,
    )
