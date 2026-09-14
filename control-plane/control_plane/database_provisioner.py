from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import io
import logging
from pathlib import PurePosixPath
import re
import tarfile
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import secrets

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_secrets import (
    delete_agent_secret_value,
    ensure_agent_secret_projection,
    upsert_agent_secret_value,
)
from .config import settings
from .database_bindings import postgres_identifier
from .db import SessionLocal
from .gitea import source_tarball_from_repo
from .models import (
    Agent,
    AgentDatabaseBinding,
    DatabaseBranch,
    DatabaseProject,
    DatabaseProvisionEvent,
    DatabaseRole,
)
from .secret_crypto import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

READY = "ready"
PENDING = "pending"
PROVISIONING = "provisioning"
FAILED = "failed"
REMOVED = "removed"
ADMIN_CONNECT_ATTEMPTS = 30
ADMIN_CONNECT_RETRY_SECONDS = 2.0
RUNTIME_CONNECT_ATTEMPTS = 30
RUNTIME_CONNECT_RETRY_SECONDS = 2.0
MAX_MIGRATION_FILES = 100
MAX_MIGRATION_FILE_BYTES = 1024 * 1024
MAX_MIGRATION_TOTAL_BYTES = 10 * 1024 * 1024

SENSITIVE_KEYS = {
    "password",
    "pgpassword",
    "database_url",
    "connection_uri",
    "connection_url",
    "url",
    "uri",
    "secret",
    "token",
}
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(password|secret|token|database_url|connection_uri|connection_url|uri|url)\s*=\s*[^,\s]+"
)


@dataclass
class DatabaseProvisionerMetrics:
    reconcile_runs: int = 0
    queue_depth: int = 0
    create_attempts: int = 0
    remove_attempts: int = 0
    ready_bindings: int = 0
    failed_bindings: int = 0
    secret_writes: int = 0
    secret_deletes: int = 0
    failures: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "reconcile_runs": self.reconcile_runs,
            "queue_depth": self.queue_depth,
            "create_attempts": self.create_attempts,
            "remove_attempts": self.remove_attempts,
            "ready_bindings": self.ready_bindings,
            "failed_bindings": self.failed_bindings,
            "secret_writes": self.secret_writes,
            "secret_deletes": self.secret_deletes,
            "failures": self.failures,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
        }


metrics = DatabaseProvisionerMetrics()


@dataclass(frozen=True)
class ProvisionedDatabaseRole:
    project_ref: str
    branch_ref: str
    username: str
    database_name: str
    password: str
    connection_uri: str


@dataclass(frozen=True)
class DatabaseMigration:
    path: str
    sql: str


class DatabaseBackend:
    async def ensure_binding(
        self,
        *,
        project: DatabaseProject,
        branch: DatabaseBranch,
        role: DatabaseRole,
        binding: AgentDatabaseBinding,
    ) -> ProvisionedDatabaseRole:
        raise NotImplementedError


