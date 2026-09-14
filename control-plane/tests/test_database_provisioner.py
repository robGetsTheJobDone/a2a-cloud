from __future__ import annotations

import io
import os
import sys
import tarfile
import types

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.database_provisioner import (
    DatabaseBackend,
    DatabaseMigration,
    DatabaseProvisioner,
    ProvisionedDatabaseRole,
    _admin_url_for_database,
    _grant_role_schema_access,
    migration_sql_from_tarball,
    redact_database_event_data,
    wait_for_database_ready,
)
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentDatabaseBinding,
    DatabaseBranch,
    DatabaseProject,
    DatabaseProvisionEvent,
    DatabaseRole,
    User,
)
from control_plane.secret_crypto import encrypt_secret


class FakeBackend(DatabaseBackend):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def ensure_binding(
        self,
        *,
        project: DatabaseProject,
        branch: DatabaseBranch,
        role: DatabaseRole,
        binding: AgentDatabaseBinding,
    ) -> ProvisionedDatabaseRole:
        self.calls += 1
        if self.fail:
            raise RuntimeError("backend failed with password=secret")
        return ProvisionedDatabaseRole(
            project_ref=f"tenant-{project.id}",
            branch_ref=f"timeline-{branch.id}",
            username=role.username,
            database_name=role.database_name,
            password="generated-password",
            connection_uri=f"postgresql://{role.username}:generated-password@db/{role.database_name}",
        )


class RecordingConnection:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute(self, command: str) -> None:
        self.commands.append(command)


async def _noop_ready(**_kwargs) -> None:
    return None


async def _session_maker() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return session_maker


async def _pending_binding(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    migrations_path: str | None = None,
) -> tuple[int, int]:
    async with session_maker() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name="db-agent",
            description="",
            version="0.1.0",
            image="registry/db-agent:latest",
            public=False,
            status="building",
            card={},
        )
        session.add(agent)
        await session.flush()
        project = DatabaseProject(
            owner_id=user.id,
            created_by_id=user.id,
            name="app",
            display_name="app",
            status="pending",
        )
        session.add(project)
        await session.flush()
        branch = DatabaseBranch(project_id=project.id, name="main", status="pending")
        role = DatabaseRole(
            project_id=project.id,
            name="agent-app-rw",
            username="agent_app_rw",
            database_name="app",
            access_mode="read_write",
            status="pending",
            secret_ref="db-agent-agent-secrets",
        )
        session.add_all([branch, role])
        await session.flush()
        binding = AgentDatabaseBinding(
            agent_id=agent.id,
            user_id=user.id,
            database_project_id=project.id,
            database_branch_id=branch.id,
            database_role_id=role.id,
            agent_name=agent.name,
            binding_name="app",
            env_var="DATABASE_URL",
            access_mode="read_write",
            status="pending",
            declaration_json={
                "name": "app",
                "provider": "neon",
                "engine": "postgres",
                "scope": "user",
                "branch": "main",
                "access_mode": "read_write",
                "env": {"url": "DATABASE_URL"},
                "migrations_path": migrations_path,
                "scale_to_zero": True,
            },
            metadata_json={
                "runtime_secret": "db-agent-agent-secrets",
                "secret_key": "DATABASE_URL",
            },
        )
        session.add(binding)
        await session.commit()
        return binding.id, project.id


def _tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, body in files.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_migration_sql_from_tarball_returns_ordered_sql_files() -> None:
    migrations = migration_sql_from_tarball(
        _tarball(
            {
                "db/migrations/002_seed.sql": "insert into items values (1);",
                "db/migrations/readme.md": "ignored",
                "db/migrations/001_init.sql": "create table items(id int);",
                "other/003.sql": "ignored",
            }
        ),
        "db/migrations",
    )

    assert migrations == [
        DatabaseMigration("db/migrations/001_init.sql", "create table items(id int);"),
        DatabaseMigration("db/migrations/002_seed.sql", "insert into items values (1);"),
    ]


