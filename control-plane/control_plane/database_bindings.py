from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent_secret_names import agent_runtime_secret_name
from .database_resources import AgentDatabaseDeclaration
from .models import (
    Agent,
    AgentDatabaseBinding,
    DatabaseBranch,
    DatabaseProject,
    DatabaseProvisionEvent,
    DatabaseRole,
    User,
)


@dataclass(frozen=True)
class DatabaseBindingReconcileResult:
    requested: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


POSTGRES_IDENTIFIER_MAX_BYTES = 63


def postgres_identifier(value: str) -> str:
    """Return a deterministic, collision-resistant PostgreSQL identifier."""
    normalized = value.replace("-", "_")
    encoded = normalized.encode("utf-8")
    if len(encoded) <= POSTGRES_IDENTIFIER_MAX_BYTES:
        return normalized
    digest = hashlib.sha256(encoded).hexdigest()[:10]
    suffix = f"_{digest}"
    prefix_budget = POSTGRES_IDENTIFIER_MAX_BYTES - len(suffix)
    prefix = encoded[:prefix_budget].decode("utf-8", "ignore")
    return f"{prefix}{suffix}"


async def reconcile_agent_database_bindings(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    declarations: list[AgentDatabaseDeclaration],
) -> DatabaseBindingReconcileResult:
    """Persist platform database resources declared by an agent source repo."""

    if agent.id is None:
        await session.flush()
    if any(item.scope == "org" for item in declarations) and agent.organization_id is None:
        raise ValueError("org database declarations require an organization-scoped agent")

    requested: list[str] = []
    active_names = {item.name for item in declarations}
    for declaration in declarations:
        project = await _ensure_project(session, agent=agent, user=user, declaration=declaration)
        branch = await _ensure_branch(session, project=project, name=declaration.branch)
        role = await _ensure_role(
            session,
            agent=agent,
            project=project,
            branch=branch,
            declaration=declaration,
        )
        binding = await _binding(session, agent.id, declaration.name)
        if binding is None:
            binding = AgentDatabaseBinding(
                agent_id=agent.id,
                agent_name=agent.name,
                binding_name=declaration.name,
            )
            session.add(binding)
        binding.user_id = user.id if declaration.scope == "user" else None
        binding.organization_id = agent.organization_id if declaration.scope == "org" else None
        binding.database_project_id = project.id
        binding.database_branch_id = branch.id
        binding.database_role_id = role.id
        binding.agent_name = agent.name
        binding.env_var = declaration.env.url
        binding.access_mode = declaration.access_mode
        binding.status = "pending"
        binding.declaration_json = {
            "name": declaration.name,
            "provider": declaration.provider,
            "engine": declaration.engine,
            "scope": declaration.scope,
            "branch": declaration.branch,
            "access_mode": declaration.access_mode,
            "env": {"url": declaration.env.url},
            "migrations_path": declaration.migrations_path,
            "scale_to_zero": declaration.scale_to_zero,
        }
        binding.metadata_json = {
            "runtime_secret": agent_runtime_secret_name(agent.name),
            "secret_key": declaration.env.url,
        }
        session.add(
            DatabaseProvisionEvent(
                database_project_id=project.id,
                agent_id=agent.id,
                actor_user_id=user.id,
                event_type="agent_database_binding_requested",
                status="queued",
                message=f"requested {declaration.name} for {agent.name}",
                data={
                    "binding_name": declaration.name,
                    "branch": declaration.branch,
                    "access_mode": declaration.access_mode,
                    "env_var": declaration.env.url,
                    "runtime_secret": agent_runtime_secret_name(agent.name),
                },
            )
        )
        requested.append(declaration.name)

    removed: list[str] = []
    existing = (
        await session.execute(
            select(AgentDatabaseBinding).where(AgentDatabaseBinding.agent_id == agent.id)
        )
    ).scalars().all()
    for binding in existing:
        if binding.binding_name in active_names or binding.status == "removed":
            continue
        binding.status = "removed"
        session.add(
            DatabaseProvisionEvent(
                database_project_id=binding.database_project_id,
                agent_id=agent.id,
                actor_user_id=user.id,
                event_type="agent_database_binding_removed",
                status="queued",
                message=f"removed {binding.binding_name} for {agent.name}",
                data={
                    "binding_name": binding.binding_name,
                    "runtime_secret": agent_runtime_secret_name(agent.name),
                    "env_var": binding.env_var,
                },
            )
        )
        removed.append(binding.binding_name)

    await session.commit()
    return DatabaseBindingReconcileResult(requested=requested, removed=removed)


async def _ensure_project(
    session: AsyncSession,
    *,
    agent: Agent,
    user: User,
    declaration: AgentDatabaseDeclaration,
) -> DatabaseProject:
    if declaration.scope == "org":
        query = select(DatabaseProject).where(
            DatabaseProject.organization_id == agent.organization_id,
            DatabaseProject.name == declaration.name,
        )
        owner_id = None
        organization_id = agent.organization_id
    else:
        query = select(DatabaseProject).where(
            DatabaseProject.owner_id == user.id,
            DatabaseProject.name == declaration.name,
        )
        owner_id = user.id
        organization_id = None
    project = (await session.execute(query)).scalar_one_or_none()
    if project is not None:
        return project
    project = DatabaseProject(
        owner_id=owner_id,
        organization_id=organization_id,
        created_by_id=user.id,
        name=declaration.name,
        display_name=declaration.name,
        engine=declaration.engine,
        status="pending",
        default_branch_name=declaration.branch,
        metadata_json={
            "requested_by": "agent_declaration",
            "scale_to_zero": declaration.scale_to_zero,
        },
    )
    session.add(project)
    await session.flush()
    return project


async def _ensure_branch(
    session: AsyncSession,
    *,
    project: DatabaseProject,
    name: str,
) -> DatabaseBranch:
    branch = (
        await session.execute(
            select(DatabaseBranch).where(
                DatabaseBranch.project_id == project.id,
                DatabaseBranch.name == name,
            )
        )
    ).scalar_one_or_none()
    if branch is not None:
        return branch
    branch = DatabaseBranch(
        project_id=project.id,
        name=name,
        status="pending",
        protected=name == project.default_branch_name,
    )
    session.add(branch)
    await session.flush()
    return branch


async def _ensure_role(
    session: AsyncSession,
    *,
    agent: Agent,
    project: DatabaseProject,
    branch: DatabaseBranch,
    declaration: AgentDatabaseDeclaration,
) -> DatabaseRole:
    role_name = f"agent-{agent.id}-{declaration.name}-{declaration.access_mode}"[:96]
    role = (
        await session.execute(
            select(DatabaseRole).where(
                DatabaseRole.project_id == project.id,
                DatabaseRole.name == role_name,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        role = DatabaseRole(
            project_id=project.id,
            name=role_name,
            username=postgres_identifier(role_name),
            database_name=postgres_identifier(declaration.name),
        )
        session.add(role)
    role.branch_id = branch.id
    role.access_mode = declaration.access_mode
    role.status = "pending"
    role.secret_ref = agent_runtime_secret_name(agent.name)
    await session.flush()
    return role


async def _binding(
    session: AsyncSession,
    agent_id: int,
    name: str,
) -> AgentDatabaseBinding | None:
    return (
        await session.execute(
            select(AgentDatabaseBinding).where(
                AgentDatabaseBinding.agent_id == agent_id,
                AgentDatabaseBinding.binding_name == name,
            )
        )
    ).scalar_one_or_none()