class PostgresAdminBackend(DatabaseBackend):
    def __init__(
        self,
        *,
        admin_url: str,
        pageserver_url: str | None = None,
        pg_version: int = 16,
        runtime_host: str | None = None,
        runtime_port: int | None = None,
        sslmode: str | None = None,
    ) -> None:
        self.admin_url = admin_url
        self.pageserver_url = pageserver_url.rstrip("/") if pageserver_url else None
        self.pg_version = pg_version
        self.runtime_host = runtime_host
        self.runtime_port = runtime_port
        self.sslmode = sslmode

    async def ensure_binding(
        self,
        *,
        project: DatabaseProject,
        branch: DatabaseBranch,
        role: DatabaseRole,
        binding: AgentDatabaseBinding,
    ) -> ProvisionedDatabaseRole:
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - dependency exists in runtime image
            raise RuntimeError("asyncpg is required for database provisioning") from exc

        password = secrets.token_urlsafe(32)
        project_ref, branch_ref = await self._ensure_neon_refs(project=project, branch=branch)
        for attempt in range(1, ADMIN_CONNECT_ATTEMPTS + 1):
            conn = None
            try:
                conn = await asyncpg.connect(self.admin_url)
                try:
                    await conn.execute(f"CREATE DATABASE {_quote_ident(role.database_name)}")
                except asyncpg.DuplicateDatabaseError:
                    pass
                role_exists = await conn.fetchval(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1",
                    role.username,
                )
                if not role_exists:
                    await conn.execute(
                        f"CREATE ROLE {_quote_ident(role.username)} LOGIN PASSWORD {_quote_literal(password)}"
                    )
                else:
                    await conn.execute(
                        f"ALTER ROLE {_quote_ident(role.username)} WITH LOGIN PASSWORD {_quote_literal(password)}"
                    )
                await _grant_role_access(conn, role=role)
                target_conn = await asyncpg.connect(
                    _admin_url_for_database(self.admin_url, role.database_name)
                )
                try:
                    await _grant_role_schema_access(target_conn, role=role)
                finally:
                    await target_conn.close()
                connection_uri = _runtime_connection_uri(
                    self.admin_url,
                    username=role.username,
                    password=password,
                    database=role.database_name,
                    runtime_host=self.runtime_host,
                    runtime_port=self.runtime_port,
                    sslmode=self.sslmode,
                )
                return ProvisionedDatabaseRole(
                    project_ref=project_ref,
                    branch_ref=branch_ref,
                    username=role.username,
                    database_name=role.database_name,
                    password=password,
                    connection_uri=connection_uri,
                )
            except Exception as exc:
                if attempt >= ADMIN_CONNECT_ATTEMPTS or not _is_transient_admin_error(exc):
                    raise
                log.info(
                    "database admin endpoint not ready; retrying binding provision",
                    extra={
                        "binding_id": binding.id,
                        "attempt": attempt,
                        "error": redact_database_message(str(exc)),
                    },
                )
                await asyncio.sleep(ADMIN_CONNECT_RETRY_SECONDS)
            finally:
                if conn is not None:
                    await conn.close()
        raise RuntimeError("database admin endpoint did not become ready")

    async def _ensure_neon_refs(
        self,
        *,
        project: DatabaseProject,
        branch: DatabaseBranch,
    ) -> tuple[str, str]:
        if not self.pageserver_url:
            return (
                project.project_ref or f"postgres-project-{project.id or project.name}",
                branch.branch_ref or f"postgres-branch-{branch.id or branch.name}",
            )
        tenant_id = project.project_ref or secrets.token_hex(16)
        timeline_id = branch.branch_ref or secrets.token_hex(16)
        async with httpx.AsyncClient(timeout=30) as client:
            if not project.project_ref:
                response = await client.put(
                    f"{self.pageserver_url}/v1/tenant/{tenant_id}/location_config",
                    json={
                        "mode": "AttachedSingle",
                        "generation": 1,
                        "tenant_conf": {},
                    },
                )
                if response.status_code not in {200, 201, 202, 409}:
                    response.raise_for_status()
            if not branch.branch_ref:
                response = await client.post(
                    f"{self.pageserver_url}/v1/tenant/{tenant_id}/timeline/",
                    json={
                        "new_timeline_id": timeline_id,
                        "pg_version": self.pg_version,
                    },
                )
                if response.status_code not in {200, 201, 202, 409}:
                    response.raise_for_status()
        return tenant_id, timeline_id


async def _grant_role_access(conn: Any, *, role: DatabaseRole) -> None:
    database = _quote_ident(role.database_name)
    username = _quote_ident(role.username)
    if role.access_mode == "read_only":
        await conn.execute(f"GRANT CONNECT ON DATABASE {database} TO {username}")
        return
    await conn.execute(f"GRANT ALL PRIVILEGES ON DATABASE {database} TO {username}")


