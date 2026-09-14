from __future__ import annotations

import os
import tarfile
from pathlib import Path

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.database_bindings import (
    postgres_identifier,
    reconcile_agent_database_bindings,
)
from control_plane.database_resources import (
    read_agent_database_declarations,
    read_agent_database_declarations_from_tarball,
    read_agent_database_declarations_from_tarball_bytes,
)
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentDatabaseBinding,
    DatabaseBranch,
    DatabaseCluster,
    DatabaseProject,
    DatabaseProvisionEvent,
    DatabaseRole,
    Organization,
    User,
)
from control_plane.scaffold import _stamp_platform_files


def _write_manifest(tmp_path: Path, body: str) -> None:
    (tmp_path / "a2a.yaml").write_text(body)


def _tar_source(tmp_path: Path, body: str) -> Path:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a2a.yaml").write_text(body)
    (source_dir / "agent.py").write_text("class Demo: pass\n")
    tar_path = tmp_path / "source.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(source_dir / "a2a.yaml", arcname="project/a2a.yaml")
        archive.add(source_dir / "agent.py", arcname="project/agent.py")
    return tar_path


def test_read_agent_database_declarations_normalizes_defaults(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        """
name: db-agent
resources:
  databases:
    - name: App
""",
    )

    declarations = read_agent_database_declarations(tmp_path)

    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.name == "app"
    assert declaration.provider == "neon"
    assert declaration.engine == "postgres"
    assert declaration.scope == "user"
    assert declaration.branch == "main"
    assert declaration.access_mode == "read_write"
    assert declaration.env.url == "DATABASE_URL"
    assert declaration.migrations_path is None
    assert declaration.scale_to_zero is True


def test_postgres_identifier_truncates_with_stable_hash() -> None:
    original = "agent_188_" + ("long_database_name_" * 5) + "read_write"

    shortened = postgres_identifier(original)

    assert len(shortened.encode("utf-8")) == 63
    assert shortened == postgres_identifier(original)
    assert shortened != postgres_identifier(f"{original}_other")
    assert postgres_identifier("short-name") == "short_name"


def test_read_agent_database_declarations_accepts_org_branch_and_migrations(
    tmp_path: Path,
) -> None:
    _write_manifest(
        tmp_path,
        """
name: db-agent
resources:
  databases:
    - name: analytics
      engine: postgres
      scope: org
      branch: reporting
      access_mode: read_only
      env:
        url: ANALYTICS_DATABASE_URL
      migrations:
        path: db/migrations
      scale_to_zero: false
""",
    )

    [declaration] = read_agent_database_declarations(tmp_path)

    assert declaration.name == "analytics"
    assert declaration.scope == "org"
    assert declaration.branch == "reporting"
    assert declaration.access_mode == "read_only"
    assert declaration.env.url == "ANALYTICS_DATABASE_URL"
    assert declaration.migrations_path == "db/migrations"
    assert declaration.scale_to_zero is False


