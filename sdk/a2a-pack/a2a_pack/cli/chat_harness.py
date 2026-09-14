from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ChatComposePlan:
    compose_path: Path
    project_name: str
    services: tuple[str, ...]
    environment: tuple[str, ...]
    resource_summary: tuple[str, ...]


def declared_local_services(agent_cls: Any) -> tuple[bool, tuple[Any, ...]]:
    """``(needs_vector, databases)`` declared by an agent class.

    Single source of truth for which backing containers a project implies:
    `a2a chat` starts them, `a2a dev --local` reports that it does not.
    """
    resources = getattr(agent_cls, "platform_resources", None)
    memory = getattr(resources, "memory", None)
    databases = tuple(getattr(resources, "databases", ()) or ())
    needs_vector = bool(
        memory is not None
        and "vector" in {str(tier).strip().lower() for tier in getattr(memory, "tiers", ())}
    )
    return needs_vector, databases


def container_backed_resources(agent_cls: Any) -> tuple[str, ...]:
    """Human-readable names of the declared resources that need containers.

    Both the in-process and the ``--docker`` dev runtimes run the agent alone;
    neither starts Qdrant or Postgres. Only the `a2a chat` compose harness
    does, so `a2a dev --local` uses this to say so instead of letting the
    agent fail against a missing backing service.
    """
    needs_vector, databases = declared_local_services(agent_cls)
    needs: list[str] = []
    if needs_vector:
        needs.append("vector memory (qdrant)")
    if databases:
        names = ", ".join(str(getattr(database, "name", "app")) for database in databases)
        needs.append(f"postgres databases: {names}")
    return tuple(needs)