async def _grant_role_schema_access(conn: Any, *, role: DatabaseRole) -> None:
    username = _quote_ident(role.username)
    if role.access_mode == "read_only":
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {username}")
        await conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {username}")
        await conn.execute(
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {username}"
        )
        return
    await conn.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {username}")


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _admin_url_for_database(admin_url: str, database: str) -> str:
    parsed = urlsplit(admin_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{quote(database)}",
            parsed.query,
            "",
        )
    )


def _is_transient_admin_error(exc: Exception) -> bool:
    message = str(exc).lower()
    transient_fragments = (
        "database system is starting up",
        "database system is shutting down",
        "the database system is in recovery mode",
        "connection refused",
        "connection reset by peer",
        "server closed the connection unexpectedly",
        "could not connect to server",
        "timeout",
        "timed out",
    )
    return any(fragment in message for fragment in transient_fragments)


def _runtime_connection_uri(
    admin_url: str,
    *,
    username: str,
    password: str,
    database: str,
    runtime_host: str | None,
    runtime_port: int | None,
    sslmode: str | None,
) -> str:
    parsed = urlsplit(admin_url)
    host = runtime_host or (parsed.hostname or "localhost")
    port = runtime_port if runtime_port is not None else parsed.port
    netloc = f"{quote(username)}:{quote(password)}@{host}"
    if port is not None:
        netloc += f":{port}"
    query = parsed.query
    if sslmode:
        query = f"{query}&sslmode={quote(sslmode)}" if query else f"sslmode={quote(sslmode)}"
    return urlunsplit(("postgresql", netloc, f"/{quote(database)}", query, ""))


async def run_database_migrations(
    *,
    agent: Agent,
    binding: AgentDatabaseBinding,
    connection_uri: str,
) -> list[str]:
    """Run SQL migrations declared by an agent source repository."""

    declaration = binding.declaration_json or {}
    migrations_path = str(declaration.get("migrations_path") or "").strip()
    if not migrations_path:
        return []
    tarball, _head_sha = await asyncio.to_thread(
        source_tarball_from_repo,
        agent.name,
        owner=agent.gitea_owner,
    )
    migrations = migration_sql_from_tarball(tarball, migrations_path)
    if not migrations:
        raise RuntimeError(f"no SQL migration files found at {migrations_path}")
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - dependency exists in runtime image
        raise RuntimeError("asyncpg is required for database migrations") from exc

    conn = await asyncpg.connect(connection_uri)
    try:
        for migration in migrations:
            await conn.execute(migration.sql)
    finally:
        await conn.close()
    return [migration.path for migration in migrations]


async def wait_for_database_ready(
    *,
    binding: AgentDatabaseBinding,
    connection_uri: str,
    attempts: int = RUNTIME_CONNECT_ATTEMPTS,
    retry_seconds: float = RUNTIME_CONNECT_RETRY_SECONDS,
) -> None:
    """Wait until the runtime role can open a simple connection."""

    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - dependency exists in runtime image
        raise RuntimeError("asyncpg is required for database readiness checks") from exc

    for attempt in range(1, attempts + 1):
        conn = None
        try:
            conn = await asyncpg.connect(connection_uri)
            await conn.execute("SELECT 1")
            return
        except Exception as exc:
            if attempt >= attempts or not _is_transient_admin_error(exc):
                raise
            log.info(
                "database runtime endpoint not ready; retrying readiness check",
                extra={
                    "binding_id": binding.id,
                    "attempt": attempt,
                    "error": redact_database_message(str(exc)),
                },
            )
            await asyncio.sleep(retry_seconds)
        finally:
            if conn is not None:
                await conn.close()
    raise RuntimeError("database runtime endpoint did not become ready")