def test_read_agent_database_declarations_from_tarball(tmp_path: Path) -> None:
    tar_path = _tar_source(
        tmp_path,
        """
name: db-agent
resources:
  databases:
    - name: app
      env:
        url: APP_DATABASE_URL
""",
    )

    [declaration] = read_agent_database_declarations_from_tarball(tar_path)
    [memory_declaration] = read_agent_database_declarations_from_tarball_bytes(
        tar_path.read_bytes()
    )

    assert declaration.name == "app"
    assert memory_declaration == declaration
    assert declaration.env.url == "APP_DATABASE_URL"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            """
resources:
  databases: {}
""",
            "resources.databases must be a list",
        ),
        (
            """
resources:
  databases:
    - name: Bad_Name
""",
            "name must be a slug",
        ),
        (
            """
resources:
  databases:
    - name: app
      provider: rds
""",
            "provider must be one of",
        ),
        (
            """
resources:
  databases:
    - name: app
      scope: account
""",
            "scope must be one of",
        ),
        (
            """
resources:
  databases:
    - name: app
      env:
        url: database-url
""",
            "env.url must be an environment variable name",
        ),
        (
            """
resources:
  databases:
    - name: app
      migrations:
        path: ../db
""",
            "migrations.path must be a safe relative path",
        ),
    ],
)
def test_read_agent_database_declarations_rejects_invalid_contract(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    _write_manifest(tmp_path, body)

    with pytest.raises(ValueError, match=message):
        read_agent_database_declarations(tmp_path)


def test_stamp_platform_files_validates_database_declarations(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("class Demo: pass\n")
    (tmp_path / "requirements.txt").write_text("")
    _write_manifest(
        tmp_path,
        """
name: db-agent
entrypoint: agent:Demo
resources:
  databases:
    - name: app
      env:
        url: bad-env-name
""",
    )

    with pytest.raises(ValueError, match="env.url must be an environment variable name"):
        _stamp_platform_files(tmp_path, name="db-agent", entrypoint="agent:Demo")


@pytest.mark.asyncio
async def test_database_resource_models_persist_ownership_and_agent_binding() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        org = Organization(slug="acme", name="Acme", created_by_id=user.id)
        session.add(org)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            organization_id=org.id,
            name="db-agent",
            description="",
            version="0.1.0",
            image="registry/a",
            public=False,
            status="ready",
            card={},
        )
        cluster = DatabaseCluster(
            name="prod-neon-a",
            status="ready",
            region="us-east",
            storage_class="longhorn-replica-2",
        )
        session.add_all([agent, cluster])
        await session.flush()
        project = DatabaseProject(
            owner_id=None,
            organization_id=org.id,
            created_by_id=user.id,
            cluster_id=cluster.id,
            name="app",
            display_name="App",
            engine="postgres",
            status="ready",
            project_ref="neon-project-1",
        )
        session.add(project)
        await session.flush()
        branch = DatabaseBranch(
            project_id=project.id,
            name="main",
            status="ready",
            branch_ref="timeline-main",
            protected=True,
        )
        role = DatabaseRole(
            project_id=project.id,
            name="db-agent-rw",
            username="db_agent_rw",
            database_name="app",
            access_mode="read_write",
            status="ready",
            secret_ref="agent-db-agent-runtime",
        )
        session.add_all([branch, role])
        await session.flush()
        binding = AgentDatabaseBinding(
            agent_id=agent.id,
            organization_id=org.id,
            database_project_id=project.id,
            database_branch_id=branch.id,
            database_role_id=role.id,
            agent_name=agent.name,
            binding_name="app",
            env_var="DATABASE_URL",
            access_mode="read_write",
            status="ready",
            declaration_json={"name": "app", "scope": "org"},
        )
        event = DatabaseProvisionEvent(
            database_project_id=project.id,
            agent_id=agent.id,
            actor_user_id=user.id,
            event_type="agent_binding_ready",
            status="ok",
            message="bound db-agent to app",
        )
        session.add_all([binding, event])
        await session.commit()

    async with session_maker() as session:
        row = (
            await session.execute(
                select(AgentDatabaseBinding).where(
                    AgentDatabaseBinding.binding_name == "app"
                )
            )
        ).scalar_one()
        assert row.organization_id == org.id
        assert row.env_var == "DATABASE_URL"
        assert row.access_mode == "read_write"
        assert row.declaration_json == {"name": "app", "scope": "org"}


@pytest.mark.asyncio
async def test_agent_database_binding_name_is_unique_per_agent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="db-agent",
            description="",
            version="0.1.0",
            image="registry/a",
            public=False,
            status="ready",
            card={},
        )
        project = DatabaseProject(
            owner_id=user.id,
            created_by_id=user.id,
            name="app",
            display_name="App",
            status="ready",
        )
        session.add_all([agent, project])
        await session.flush()
        session.add_all(
            [
                AgentDatabaseBinding(
                    agent_id=agent.id,
                    user_id=user.id,
                    database_project_id=project.id,
                    agent_name=agent.name,
                    binding_name="app",
                ),
                AgentDatabaseBinding(
                    agent_id=agent.id,
                    user_id=user.id,
                    database_project_id=project.id,
                    agent_name=agent.name,
                    binding_name="app",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_reconcile_agent_database_bindings_creates_pending_resources(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="db-agent",
            description="",
            version="0.1.0",
            image="registry/a",
            public=False,
            status="building",
            card={},
        )
        session.add(agent)
        await session.flush()
        declarations = read_agent_database_declarations_from_tarball(
            _tar_source(
                tmp_path,
                """
resources:
  databases:
    - name: app
      branch: preview
      access_mode: read_only
      env:
        url: APP_DATABASE_URL
""",
            )
        )
        # The declaration parser is already covered through filesystem tests;
        # use the normalized value here to keep the reconciler assertions tight.
        result = await reconcile_agent_database_bindings(
            session,
            agent=agent,
            user=user,
            declarations=declarations,
        )
        assert result.requested == ["app"]

    async with session_maker() as session:
        project = (
            await session.execute(select(DatabaseProject).where(DatabaseProject.name == "app"))
        ).scalar_one()
        branch = (
            await session.execute(
                select(DatabaseBranch).where(DatabaseBranch.project_id == project.id)
            )
        ).scalar_one()
        role = (
            await session.execute(
                select(DatabaseRole).where(DatabaseRole.project_id == project.id)
            )
        ).scalar_one()
        binding = (
            await session.execute(
                select(AgentDatabaseBinding).where(
                    AgentDatabaseBinding.binding_name == "app"
                )
            )
        ).scalar_one()
        events = (
            await session.execute(
                select(DatabaseProvisionEvent).where(
                    DatabaseProvisionEvent.database_project_id == project.id
                )
            )
        ).scalars().all()
        assert project.status == "pending"
        assert project.default_branch_name == "preview"
        assert branch.name == "preview"
        assert role.access_mode == "read_only"
        assert role.secret_ref == "db-agent-agent-secrets"
        assert binding.env_var == "APP_DATABASE_URL"
        assert binding.declaration_json["provider"] == "neon"
        assert binding.status == "pending"
        assert binding.metadata_json["runtime_secret"] == "db-agent-agent-secrets"
        assert [event.event_type for event in events] == [
            "agent_database_binding_requested"
        ]


@pytest.mark.asyncio
async def test_reconcile_agent_database_bindings_marks_removed_when_declaration_drops(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_maker() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="db-agent",
            description="",
            version="0.1.0",
            image="registry/a",
            public=False,
            status="building",
            card={},
        )
        session.add(agent)
        await session.flush()
        declarations = read_agent_database_declarations_from_tarball(
            _tar_source(
                tmp_path,
                """
resources:
  databases:
    - name: app
""",
            )
        )
        await reconcile_agent_database_bindings(
            session,
            agent=agent,
            user=user,
            declarations=declarations,
        )
        removed = await reconcile_agent_database_bindings(
            session,
            agent=agent,
            user=user,
            declarations=[],
        )
        assert removed.removed == ["app"]

    async with session_maker() as session:
        binding = (
            await session.execute(
                select(AgentDatabaseBinding).where(
                    AgentDatabaseBinding.binding_name == "app"
                )
            )
        ).scalar_one()
        events = (
            await session.execute(
                select(DatabaseProvisionEvent).order_by(DatabaseProvisionEvent.id)
            )
        ).scalars().all()
        assert binding.status == "removed"
        assert [event.event_type for event in events] == [
            "agent_database_binding_requested",
            "agent_database_binding_removed",
        ]
