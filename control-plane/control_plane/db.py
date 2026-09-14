from __future__ import annotations

import re
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE(?:\s+IF\s+EXISTS)?\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+"
    r"ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


async def _missing_columns(
    conn: AsyncSession,
    table_name: str,
    columns: set[str],
) -> set[str]:
    if not columns:
        return set()
    rows = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table_name "
            "AND column_name = ANY(CAST(:columns AS TEXT[]))"
        ),
        {"table_name": table_name, "columns": sorted(columns)},
    )
    existing = {str(row[0]) for row in rows}
    return set(columns) - existing


async def _execute_add_column_alters_if_missing(
    conn: AsyncSession,
    statements: tuple[str, ...],
) -> None:
    by_table: dict[str, set[str]] = {}
    parsed: list[tuple[str, str, str] | tuple[None, None, str]] = []
    for statement in statements:
        match = _ADD_COLUMN_RE.match(statement)
        if match is None:
            parsed.append((None, None, statement))
            continue
        table_name, column_name = match.groups()
        table_name = table_name.lower()
        column_name = column_name.lower()
        by_table.setdefault(table_name, set()).add(column_name)
        parsed.append((table_name, column_name, statement))

    missing_by_table = {
        table_name: await _missing_columns(conn, table_name, columns)
        for table_name, columns in by_table.items()
    }
    for table_name, column_name, statement in parsed:
        if table_name is None or column_name is None:
            await conn.execute(text(statement))
            continue
        missing = missing_by_table.get(table_name, set())
        if column_name not in missing:
            continue
        await conn.execute(text(statement))
        missing.discard(column_name)