def migration_sql_from_tarball(tarball: bytes, migrations_path: str) -> list[DatabaseMigration]:
    """Return ordered SQL migrations from a source tarball."""

    root = PurePosixPath(migrations_path.strip("/"))
    if not root.parts or any(part in {"", ".", ".."} for part in root.parts):
        raise RuntimeError("database migrations path must be a safe relative path")

    candidates: list[DatabaseMigration] = []
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or any(part in {"", ".", ".."} for part in member_path.parts):
                continue
            if not _migration_member_matches(member_path, root):
                continue
            if member_path.suffix.lower() != ".sql":
                continue
            if member.size > MAX_MIGRATION_FILE_BYTES:
                raise RuntimeError(f"migration file {member.name} exceeds size limit")
            total_bytes += member.size
            if total_bytes > MAX_MIGRATION_TOTAL_BYTES:
                raise RuntimeError("database migrations exceed total size limit")
            handle = archive.extractfile(member)
            if handle is None:
                continue
            sql = handle.read(MAX_MIGRATION_FILE_BYTES + 1)
            if len(sql) > MAX_MIGRATION_FILE_BYTES:
                raise RuntimeError(f"migration file {member.name} exceeds size limit")
            text = sql.decode("utf-8")
            # asyncpg's simple-query protocol raises an internal
            # ``NoneType.decode`` error for a payload containing comments but
            # no executable SQL. Scaffolds may intentionally retain an empty,
            # comment-only compatibility migration, so omit those files here
            # instead of sending them to Postgres.
            if _has_executable_sql(text):
                candidates.append(DatabaseMigration(path=member_path.as_posix(), sql=text))
            if len(candidates) > MAX_MIGRATION_FILES:
                raise RuntimeError("too many database migration files")
    return sorted(candidates, key=lambda item: item.path)


def _has_executable_sql(value: str) -> bool:
    without_block_comments = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    without_line_comments = "\n".join(
        line.split("--", 1)[0] for line in without_block_comments.splitlines()
    )
    return bool(without_line_comments.strip())


def _migration_member_matches(member_path: PurePosixPath, root: PurePosixPath) -> bool:
    if member_path == root:
        return True
    root_prefix = root.as_posix().rstrip("/") + "/"
    return member_path.as_posix().startswith(root_prefix)