@pytest.mark.asyncio
async def test_database_provisioner_marks_binding_ready_and_writes_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    session_maker = await _session_maker()
    binding_id, project_id = await _pending_binding(session_maker)
    writes: list[dict] = []
    backend = FakeBackend()
    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=backend,
        upsert_secret=lambda **kwargs: writes.append(kwargs),
        delete_secret=lambda **_kwargs: None,
        ready_check=_noop_ready,
    )

    await provisioner.reconcile_once()

    assert backend.calls == 1
    assert writes == [
        {
            "agent_name": "db-agent",
            "key": "DATABASE_URL",
            "value": "postgresql://agent_app_rw:generated-password@db/app",
            "owner_id": 1,
        }
    ]
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        project = await session.get(DatabaseProject, project_id)
        role = (
            await session.execute(select(DatabaseRole).where(DatabaseRole.project_id == project_id))
        ).scalar_one()
        events = (
            await session.execute(
                select(DatabaseProvisionEvent).order_by(DatabaseProvisionEvent.id)
            )
        ).scalars().all()
        assert binding.status == "ready"
        assert project.status == "ready"
        assert project.project_ref == f"tenant-{project_id}"
        assert role.password_ciphertext.startswith("fernet:")
        assert role.connection_uri_ciphertext.startswith("fernet:")
        assert [event.event_type for event in events] == [
            "database_binding_provisioning_started",
            "database_binding_ready",
        ]


@pytest.mark.asyncio
async def test_database_provisioner_repairs_legacy_overlong_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    session_maker = await _session_maker()
    binding_id, project_id = await _pending_binding(session_maker)
    async with session_maker() as session:
        role = (
            await session.execute(
                select(DatabaseRole).where(DatabaseRole.project_id == project_id)
            )
        ).scalar_one()
        role.username = "agent_188_" + "x" * 80
        role.database_name = "database_" + "y" * 80
        await session.commit()

    backend = FakeBackend()
    writes: list[dict] = []
    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=backend,
        upsert_secret=lambda **kwargs: writes.append(kwargs),
        delete_secret=lambda **_kwargs: None,
        ready_check=_noop_ready,
    )

    await provisioner.reconcile_once()

    assert backend.calls == 1
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        role = (
            await session.execute(
                select(DatabaseRole).where(DatabaseRole.project_id == project_id)
            )
        ).scalar_one()
        assert binding.status == "ready"
        assert len(role.username.encode("utf-8")) <= 63
        assert len(role.database_name.encode("utf-8")) <= 63
        assert writes[0]["value"].endswith(f"@db/{role.database_name}")


@pytest.mark.asyncio
async def test_database_provisioner_reuses_existing_role_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    session_maker = await _session_maker()
    binding_id, project_id = await _pending_binding(session_maker)
    connection_uri = "postgresql://agent_app_rw:stable-password@db/app"
    async with session_maker() as session:
        project = await session.get(DatabaseProject, project_id)
        branch = (
            await session.execute(
                select(DatabaseBranch).where(DatabaseBranch.project_id == project_id)
            )
        ).scalar_one()
        role = (
            await session.execute(
                select(DatabaseRole).where(DatabaseRole.project_id == project_id)
            )
        ).scalar_one()
        project.status = "ready"
        project.project_ref = "tenant-stable"
        branch.status = "ready"
        branch.branch_ref = "timeline-stable"
        role.password_ciphertext = encrypt_secret("stable-password")
        role.connection_uri_ciphertext = encrypt_secret(connection_uri)
        await session.commit()

    backend = FakeBackend()
    writes: list[dict] = []
    ready_uris: list[str] = []

    async def ready(**kwargs) -> None:
        ready_uris.append(kwargs["connection_uri"])

    def unexpected_projection(_agent_name: str) -> dict:
        raise AssertionError("stable credentials must not roll an existing runtime")

    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=backend,
        upsert_secret=lambda **kwargs: writes.append(kwargs),
        delete_secret=lambda **_kwargs: None,
        ready_check=ready,
        project_secret=unexpected_projection,
    )

    await provisioner.reconcile_once()

    assert backend.calls == 0
    assert ready_uris == [connection_uri]
    assert writes == [
        {
            "agent_name": "db-agent",
            "key": "DATABASE_URL",
            "value": connection_uri,
            "owner_id": 1,
        }
    ]
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        assert binding.status == "ready"