async def init_models() -> None:
    """Create tables if they don't exist (MVP only; alembic comes next)."""
    from . import models  # noqa: F401

    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(4201042001)"))
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await conn.execute(
                text(
                    "ALTER TABLE organization_members "
                    "ADD COLUMN IF NOT EXISTS external_id VARCHAR(255)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE organization_members "
                    "ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE organization_members "
                    "ADD COLUMN IF NOT EXISTS updated_at "
                    "TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE agents "
                    "ADD COLUMN IF NOT EXISTS organization_id INTEGER"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE agents "
                    "ADD COLUMN IF NOT EXISTS gitea_owner VARCHAR(96)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE agents "
                    "ADD COLUMN IF NOT EXISTS source_agent_id INTEGER "
                    "REFERENCES agents(id) ON DELETE SET NULL"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agents_source_agent_id "
                    "ON agents (source_agent_id)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE users "
                    "ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE chat_threads "
                    "ADD COLUMN IF NOT EXISTS settings_json JSONB NOT NULL DEFAULT '{}'::jsonb"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_users_is_admin "
                    "ON users (is_admin)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE IF EXISTS agent_custom_domains "
                    "ADD COLUMN IF NOT EXISTS canonical_hostname VARCHAR(255)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE IF EXISTS agent_custom_domains "
                    "ADD COLUMN IF NOT EXISTS redirect_enabled BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_custom_domains_redirect_enabled "
                    "ON agent_custom_domains (redirect_enabled)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS feature_flags ("
                    "key VARCHAR(96) PRIMARY KEY, "
                    "label VARCHAR(160) NOT NULL, "
                    "description TEXT, "
                    "default_enabled BOOLEAN NOT NULL DEFAULT FALSE, "
                    "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(), "
                    "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(), "
                    "deleted_at TIMESTAMP WITH TIME ZONE"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_feature_flags_default_enabled "
                    "ON feature_flags (default_enabled)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_feature_flags_deleted_at "
                    "ON feature_flags (deleted_at)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS user_feature_flags ("
                    "id SERIAL PRIMARY KEY, "
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "flag_key VARCHAR(96) NOT NULL REFERENCES feature_flags(key) ON DELETE CASCADE, "
                    "enabled BOOLEAN NOT NULL DEFAULT TRUE, "
                    "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(), "
                    "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(), "
                    "CONSTRAINT uq_user_feature_flags_user_flag UNIQUE (user_id, flag_key)"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_feature_flags_user_id "
                    "ON user_feature_flags (user_id)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_feature_flags_flag_key "
                    "ON user_feature_flags (flag_key)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_feature_flags_flag_enabled "
                    "ON user_feature_flags (flag_key, enabled)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS user_onboarding_states ("
                    "id SERIAL PRIMARY KEY, "
                    "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                    "current_step VARCHAR(32) NOT NULL DEFAULT 'llm_key', "
                    "llm_key_step_completed BOOLEAN NOT NULL DEFAULT FALSE, "
                    "walkthrough_completed BOOLEAN NOT NULL DEFAULT FALSE, "
                    "llm_key_completed_at TIMESTAMP WITH TIME ZONE, "
                    "walkthrough_started_at TIMESTAMP WITH TIME ZONE, "
                    "walkthrough_completed_at TIMESTAMP WITH TIME ZONE, "
                    "last_seen_step VARCHAR(128), "
                    "tour_step_index INTEGER NOT NULL DEFAULT 0, "
                    "tour_step_total INTEGER NOT NULL DEFAULT 0, "
                    "dismissed_at TIMESTAMP WITH TIME ZONE, "
                    "completed_at TIMESTAMP WITH TIME ZONE, "
                    "created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(), "
                    "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(), "
                    "CONSTRAINT uq_user_onboarding_states_user UNIQUE (user_id)"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_onboarding_states_user_id "
                    "ON user_onboarding_states (user_id)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_user_onboarding_states_completed_at "
                    "ON user_onboarding_states (completed_at)"
                )
            )
            onboarding_alters = (
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS llm_key_completed_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS walkthrough_started_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS walkthrough_completed_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS last_seen_step VARCHAR(128)",
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS tour_step_index INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS tour_step_total INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE IF EXISTS user_onboarding_states "
                "ADD COLUMN IF NOT EXISTS dismissed_at TIMESTAMP WITH TIME ZONE",
            )
            for statement in onboarding_alters:
                await conn.execute(text(statement))
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_organization_members_external_id "
                    "ON organization_members (external_id)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agents_organization_id "
                    "ON agents (organization_id)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_agents_gitea_owner "
                    "ON agents (gitea_owner)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_organization_members_active "
                    "ON organization_members (active)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE user_llm_creds "
                    "ADD COLUMN IF NOT EXISTS temperature_mode "
                    "VARCHAR(16) NOT NULL DEFAULT 'omit'"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE user_llm_creds "
                    "ADD COLUMN IF NOT EXISTS temperature DOUBLE PRECISION"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE user_llm_creds "
                    "ADD COLUMN IF NOT EXISTS extra_body JSON NOT NULL DEFAULT '{}'::json"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE IF EXISTS agent_auth_connections "
                    "DROP CONSTRAINT IF EXISTS uq_agent_auth_agent_scheme"
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "uq_agent_auth_agent_user_scheme "
                    "ON agent_auth_connections (agent_id, user_id, scheme_name)"
                )
            )
            work_ledger_alters = (
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS job_id VARCHAR(64) NOT NULL DEFAULT ''",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS kind VARCHAR(64) NOT NULL DEFAULT 'generic'",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'queued'",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS title VARCHAR(255)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS summary TEXT",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS error TEXT",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS queue VARCHAR(64) NOT NULL DEFAULT 'default'",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 1",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS user_id INTEGER",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS thread_id VARCHAR(36)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS root_job_id VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS parent_job_id VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(96)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS source_type VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS source_id VARCHAR(160)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS subject_type VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS subject_id VARCHAR(160)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS worker_type VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_jobs ADD COLUMN IF NOT EXISTS worker_name VARCHAR(160)",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS input_payload JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS output_payload JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS error_payload JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS artifact_refs JSON NOT NULL DEFAULT '[]'::json",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS proof_refs JSON NOT NULL DEFAULT '[]'::json",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS metadata_json JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS queued_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS leased_until TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS work_jobs "
                "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS event_id VARCHAR(64) NOT NULL DEFAULT ''",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS job_id VARCHAR(64) NOT NULL DEFAULT ''",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS event_seq INTEGER",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS parent_event_id VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(96)",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS event_type VARCHAR(64) NOT NULL DEFAULT 'event'",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS stage VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS status VARCHAR(32)",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS severity VARCHAR(16) NOT NULL DEFAULT 'info'",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS message TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS actor_type VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS actor_id VARCHAR(160)",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS source_type VARCHAR(64)",
                "ALTER TABLE IF EXISTS work_events ADD COLUMN IF NOT EXISTS source_id VARCHAR(160)",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS payload JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS artifact_refs JSON NOT NULL DEFAULT '[]'::json",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS proof_refs JSON NOT NULL DEFAULT '[]'::json",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS metrics JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS metadata_json JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS work_events "
                "ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS scope VARCHAR(128) NOT NULL DEFAULT 'global'",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(255) NOT NULL DEFAULT ''",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS request_hash VARCHAR(96) NOT NULL DEFAULT ''",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'in_progress'",
                "ALTER TABLE IF EXISTS idempotency_records ADD COLUMN IF NOT EXISTS user_id INTEGER",
                "ALTER TABLE IF EXISTS idempotency_records ADD COLUMN IF NOT EXISTS thread_id VARCHAR(36)",
                "ALTER TABLE IF EXISTS idempotency_records ADD COLUMN IF NOT EXISTS job_id VARCHAR(64)",
                "ALTER TABLE IF EXISTS idempotency_records ADD COLUMN IF NOT EXISTS result_type VARCHAR(64)",
                "ALTER TABLE IF EXISTS idempotency_records ADD COLUMN IF NOT EXISTS result_id VARCHAR(160)",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS response_status_code INTEGER",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS response_body JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS idempotency_records ADD COLUMN IF NOT EXISTS error TEXT",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS metadata_json JSON NOT NULL DEFAULT '{}'::json",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE",
                "ALTER TABLE IF EXISTS idempotency_records "
                "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE",
            )
            await _execute_add_column_alters_if_missing(conn, work_ledger_alters)

            work_ledger_indexes = (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_jobs_job_id "
                "ON work_jobs (job_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_kind_status_created_at "
                "ON work_jobs (kind, status, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_queue_status_priority "
                "ON work_jobs (queue, status, priority)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_thread_created_at "
                "ON work_jobs (thread_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_parent_created_at "
                "ON work_jobs (parent_job_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_subject "
                "ON work_jobs (subject_type, subject_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_job_id ON work_jobs (job_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_kind ON work_jobs (kind)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_status ON work_jobs (status)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_queue ON work_jobs (queue)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_user_id ON work_jobs (user_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_thread_id ON work_jobs (thread_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_root_job_id ON work_jobs (root_job_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_parent_job_id ON work_jobs (parent_job_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_correlation_id ON work_jobs (correlation_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_idempotency_key ON work_jobs (idempotency_key)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_worker_name ON work_jobs (worker_name)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_created_at ON work_jobs (created_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_heartbeat_at ON work_jobs (heartbeat_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_jobs_leased_until ON work_jobs (leased_until)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_events_event_id "
                "ON work_events (event_id)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_work_events_job_seq "
                "ON work_events (job_id, event_seq)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_job_created_at "
                "ON work_events (job_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_type_created_at "
                "ON work_events (event_type, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_source "
                "ON work_events (source_type, source_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_event_id ON work_events (event_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_job_id ON work_events (job_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_parent_event_id "
                "ON work_events (parent_event_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_correlation_id "
                "ON work_events (correlation_id)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_event_type ON work_events (event_type)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_stage ON work_events (stage)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_status ON work_events (status)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_severity ON work_events (severity)",
                "CREATE INDEX IF NOT EXISTS ix_work_events_created_at ON work_events (created_at)",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_idempotency_scope_key "
                "ON idempotency_records (scope, idempotency_key)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_status_expires_at "
                "ON idempotency_records (status, expires_at)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_user_created_at "
                "ON idempotency_records (user_id, created_at)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_scope "
                "ON idempotency_records (scope)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_idempotency_key "
                "ON idempotency_records (idempotency_key)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_request_hash "
                "ON idempotency_records (request_hash)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_status "
                "ON idempotency_records (status)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_user_id "
                "ON idempotency_records (user_id)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_thread_id "
                "ON idempotency_records (thread_id)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_job_id "
                "ON idempotency_records (job_id)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_created_at "
                "ON idempotency_records (created_at)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_locked_until "
                "ON idempotency_records (locked_until)",
                "CREATE INDEX IF NOT EXISTS ix_idempotency_records_expires_at "
                "ON idempotency_records (expires_at)",
            )
            for statement in work_ledger_indexes:
                await conn.execute(text(statement))

            langfuse_workspace_alters = (
                "ALTER TABLE IF EXISTS organization_langfuse_workspaces "
                "ADD COLUMN IF NOT EXISTS secret_key_ciphertext TEXT",
                "ALTER TABLE IF EXISTS organization_langfuse_workspaces "
                "ADD COLUMN IF NOT EXISTS litellm_team_id VARCHAR(160)",
                "ALTER TABLE IF EXISTS organization_langfuse_workspaces "
                "ADD COLUMN IF NOT EXISTS litellm_key_ref VARCHAR(255)",
                "ALTER TABLE IF EXISTS organization_langfuse_workspaces "
                "ADD COLUMN IF NOT EXISTS litellm_key_ciphertext TEXT",
            )
            for statement in langfuse_workspace_alters:
                await conn.execute(text(statement))

            langfuse_account_alters = (
                "ALTER TABLE IF EXISTS user_langfuse_accounts "
                "ADD COLUMN IF NOT EXISTS login_password_ref VARCHAR(255)",
                "ALTER TABLE IF EXISTS user_langfuse_accounts "
                "ADD COLUMN IF NOT EXISTS login_password_ciphertext TEXT",
            )
            for statement in langfuse_account_alters:
                await conn.execute(text(statement))



            studio_alters = (
                "ALTER TABLE IF EXISTS agents "
                "ADD COLUMN IF NOT EXISTS fork_policy VARCHAR(32) NOT NULL DEFAULT 'organization'",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS organization_id INTEGER",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS action VARCHAR(32) NOT NULL DEFAULT 'build_new'",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS source_agent_id INTEGER",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS source_version VARCHAR(64)",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS source_sha VARCHAR(64)",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS source_card_hash VARCHAR(64)",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS plan_id VARCHAR(64)",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128)",
                "ALTER TABLE IF EXISTS agent_studio_runs "
                "ADD COLUMN IF NOT EXISTS authorization_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb",
            )
            await _execute_add_column_alters_if_missing(conn, studio_alters)
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_agents_fork_policy "
                "ON agents (fork_policy)"
            ))
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_studio_runs_user_idempotency "
                "ON agent_studio_runs (user_id, idempotency_key) "
                "WHERE user_id IS NOT NULL AND idempotency_key IS NOT NULL"
            ))
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_agent_studio_runs_plan_id "
                "ON agent_studio_runs (plan_id)"
            ))