def normalize_local_llm_env() -> None:
    """Map a plain OpenAI env file into the local ctx.llm contract."""

    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("AGENT_LLM_KEY"):
        os.environ["AGENT_LLM_KEY"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("AGENT_LLM_KEY"):
        os.environ.setdefault("AGENT_LLM_URL", "https://api.openai.com/v1")
        os.environ.setdefault("AGENT_LLM_MODEL", "gpt-4o")


def write_chat_compose(
    *,
    project: Path,
    local: Any,
    image: str,
    env_path: Path,
    workspace_root: Path,
    host: str,
    port: int,
    reload: bool,
    passthrough_env: list[str],
) -> ChatComposePlan:
    chat_dir = project / ".a2a" / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    init_sql = chat_dir / "init-postgres.sql"
    needs_vector, databases = declared_local_services(local.agent_cls)
    needs_postgres = bool(databases)

    environment = _agent_environment(
        local=local,
        env_path=env_path,
        project=project,
        databases=databases,
        needs_vector=needs_vector,
        passthrough_env=passthrough_env,
    )
    services: dict[str, Any] = {
        "agent": {
            "image": image,
            "working_dir": "/app",
            "ports": [f"{host}:{port}:8000"],
            "volumes": [
                f"{project.resolve()}:/app",
                f"{workspace_root.resolve()}:/workspace",
            ],
            "environment": environment,
            "command": _agent_command(reload=reload),
        }
    }
    if env_path.exists():
        services["agent"]["env_file"] = [str(env_path.resolve())]

    depends_on: dict[str, dict[str, str]] = {}
    resource_summary: list[str] = []
    if needs_vector:
        services["qdrant"] = _qdrant_service()
        depends_on["qdrant"] = {"condition": "service_started"}
        resource_summary.append("qdrant vector memory")
    if needs_postgres:
        init_sql.write_text(_postgres_init_sql(databases), encoding="utf-8")
        services["postgres"] = _postgres_service(init_sql)
        depends_on["postgres"] = {"condition": "service_healthy"}
        resource_summary.append(
            "postgres databases: "
            + ", ".join(getattr(database, "name", "app") for database in databases)
        )
    if depends_on:
        services["agent"]["depends_on"] = depends_on

    compose = {
        "name": _compose_project_name(local.agent_cls.name),
        "services": services,
    }
    volumes = _compose_volumes(needs_vector=needs_vector, needs_postgres=needs_postgres)
    if volumes:
        compose["volumes"] = volumes
    compose_path = chat_dir / "docker-compose.yml"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    return ChatComposePlan(
        compose_path=compose_path,
        project_name=compose["name"],
        services=tuple(services),
        environment=tuple(environment),
        resource_summary=tuple(resource_summary) or ("agent only",),
    )


def _agent_environment(
    *,
    local: Any,
    env_path: Path,
    project: Path,
    databases: tuple[Any, ...],
    needs_vector: bool,
    passthrough_env: list[str],
) -> list[str]:
    try:
        container_env_path = f"/app/{env_path.resolve().relative_to(project.resolve())}"
    except ValueError:
        container_env_path = "/app/.env.local"
    items = [
        "A2A_PROJECT_DIR=/app",
        f"A2A_ENTRYPOINT={local.entrypoint}",
        f"A2A_ENV_FILE={container_env_path}",
        "A2A_LOCAL_DEV=1",
        "A2A_LOCAL_DOCKER_CHAT=1",
        "A2A_LOCAL_WORKSPACE_DIR=/workspace",
    ]
    if needs_vector:
        items.extend(
            [
                "A2A_MEMORY_VECTOR_URL=http://qdrant:6333",
                "A2A_QDRANT_URL=http://qdrant:6333",
                "QDRANT_URL=http://qdrant:6333",
            ]
        )
    if databases:
        items.append("A2A_DATABASE_PROVIDER=postgres")
        for database in databases:
            env_name = getattr(getattr(database, "env", None), "url", "DATABASE_URL")
            db_name = getattr(database, "name", "app")
            items.append(
                f"{env_name}=postgresql://a2a:a2a@postgres:5432/{db_name}"
            )
    for name in passthrough_env:
        if name not in {item.split("=", 1)[0] for item in items}:
            items.append(name)
    return items


def _agent_command(*, reload: bool) -> list[str]:
    cmd = [
        "python",
        "-m",
        "uvicorn",
        "a2a_pack.cli.dev_server:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    if reload:
        cmd.extend(["--reload", "--reload-dir", "/app"])
    return cmd


def _qdrant_service() -> dict[str, Any]:
    return {
        "image": "qdrant/qdrant:latest",
        "volumes": ["qdrant-data:/qdrant/storage"],
    }


def _postgres_service(init_sql: Path) -> dict[str, Any]:
    return {
        "image": "postgres:16-alpine",
        "environment": [
            "POSTGRES_USER=a2a",
            "POSTGRES_PASSWORD=a2a",
            "POSTGRES_DB=a2a",
        ],
        "volumes": [
            "postgres-data:/var/lib/postgresql/data",
            f"{init_sql.resolve()}:/docker-entrypoint-initdb.d/10-a2a-databases.sql:ro",
        ],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U a2a -d a2a"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 30,
        },
    }


def _postgres_init_sql(databases: tuple[Any, ...]) -> str:
    names = ["a2a", *[str(getattr(database, "name", "app")) for database in databases]]
    lines = ["-- Generated by a2a chat. Safe to delete with .a2a/chat.\n"]
    for name in dict.fromkeys(names):
        lines.append(
            "SELECT 'CREATE DATABASE "
            + _quote_postgres_ident(name)
            + "' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = "
            + _quote_postgres_literal(name)
            + ")\\gexec\n"
        )
    return "".join(lines)


def _quote_postgres_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _quote_postgres_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _compose_project_name(name: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
    return f"a2a-chat-{safe}"[:63].strip("-")


def _compose_volumes(*, needs_vector: bool, needs_postgres: bool) -> dict[str, Any]:
    volumes: dict[str, Any] = {}
    if needs_vector:
        volumes["qdrant-data"] = {}
    if needs_postgres:
        volumes["postgres-data"] = {}
    return volumes