@pytest.mark.asyncio
async def test_database_provisioner_runs_migrations_before_secret_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    session_maker = await _session_maker()
    binding_id, _project_id = await _pending_binding(
        session_maker,
        migrations_path="db/migrations",
    )
    operations: list[str] = []

    async def migrate(**kwargs) -> list[str]:
        assert kwargs["connection_uri"] == "postgresql://agent_app_rw:generated-password@db/app"
        operations.append("migrate")
        return ["db/migrations/001_init.sql"]

    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=FakeBackend(),
        upsert_secret=lambda **_kwargs: operations.append("secret"),
        delete_secret=lambda **_kwargs: None,
        migrate=migrate,
        ready_check=_noop_ready,
    )

    await provisioner.reconcile_once()

    assert operations == ["migrate", "secret"]
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        events = (
            await session.execute(
                select(DatabaseProvisionEvent).order_by(DatabaseProvisionEvent.id)
            )
        ).scalars().all()
        assert binding.status == "ready"
        assert [event.event_type for event in events] == [
            "database_binding_provisioning_started",
            "database_binding_migrations_started",
            "database_binding_migrations_applied",
            "database_binding_ready",
        ]
        assert events[2].data["migration_files"] == ["db/migrations/001_init.sql"]


@pytest.mark.asyncio
async def test_wait_for_database_ready_retries_transient_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeConnection:
        async def execute(self, command: str) -> None:
            calls.append(command)

        async def close(self) -> None:
            calls.append("close")

    async def connect(connection_uri: str) -> FakeConnection:
        calls.append(connection_uri)
        if calls.count(connection_uri) == 1:
            raise RuntimeError("FATAL: the database system is starting up")
        return FakeConnection()

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    binding = AgentDatabaseBinding(id=123, agent_id=1, user_id=1, binding_name="app")

    await wait_for_database_ready(
        binding=binding,
        connection_uri="postgresql://agent:secret@db/app",
        attempts=2,
        retry_seconds=0,
    )

    assert calls == [
        "postgresql://agent:secret@db/app",
        "postgresql://agent:secret@db/app",
        "SELECT 1",
        "close",
    ]


@pytest.mark.asyncio
async def test_database_provisioner_fails_without_writing_secret_when_migration_fails() -> None:
    session_maker = await _session_maker()
    binding_id, _project_id = await _pending_binding(
        session_maker,
        migrations_path="db/migrations",
    )
    writes: list[dict] = []

    async def migrate(**_kwargs) -> list[str]:
        raise RuntimeError("migration failed with password=secret")

    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=FakeBackend(),
        upsert_secret=lambda **kwargs: writes.append(kwargs),
        delete_secret=lambda **_kwargs: None,
        migrate=migrate,
        ready_check=_noop_ready,
    )

    await provisioner.reconcile_once()

    assert writes == []
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        events = (
            await session.execute(
                select(DatabaseProvisionEvent).order_by(DatabaseProvisionEvent.id)
            )
        ).scalars().all()
        assert binding.status == "failed"
        assert [event.event_type for event in events] == [
            "database_binding_provisioning_started",
            "database_binding_migrations_started",
            "database_binding_failed",
        ]
        assert events[-1].message == "migration failed with password=[redacted]"