@dataclass
class DatabaseProvisioner:
    session_maker: async_sessionmaker[AsyncSession]
    backend: DatabaseBackend
    upsert_secret: Callable[..., None] = upsert_agent_secret_value
    delete_secret: Callable[..., None] = delete_agent_secret_value
    migrate: Callable[..., Any] = run_database_migrations
    ready_check: Callable[..., Any] = wait_for_database_ready
    project_secret: Callable[..., dict[str, Any]] = ensure_agent_secret_projection
    worker_name: str = "database-provisioner"
    metrics: DatabaseProvisionerMetrics = field(default_factory=lambda: metrics)

    async def reconcile_once(self) -> DatabaseProvisionerMetrics:
        self.metrics.reconcile_runs += 1
        self.metrics.last_run_at = datetime.now(UTC)
        async with self.session_maker() as session:
            rows = (
                await session.execute(
                    select(AgentDatabaseBinding)
                    .where(AgentDatabaseBinding.status.in_([PENDING, REMOVED, FAILED]))
                    .order_by(AgentDatabaseBinding.updated_at, AgentDatabaseBinding.id)
                )
            ).scalars().all()
            work = [
                row
                for row in rows
                if row.status != REMOVED or not (row.metadata_json or {}).get("secret_revoked_at")
            ]
            self.metrics.queue_depth = len(work)
            for binding in work:
                if binding.status == REMOVED:
                    await self._remove_binding(session, binding)
                else:
                    await self._provision_binding(session, binding)
        return self.metrics

    async def _provision_binding(
        self,
        session: AsyncSession,
        binding: AgentDatabaseBinding,
    ) -> None:
        self.metrics.create_attempts += 1
        project = await session.get(DatabaseProject, binding.database_project_id)
        branch = (
            await session.get(DatabaseBranch, binding.database_branch_id)
            if binding.database_branch_id is not None
            else None
        )
        role = (
            await session.get(DatabaseRole, binding.database_role_id)
            if binding.database_role_id is not None
            else None
        )
        agent = await session.get(Agent, binding.agent_id)
        if project is None or branch is None or role is None or agent is None:
            await self._fail(
                session,
                binding,
                "database resource row is incomplete",
                {
                    "project": bool(project),
                    "branch": bool(branch),
                    "role": bool(role),
                    "agent": bool(agent),
                },
            )
            return

        # Rows created before identifier length enforcement may already carry
        # role or database names that PostgreSQL rejects (>63 bytes). Repair
        # those rows in place so a failed binding can reconcile without a
        # destructive delete/recreate operation.
        role.username = postgres_identifier(role.username)
        role.database_name = postgres_identifier(role.database_name)

        reusable_result = _reusable_provisioned_role(
            project=project,
            branch=branch,
            role=role,
        )
        binding.status = PROVISIONING
        role.status = PROVISIONING
        branch.status = PROVISIONING
        project.status = PROVISIONING
        session.add_all([binding, role, branch, project])
        await self._event(
            session,
            binding,
            event_type="database_binding_provisioning_started",
            status="started",
            message=f"provisioning database binding {binding.binding_name}",
            data={"binding_name": binding.binding_name, "env_var": binding.env_var},
        )
        await session.commit()

        try:
            result = reusable_result
            credentials_reused = result is not None
            if result is None:
                result = await self.backend.ensure_binding(
                    project=project,
                    branch=branch,
                    role=role,
                    binding=binding,
                )
            await self.ready_check(binding=binding, connection_uri=result.connection_uri)
            migration_path = str((binding.declaration_json or {}).get("migrations_path") or "").strip()
            applied_migrations: list[str] = []
            if migration_path:
                await self._event(
                    session,
                    binding,
                    event_type="database_binding_migrations_started",
                    status="started",
                    message=f"running database migrations for {binding.binding_name}",
                    data={"binding_name": binding.binding_name, "migrations_path": migration_path},
                )
                await session.commit()
                applied_migrations = await self.migrate(
                    agent=agent,
                    binding=binding,
                    connection_uri=result.connection_uri,
                )
                await self._event(
                    session,
                    binding,
                    event_type="database_binding_migrations_applied",
                    status="ok",
                    message=f"applied {len(applied_migrations)} database migration(s)",
                    data={
                        "binding_name": binding.binding_name,
                        "migrations_path": migration_path,
                        "migration_files": applied_migrations,
                    },
                )
            self.upsert_secret(
                agent_name=binding.agent_name,
                key=binding.env_var,
                value=result.connection_uri,
                owner_id=binding.user_id,
            )
            self.metrics.secret_writes += 1
            project.project_ref = result.project_ref
            branch.branch_ref = result.branch_ref
            role.username = result.username
            role.database_name = result.database_name
            role.password_ciphertext = encrypt_secret(result.password)
            role.connection_uri_ciphertext = encrypt_secret(result.connection_uri)
            project.status = READY
            branch.status = READY
            role.status = READY
            binding.status = READY
            binding.metadata_json = {
                **(binding.metadata_json or {}),
                "runtime_secret": role.secret_ref or (binding.metadata_json or {}).get("runtime_secret"),
                "secret_key": binding.env_var,
                "provisioned_at": datetime.now(UTC).isoformat(),
            }
            await self._event(
                session,
                binding,
                event_type="database_binding_ready",
                status="ok",
                message=f"database binding {binding.binding_name} is ready",
                data={
                    "binding_name": binding.binding_name,
                    "project_ref": project.project_ref,
                    "branch_ref": branch.branch_ref,
                    "role": role.name,
                    "runtime_secret": role.secret_ref,
                    "env_var": binding.env_var,
                },
            )
            session.add_all([project, branch, role, binding])
            await session.commit()
            self.metrics.ready_bindings += 1
            # Existing bindings keep the same role password across source
            # deploys. Rewriting the same Secret is enough; the new source
            # revision will read it when its pod starts. A newly provisioned
            # role does need a rollout when an older runtime already exists.
            if not credentials_reused:
                try:
                    projection = await asyncio.to_thread(
                        self.project_secret, binding.agent_name
                    )
                    log.info(
                        "rolled agent to pick up rotated database secret",
                        extra={
                            "binding_id": binding.id,
                            "agent": binding.agent_name,
                            "projection": projection,
                        },
                    )
                except Exception as roll_exc:  # noqa: BLE001
                    log.warning(
                        "failed to roll agent after database secret rotation",
                        extra={
                            "binding_id": binding.id,
                            "agent": binding.agent_name,
                            "error": redact_database_message(str(roll_exc)),
                        },
                    )
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            await self._fail(session, binding, str(exc), {"binding_name": binding.binding_name})

    async def _remove_binding(
        self,
        session: AsyncSession,
        binding: AgentDatabaseBinding,
    ) -> None:
        self.metrics.remove_attempts += 1
        try:
            self.delete_secret(agent_name=binding.agent_name, key=binding.env_var)
            self.metrics.secret_deletes += 1
            binding.metadata_json = {
                **(binding.metadata_json or {}),
                "secret_revoked_at": datetime.now(UTC).isoformat(),
            }
            await self._event(
                session,
                binding,
                event_type="database_binding_secret_revoked",
                status="ok",
                message=f"revoked database binding {binding.binding_name}",
                data={"binding_name": binding.binding_name, "env_var": binding.env_var},
            )
            session.add(binding)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            await self._fail(session, binding, str(exc), {"binding_name": binding.binding_name})

    async def _fail(
        self,
        session: AsyncSession,
        binding: AgentDatabaseBinding,
        message: str,
        data: dict[str, Any],
    ) -> None:
        self.metrics.failures += 1
        self.metrics.failed_bindings += 1
        self.metrics.last_error = message
        binding.status = FAILED
        if binding.database_role_id is not None:
            role = await session.get(DatabaseRole, binding.database_role_id)
            if role is not None:
                role.status = FAILED
                session.add(role)
        await self._event(
            session,
            binding,
            event_type="database_binding_failed",
            status="failed",
            message=message,
            data=data,
        )
        session.add(binding)
        await session.commit()

    async def _event(
        self,
        session: AsyncSession,
        binding: AgentDatabaseBinding,
        *,
        event_type: str,
        status: str,
        message: str,
        data: dict[str, Any],
    ) -> None:
        session.add(
            DatabaseProvisionEvent(
                database_project_id=binding.database_project_id,
                agent_id=binding.agent_id,
                event_type=event_type,
                status=status,
                message=redact_database_message(message),
                data=redact_database_event_data(data),
            )
        )


