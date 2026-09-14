from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
import re
import tarfile
from typing import Any

import yaml


_DB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_SUPPORTED_ENGINES = {"postgres"}
_SUPPORTED_PROVIDERS = {"neon"}
_SUPPORTED_SCOPES = {"user", "org"}
_SUPPORTED_ACCESS_MODES = {"read_only", "read_write", "owner"}


@dataclass(frozen=True)
class DatabaseEnvDeclaration:
    url: str


@dataclass(frozen=True)
class AgentDatabaseDeclaration:
    name: str
    provider: str
    engine: str
    scope: str
    branch: str
    access_mode: str
    env: DatabaseEnvDeclaration
    migrations_path: str | None
    scale_to_zero: bool
    raw: dict[str, Any]


def read_agent_database_declarations(source: Path) -> list[AgentDatabaseDeclaration]:
    """Read platform-managed database declarations from ``a2a.yaml``.

    The public contract is top-level ``resources.databases``. Keeping this out
    of ``runtime.resources`` avoids mixing platform resources with container
    CPU/memory sizing.
    """

    yaml_path = source / "a2a.yaml"
    if not yaml_path.exists():
        return []
    data = _load_manifest_text(yaml_path.read_text())
    return _read_database_declarations_from_manifest(data)


def read_agent_database_declarations_from_tarball(
    tarball_path: str | Path,
) -> list[AgentDatabaseDeclaration]:
    """Read database declarations from an uploaded source tarball."""

    with tarfile.open(tarball_path) as archive:
        return _read_database_declarations_from_archive(archive)


def read_agent_database_declarations_from_tarball_bytes(
    tarball: bytes,
) -> list[AgentDatabaseDeclaration]:
    """Read declarations from an in-memory managed-source export."""

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:*") as archive:
        return _read_database_declarations_from_archive(archive)


def _read_database_declarations_from_archive(
    archive: tarfile.TarFile,
) -> list[AgentDatabaseDeclaration]:
    members = [
        member
        for member in archive.getmembers()
        if not member.isdir() and Path(member.name).name == "a2a.yaml"
    ]
    if not members:
        return []
    member = min(members, key=lambda item: len(Path(item.name).parts))
    handle = archive.extractfile(member)
    if handle is None:
        return []
    text = handle.read(512 * 1024 + 1)
    if len(text) > 512 * 1024:
        raise ValueError("a2a.yaml is too large")
    data = _load_manifest_text(text.decode("utf-8"))
    return _read_database_declarations_from_manifest(data)


def _load_manifest_text(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid a2a.yaml: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return data


def _read_database_declarations_from_manifest(
    data: dict[str, Any],
) -> list[AgentDatabaseDeclaration]:
    resources = data.get("resources")
    if not isinstance(resources, dict):
        return []
    databases = resources.get("databases")
    if databases is None:
        return []
    if not isinstance(databases, list):
        raise ValueError("resources.databases must be a list")
    return [_normalize_database_declaration(item, index) for index, item in enumerate(databases)]


def _normalize_database_declaration(raw: object, index: int) -> AgentDatabaseDeclaration:
    if not isinstance(raw, dict):
        raise ValueError(f"resources.databases[{index}] must be an object")
    name = _required_slug(raw, "name", index)
    provider = str(raw.get("provider") or "neon").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            f"resources.databases[{index}].provider must be one of "
            f"{sorted(_SUPPORTED_PROVIDERS)}"
        )
    engine = str(raw.get("engine") or "postgres").strip().lower()
    if engine not in _SUPPORTED_ENGINES:
        raise ValueError(
            f"resources.databases[{index}].engine must be one of "
            f"{sorted(_SUPPORTED_ENGINES)}"
        )
    scope = str(raw.get("scope") or "user").strip().lower()
    if scope not in _SUPPORTED_SCOPES:
        raise ValueError(
            f"resources.databases[{index}].scope must be one of "
            f"{sorted(_SUPPORTED_SCOPES)}"
        )
    branch = str(raw.get("branch") or "main").strip().lower()
    if not _DB_NAME_RE.match(branch):
        raise ValueError(f"resources.databases[{index}].branch must be a slug")
    access_mode = str(raw.get("access_mode") or raw.get("role") or "read_write").strip().lower()
    if access_mode not in _SUPPORTED_ACCESS_MODES:
        raise ValueError(
            f"resources.databases[{index}].access_mode must be one of "
            f"{sorted(_SUPPORTED_ACCESS_MODES)}"
        )
    env = raw.get("env")
    env_url = "DATABASE_URL"
    if isinstance(env, dict) and env.get("url") is not None:
        env_url = str(env.get("url") or "").strip()
    if not _ENV_NAME_RE.match(env_url):
        raise ValueError(f"resources.databases[{index}].env.url must be an environment variable name")
    migrations = raw.get("migrations")
    migrations_path = None
    if isinstance(migrations, dict) and migrations.get("path") is not None:
        migrations_path = _safe_relative_path(str(migrations.get("path") or ""), index)
    elif raw.get("migrations_path") is not None:
        migrations_path = _safe_relative_path(str(raw.get("migrations_path") or ""), index)
    return AgentDatabaseDeclaration(
        name=name,
        provider=provider,
        engine=engine,
        scope=scope,
        branch=branch,
        access_mode=access_mode,
        env=DatabaseEnvDeclaration(url=env_url),
        migrations_path=migrations_path,
        scale_to_zero=bool(raw.get("scale_to_zero", True)),
        raw=dict(raw),
    )


def _required_slug(raw: dict[str, Any], key: str, index: int) -> str:
    value = str(raw.get(key) or "").strip().lower()
    if not _DB_NAME_RE.match(value):
        raise ValueError(f"resources.databases[{index}].{key} must be a slug")
    return value


def _safe_relative_path(value: str, index: int) -> str:
    path = value.strip()
    if not path or path.startswith("/") or ".." in Path(path).parts:
        raise ValueError(
            f"resources.databases[{index}].migrations.path must be a safe relative path"
        )
    return path
