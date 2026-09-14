from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..database_provisioner import database_provisioner_health
from ..db import get_session
from ..models import (
    AgentDatabaseBinding,
    DatabaseBranch,
    DatabaseProject,
    DatabaseProvisionEvent,
    DatabaseRole,
    Organization,
    OrganizationMember,
    User,
)

router = APIRouter(prefix="/v1/me/databases", tags=["databases"])

ADMIN_ROLES = {"owner", "admin"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class DatabaseProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    display_name: str | None = Field(default=None, max_length=160)
    scope: str = Field(default="user", pattern="^(user|org)$")
    organization_slug: str | None = Field(default=None, max_length=96)
    engine: str = Field(default="postgres", pattern="^postgres$")


class DatabaseProjectOut(BaseModel):
    id: int
    name: str
    display_name: str
    engine: str
    status: str
    scope: str
    organization_slug: str | None = None
    default_branch_name: str
    created_at: datetime
    updated_at: datetime | None = None


class DatabaseProvisionEventOut(BaseModel):
    id: int
    event_type: str
    status: str
    message: str
    data: dict = Field(default_factory=dict)
    created_at: datetime


class DatabaseBranchOut(BaseModel):
    id: int
    name: str
    status: str
    branch_ref: str | None = None
    protected: bool


class DatabaseRoleOut(BaseModel):
    id: int
    name: str
    username: str
    database_name: str
    access_mode: str
    status: str
    secret_ref: str | None = None


class AgentDatabaseBindingOut(BaseModel):
    id: int
    agent_id: int
    agent_name: str
    binding_name: str
    env_var: str
    access_mode: str
    status: str
    database_branch_id: int | None = None
    database_role_id: int | None = None
    metadata_json: dict = Field(default_factory=dict)


class DatabaseProjectDetailOut(DatabaseProjectOut):
    project_ref: str | None = None
    branches: list[DatabaseBranchOut] = Field(default_factory=list)
    roles: list[DatabaseRoleOut] = Field(default_factory=list)
    bindings: list[AgentDatabaseBindingOut] = Field(default_factory=list)
    events: list[DatabaseProvisionEventOut] = Field(default_factory=list)


class DatabaseProvisionerStatusOut(BaseModel):
    enabled: bool
    configured: bool
    ok: bool
    metrics: dict = Field(default_factory=dict)


def _normalize_name(value: str) -> str:
    name = value.strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(400, "database name must be a slug")
    return name


async def _admin_org(
    slug: str,
    user: User,
    session: AsyncSession,
) -> Organization:
    row = (
        await session.execute(
            select(Organization, OrganizationMember)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                Organization.slug == slug,
                OrganizationMember.user_id == user.id,
                OrganizationMember.active == True,  # noqa: E712
            )
        )
    ).first()
    if row is None:
        raise HTTPException(404, "organization not found")
    org, member = row
    if member.role not in ADMIN_ROLES:
        raise HTTPException(403, "organization admin required")
    return org


async def _member_org_ids(user: User, session: AsyncSession) -> dict[int, str]:
    rows = (
        await session.execute(
            select(Organization.id, Organization.slug)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.active == True,  # noqa: E712
            )
        )
    ).all()
    return {org_id: slug for org_id, slug in rows}


async def _visible_project(
    project_id: int,
    user: User,
    session: AsyncSession,
) -> tuple[DatabaseProject, str | None]:
    project = await session.get(DatabaseProject, project_id)
    if project is None:
        raise HTTPException(404, "database project not found")
    if project.owner_id == user.id:
        return project, None
    if project.organization_id is not None:
        org_slugs = await _member_org_ids(user, session)
        if project.organization_id in org_slugs:
            return project, org_slugs[project.organization_id]
    raise HTTPException(404, "database project not found")


