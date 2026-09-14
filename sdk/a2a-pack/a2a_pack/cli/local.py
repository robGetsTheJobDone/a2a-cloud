from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..agent import A2AAgent
from ..runtime import LLMProvisioning, apply_project_manifest
from ..workspace import (
    FileSystemWorkspaceClient,
    WorkspaceAccess,
    WorkspaceMode,
)
from .loader import load_agent_class

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class LocalProject:
    project: Path
    config: dict[str, Any]
    entrypoint: str
    agent_cls: type[A2AAgent]


@dataclass(frozen=True)
class LocalCheck:
    name: str
    ok: bool
    message: str


def load_env_file(path: Path, *, override: bool = False) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected KEY=value")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"{path}:{lineno}: invalid environment key {key!r}")
        value = _parse_env_value(value.strip())
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def load_local_project(project: Path) -> LocalProject:
    cfg_path = project / "a2a.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"missing {cfg_path} (run `a2a init NAME` first)")
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    entrypoint = cfg.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise ValueError("a2a.yaml is missing entrypoint")
    cls = load_agent_class(entrypoint.strip(), project_dir=project)
    apply_project_manifest(cls, cfg)
    return LocalProject(
        project=project,
        config=cfg,
        entrypoint=entrypoint.strip(),
        agent_cls=cls,
    )


def ensure_local_workspace(
    project: Path,
    *,
    workspace: Path | None = None,
) -> Path:
    root = (workspace or project / ".a2a" / "workspace").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "outputs").mkdir(parents=True, exist_ok=True)
    return root


def build_local_workspace(
    agent_cls: type[A2AAgent],
    root: Path,
) -> FileSystemWorkspaceClient | None:
    access = agent_cls.workspace_access
    if not access.enabled:
        return None
    local_access = WorkspaceAccess(
        enabled=True,
        max_files=access.max_files or 64,
        allowed_modes=access.allowed_modes
        or (WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
        require_reason=access.require_reason,
        deny_patterns=access.deny_patterns,
        require_human_approval=False,
        max_total_size_bytes=access.max_total_size_bytes,
    )
    return FileSystemWorkspaceClient(
        root,
        access=local_access,
        bucket="local-dev",
        issuer=agent_cls.name,
        allow_patterns=("**",),
        outputs_prefix="outputs",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )


async def run_local_preflight(
    project: Path,
    *,
    env_file: Path,
    workspace: Path | None = None,
    invoke: bool = False,
    skill_name: str | None = None,
    args_json: str | None = None,
) -> tuple[list[LocalCheck], dict[str, Any] | None]:
    loaded_env = load_env_file(env_file)
    local = load_local_project(project)
    agent = local.agent_cls()
    card = agent.card()
    checks: list[LocalCheck] = [
        LocalCheck("project", True, f"loaded {local.entrypoint}"),
        LocalCheck("card", True, f"{card.name} v{card.version}; {len(card.skills)} skills"),
    ]

    required = [*card.required_env, *card.required_secrets]
    missing = [key for key in required if not os.environ.get(key)]
    checks.append(
        LocalCheck(
            "env",
            not missing,
            "all required env/secrets present"
            if not missing
            else f"missing {', '.join(missing)}",
        )
    )

    if local.agent_cls.llm_provisioning in {
        LLMProvisioning.CALLER_PROVIDED,
        LLMProvisioning.PLATFORM,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED,
    }:
        has_llm = bool(os.environ.get("A2A_LITELLM_KEY") or os.environ.get("AGENT_LLM_KEY"))
        checks.append(
            LocalCheck(
                "llm",
                True,
                "local LLM env configured"
                if has_llm
                else (
                    "local LLM env not configured; skills that read ctx.llm "
                    "need A2A_LITELLM_KEY or AGENT_LLM_KEY"
                ),
            )
        )

    workspace_root = ensure_local_workspace(project, workspace=workspace)
    local_workspace = build_local_workspace(local.agent_cls, workspace_root)
    checks.append(
        LocalCheck(
            "workspace",
            True,
            f"{workspace_root}" if local_workspace else "agent does not request workspace access",
        )
    )

    result: dict[str, Any] | None = None
    if invoke:
        skill = _select_skill(card.model_dump(mode="json"), skill_name)
        args = _args_for_skill(skill, args_json)
        try:
            value = await agent.local_invoke(
                skill["name"],
                secrets={key: os.environ[key] for key in required if key in os.environ},
                workspace=local_workspace,
                **args,
            )
            result = {"skill": skill["name"], "result": value}
            checks.append(LocalCheck("invoke", True, f"{skill['name']} returned ok"))
        except Exception as exc:  # noqa: BLE001
            checks.append(LocalCheck("invoke", False, f"{type(exc).__name__}: {exc}"))
    return checks, result


def run_preflight_sync(*args: Any, **kwargs: Any) -> tuple[list[LocalCheck], dict[str, Any] | None]:
    return asyncio.run(run_local_preflight(*args, **kwargs))


def sample_args_from_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    value = _sample_value(schema)
    return value if isinstance(value, dict) else {}


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def _select_skill(card: dict[str, Any], requested: str | None) -> dict[str, Any]:
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ValueError("agent has no skills")
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        if requested is None or skill.get("name") == requested:
            if isinstance(skill.get("name"), str):
                return skill
    # Naming the valid skills is the whole difference between a dead end and a
    # working next command.
    available = [
        skill["name"]
        for skill in skills
        if isinstance(skill, dict) and isinstance(skill.get("name"), str)
    ]
    suffix = f"; available skills: {', '.join(available)}" if available else ""
    raise ValueError(f"unknown skill: {requested}{suffix}")


def _args_for_skill(skill: dict[str, Any], args_json: str | None) -> dict[str, Any]:
    if args_json:
        parsed = json.loads(args_json)
        if not isinstance(parsed, dict):
            raise ValueError("--args-json must decode to an object")
        return parsed
    schema = skill.get("input_schema")
    return sample_args_from_schema(schema if isinstance(schema, dict) else {})


def _sample_value(schema: dict[str, Any]) -> Any:
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((item for item in typ if item != "null"), typ[0] if typ else None)
    props = schema.get("properties")
    if typ == "object" or isinstance(props, dict):
        properties = props if isinstance(props, dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        keys = required or list(properties.keys())
        out: dict[str, Any] = {}
        for key in keys:
            child = properties.get(key)
            if isinstance(child, dict):
                out[key] = _sample_value(child)
        return out
    if typ == "array":
        return []
    if typ == "integer":
        return 1
    if typ == "number":
        return 1.0
    if typ == "boolean":
        return True
    if typ == "string":
        fmt = schema.get("format")
        if fmt == "uri":
            return "https://example.com"
        if fmt == "email":
            return "user@example.com"
        return "sample"
    return None