def _reusable_provisioned_role(
    *,
    project: DatabaseProject,
    branch: DatabaseBranch,
    role: DatabaseRole,
) -> ProvisionedDatabaseRole | None:
    """Return stable existing credentials for an unchanged database binding.

    Source deployments mark bindings pending so new migrations are applied, but
    rotating a healthy role password on every source commit creates avoidable
    downtime. New projects, branches, roles, or access modes have no stored
    credentials and continue through the backend provisioning path.
    """

    if project.status != READY or branch.status != READY:
        return None
    if not role.password_ciphertext or not role.connection_uri_ciphertext:
        return None
    return ProvisionedDatabaseRole(
        project_ref=project.project_ref or f"postgres-project-{project.id or project.name}",
        branch_ref=branch.branch_ref or f"postgres-branch-{branch.id or branch.name}",
        username=role.username,
        database_name=role.database_name,
        password=decrypt_secret(role.password_ciphertext),
        connection_uri=decrypt_secret(role.connection_uri_ciphertext),
    )


def redact_database_message(value: str) -> str:
    value = SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    redacted = redact_database_event_data(value)
    return str(redacted)


def redact_database_event_data(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SENSITIVE_KEYS):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact_database_event_data(item)
        return redacted
    if isinstance(value, list):
        return [redact_database_event_data(item) for item in value]
    if isinstance(value, str) and "://" in value and "@" in value:
        return "[redacted]"
    return value