@pytest.mark.asyncio
async def test_database_provisioner_removes_runtime_secret_key() -> None:
    session_maker = await _session_maker()
    binding_id, _project_id = await _pending_binding(session_maker)
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        binding.status = "removed"
        await session.commit()
    deletes: list[dict] = []
    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=FakeBackend(),
        upsert_secret=lambda **_kwargs: None,
        delete_secret=lambda **kwargs: deletes.append(kwargs),
        ready_check=_noop_ready,
    )

    await provisioner.reconcile_once()

    assert deletes == [{"agent_name": "db-agent", "key": "DATABASE_URL"}]
    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        event = (
            await session.execute(
                select(DatabaseProvisionEvent).where(
                    DatabaseProvisionEvent.event_type == "database_binding_secret_revoked"
                )
            )
        ).scalar_one()
        assert binding.metadata_json["secret_revoked_at"]
        assert event.status == "ok"


@pytest.mark.asyncio
async def test_database_provisioner_records_redacted_failure() -> None:
    session_maker = await _session_maker()
    binding_id, _project_id = await _pending_binding(session_maker)
    provisioner = DatabaseProvisioner(
        session_maker=session_maker,
        backend=FakeBackend(fail=True),
        upsert_secret=lambda **_kwargs: None,
        delete_secret=lambda **_kwargs: None,
        ready_check=_noop_ready,
    )

    await provisioner.reconcile_once()

    async with session_maker() as session:
        binding = await session.get(AgentDatabaseBinding, binding_id)
        event = (
            await session.execute(
                select(DatabaseProvisionEvent).where(
                    DatabaseProvisionEvent.event_type == "database_binding_failed"
                )
            )
        ).scalar_one()
        assert binding.status == "failed"
        assert "secret" not in str(event.data)
        assert "password" not in str(event.data)
        assert event.message == "backend failed with password=[redacted]"


def test_redact_database_event_data_removes_urls_and_secret_fields() -> None:
    payload = {
        "connection_uri": "postgresql://user:pass@example/app",
        "nested": {"password": "secret", "safe": "value"},
        "items": ["postgresql://user:pass@example/app"],
    }

    assert redact_database_event_data(payload) == {
        "connection_uri": "[redacted]",
        "nested": {"password": "[redacted]", "safe": "value"},
        "items": ["[redacted]"],
    }


def test_migration_parser_ignores_comment_only_files() -> None:
    bundle = _tarball(
        {
            "db/migrations/001_compat.sql": (
                "-- Retained for compatibility.\n"
                "/* There is deliberately no executable SQL here. */\n"
            ),
            "db/migrations/002_schema.sql": (
                "-- Create the product table.\n"
                "CREATE TABLE launch_audits (audit_id TEXT PRIMARY KEY);\n"
            ),
        }
    )

    migrations = migration_sql_from_tarball(bundle, "db/migrations")

    assert [migration.path for migration in migrations] == [
        "db/migrations/002_schema.sql"
    ]


def test_admin_url_for_database_replaces_path_and_keeps_query() -> None:
    assert (
        _admin_url_for_database(
            "postgresql://admin:secret@neon-compute.database.svc:55433/postgres?sslmode=require",
            "app_db",
        )
        == "postgresql://admin:secret@neon-compute.database.svc:55433/app_db?sslmode=require"
    )


@pytest.mark.asyncio
async def test_grant_role_schema_access_allows_read_write_schema_ddl() -> None:
    conn = RecordingConnection()
    role = DatabaseRole(
        project_id=1,
        name="agent-app-rw",
        username="agent_app_rw",
        database_name="app",
        access_mode="read_write",
    )

    await _grant_role_schema_access(conn, role=role)

    assert conn.commands == [
        'GRANT USAGE, CREATE ON SCHEMA public TO "agent_app_rw"',
    ]


@pytest.mark.asyncio
async def test_grant_role_schema_access_limits_read_only_to_existing_objects() -> None:
    conn = RecordingConnection()
    role = DatabaseRole(
        project_id=1,
        name="agent-app-ro",
        username="agent_app_ro",
        database_name="app",
        access_mode="read_only",
    )

    await _grant_role_schema_access(conn, role=role)

    assert conn.commands == [
        'GRANT USAGE ON SCHEMA public TO "agent_app_ro"',
        'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "agent_app_ro"',
        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "agent_app_ro"',
    ]