def _project_out(
    project: DatabaseProject,
    *,
    organization_slug: str | None = None,
) -> DatabaseProjectOut:
    return DatabaseProjectOut(
        id=project.id,
        name=project.name,
        display_name=project.display_name,
        engine=project.engine,
        status=project.status,
        scope="org" if project.organization_id is not None else "user",
        organization_slug=organization_slug,
        default_branch_name=project.default_branch_name,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("", response_model=list[DatabaseProjectOut])
async def list_database_projects(
    organization_slug: str | None = Query(default=None),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DatabaseProjectOut]:
    if organization_slug:
        org = await _admin_org(organization_slug, user, session)
        rows = (
            await session.execute(
                select(DatabaseProject)
                .where(DatabaseProject.organization_id == org.id)
                .order_by(DatabaseProject.created_at.desc(), DatabaseProject.id.desc())
            )
        ).scalars().all()
        return [_project_out(project, organization_slug=org.slug) for project in rows]

    org_slugs = await _member_org_ids(user, session)
    rows = (
        await session.execute(
            select(DatabaseProject)
            .where(
                (DatabaseProject.owner_id == user.id)
                | (DatabaseProject.organization_id.in_(org_slugs.keys()))
            )
            .order_by(DatabaseProject.created_at.desc(), DatabaseProject.id.desc())
        )
    ).scalars().all()
    return [
        _project_out(project, organization_slug=org_slugs.get(project.organization_id))
        for project in rows
    ]


@router.get("/provisioner-status", response_model=DatabaseProvisionerStatusOut)
async def get_database_provisioner_status(
    _user: User = Depends(current_user),
) -> DatabaseProvisionerStatusOut:
    return DatabaseProvisionerStatusOut(**database_provisioner_health())


@router.get("/{project_id}", response_model=DatabaseProjectDetailOut)
async def get_database_project(
    project_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DatabaseProjectDetailOut:
    project, org_slug = await _visible_project(project_id, user, session)
    branches = (
        await session.execute(
            select(DatabaseBranch)
            .where(DatabaseBranch.project_id == project.id)
            .order_by(DatabaseBranch.created_at, DatabaseBranch.id)
        )
    ).scalars().all()
    roles = (
        await session.execute(
            select(DatabaseRole)
            .where(DatabaseRole.project_id == project.id)
            .order_by(DatabaseRole.created_at, DatabaseRole.id)
        )
    ).scalars().all()
    bindings = (
        await session.execute(
            select(AgentDatabaseBinding)
            .where(AgentDatabaseBinding.database_project_id == project.id)
            .order_by(AgentDatabaseBinding.created_at, AgentDatabaseBinding.id)
        )
    ).scalars().all()
    events = (
        await session.execute(
            select(DatabaseProvisionEvent)
            .where(DatabaseProvisionEvent.database_project_id == project.id)
            .order_by(DatabaseProvisionEvent.created_at.desc(), DatabaseProvisionEvent.id.desc())
            .limit(100)
        )
    ).scalars().all()
    base = _project_out(project, organization_slug=org_slug).model_dump()
    return DatabaseProjectDetailOut(
        **base,
        project_ref=project.project_ref,
        branches=[
            DatabaseBranchOut(
                id=row.id,
                name=row.name,
                status=row.status,
                branch_ref=row.branch_ref,
                protected=row.protected,
            )
            for row in branches
        ],
        roles=[
            DatabaseRoleOut(
                id=row.id,
                name=row.name,
                username=row.username,
                database_name=row.database_name,
                access_mode=row.access_mode,
                status=row.status,
                secret_ref=row.secret_ref,
            )
            for row in roles
        ],
        bindings=[
            AgentDatabaseBindingOut(
                id=row.id,
                agent_id=row.agent_id,
                agent_name=row.agent_name,
                binding_name=row.binding_name,
                env_var=row.env_var,
                access_mode=row.access_mode,
                status=row.status,
                database_branch_id=row.database_branch_id,
                database_role_id=row.database_role_id,
                metadata_json=row.metadata_json or {},
            )
            for row in bindings
        ],
        events=[
            DatabaseProvisionEventOut(
                id=row.id,
                event_type=row.event_type,
                status=row.status,
                message=row.message,
                data=row.data or {},
                created_at=row.created_at,
            )
            for row in events
        ],
    )


@router.post("", response_model=DatabaseProjectOut, status_code=201)
async def create_database_project(
    body: DatabaseProjectCreateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> DatabaseProjectOut:
    name = _normalize_name(body.name)
    display_name = (body.display_name or name).strip() or name
    org: Organization | None = None
    if body.scope == "org":
        if not body.organization_slug:
            raise HTTPException(400, "organization_slug is required for org databases")
        org = await _admin_org(body.organization_slug, user, session)
    elif body.organization_slug:
        raise HTTPException(400, "organization_slug requires scope=org")

    project = DatabaseProject(
        owner_id=user.id if org is None else None,
        organization_id=org.id if org is not None else None,
        created_by_id=user.id,
        name=name,
        display_name=display_name,
        engine=body.engine,
        status="pending",
        default_branch_name="main",
        metadata_json={"requested_by": "control-plane-api"},
    )
    session.add(project)
    try:
        await session.flush()
        branch = DatabaseBranch(
            project_id=project.id,
            name="main",
            status="pending",
            protected=True,
        )
        event = DatabaseProvisionEvent(
            database_project_id=project.id,
            actor_user_id=user.id,
            event_type="database_project_requested",
            status="queued",
            message=f"requested database project {name}",
            data={
                "scope": body.scope,
                "organization_slug": org.slug if org is not None else None,
                "engine": body.engine,
                "default_branch": "main",
            },
        )
        session.add_all([branch, event])
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "database project already exists") from exc

    await session.refresh(project)
    return _project_out(
        project,
        organization_slug=org.slug if org is not None else None,
    )