def build_database_provisioner(
    *,
    session_maker: async_sessionmaker[AsyncSession] = SessionLocal,
) -> DatabaseProvisioner:
    if settings.database_backend == "neon_operator":
        # Per-agent operator tenant + hybrid compute + wake-proxy. Lazy import so
        # the (heavier, kubernetes-client-dependent) module only loads when
        # selected. Off by default (see config.database_backend).
        from .neon_operator_backend import NeonOperatorBackend

        operator_backend = NeonOperatorBackend(
            namespace=settings.database_operator_namespace,
            cluster_name=settings.database_operator_cluster,
            pageserver_url=settings.database_operator_pageserver_url
            or f"http://{settings.database_operator_cluster}-pageserver-0."
            f"{settings.database_operator_namespace}.svc:9898",
            storcon_url=settings.database_operator_storcon_url,
            safekeepers=settings.database_operator_safekeepers or "",
            compute_image=settings.database_operator_compute_image or "",
            wrapper_configmap=settings.database_operator_wrapper_configmap,
            wake_proxy_image=settings.database_operator_wake_proxy_image or "",
            wake_proxy_configmap=settings.database_operator_wake_proxy_configmap,
            wake_proxy_sa=settings.database_operator_wake_proxy_sa,
            admin_user=settings.database_operator_admin_user,
            admin_password=settings.database_operator_admin_password or "",
            admin_secret_name=settings.database_operator_admin_secret,
            pg_version=settings.database_pg_version,
            sslmode=settings.database_sslmode,
            idle_seconds=settings.database_operator_idle_seconds,
            compute_min_replicas=settings.database_operator_compute_min_replicas,
        )
        return DatabaseProvisioner(session_maker=session_maker, backend=operator_backend)
    if not settings.database_admin_url:
        raise RuntimeError("A2A_CP_DATABASE_ADMIN_URL is required when database provisioning is enabled")
    backend = PostgresAdminBackend(
        admin_url=settings.database_admin_url,
        pageserver_url=settings.database_neon_pageserver_url,
        pg_version=settings.database_pg_version,
        runtime_host=settings.database_runtime_host,
        runtime_port=settings.database_runtime_port,
        sslmode=settings.database_sslmode,
    )
    return DatabaseProvisioner(session_maker=session_maker, backend=backend)


async def run_database_provisioner_loop(
    *,
    interval_seconds: float | None = None,
    provisioner: DatabaseProvisioner | None = None,
) -> None:
    interval = (
        settings.database_provisioner_interval_seconds
        if interval_seconds is None
        else interval_seconds
    )
    provisioner = provisioner or build_database_provisioner()
    while True:
        try:
            await provisioner.reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            metrics.failures += 1
            metrics.last_error = str(exc)
            log.exception("database provisioner reconcile failed")
        await asyncio.sleep(interval)


def start_database_provisioner(app: Any) -> None:
    if not settings.database_provisioning_enabled:
        return
    if not settings.database_admin_url:
        metrics.last_error = (
            "A2A_CP_DATABASE_ADMIN_URL is required when database provisioning is enabled"
        )
        log.error(metrics.last_error)
        return
    task = asyncio.create_task(
        run_database_provisioner_loop(),
        name="database-provisioner",
    )
    app.state.database_provisioner = task


async def stop_database_provisioner(app: Any) -> None:
    task = getattr(app.state, "database_provisioner", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def database_metrics_snapshot() -> dict[str, Any]:
    return metrics.snapshot()


def database_provisioner_health() -> dict[str, Any]:
    enabled = settings.database_provisioning_enabled
    configured = bool(settings.database_admin_url)
    return {
        "enabled": enabled,
        "configured": configured,
        "ok": (not enabled) or configured,
        "metrics": database_metrics_snapshot(),
    }
