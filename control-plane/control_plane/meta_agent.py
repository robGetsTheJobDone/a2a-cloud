from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import yaml
from a2a_pack import MetaAgentManifest

from .openapi_agent import _clean_version, _slug_to_class, _slugify_agent_name


class MetaAgentGenerationError(ValueError):
    """Raised when a composition manifest cannot be converted to source."""


@dataclass(frozen=True)
class GeneratedMetaAgent:
    name: str
    class_name: str
    description: str
    version: str
    files: dict[str, str]
    preview: dict[str, Any]


def build_meta_agent_source(
    manifest: MetaAgentManifest | Mapping[str, Any],
    *,
    name: str | None = None,
    description: str | None = None,
    version: str | None = None,
) -> GeneratedMetaAgent:
    parsed = _coerce_manifest(manifest)
    composition = parsed.composition
    if composition is None or not composition.sub_agents:
        raise MetaAgentGenerationError("meta-agent manifest requires composition.sub_agents")

    goal_text = parsed.goal.objective if parsed.goal else ""
    agent_name = _slugify_agent_name(name or _name_from_goal(goal_text))
    class_name = _slug_to_class(agent_name)
    clean_version = _clean_version(version or "0.1.0")
    agent_description = (
        str(description or "").strip()
        or _description_from_manifest(parsed)
        or "Generated meta-agent composed from existing A2A agents."
    )
    manifest_json = json.dumps(
        parsed.model_dump(mode="json", exclude_none=True),
        indent=2,
        sort_keys=True,
    )
    files = {
        "README.md": _readme(
            name=agent_name,
            description=agent_description,
            manifest=parsed,
        ),
        "agent.py": _agent_py(
            class_name=class_name,
            agent_name=agent_name,
            description=agent_description,
            version=clean_version,
            manifest_json=manifest_json,
        ),
        "a2a.yaml": _a2a_yaml(
            name=agent_name,
            class_name=class_name,
            description=agent_description,
            version=clean_version,
            manifest=parsed,
        ),
        "requirements.txt": _requirements_txt(),
        "meta_agent_manifest.json": manifest_json + "\n",
    }
    preview = {
        "name": agent_name,
        "class_name": class_name,
        "description": agent_description,
        "version": clean_version,
        "skills": ["pursue"],
        "composition": parsed.public_payload().get("composition", {}),
        "goal": parsed.public_payload().get("goal", {}),
        "memory": parsed.public_payload().get("memory", {}),
        "sub_agents": [
            item.public_payload()
            for item in (composition.sub_agents if composition is not None else ())
        ],
        "source_files": sorted(files),
        "warnings": _warnings(parsed),
    }
    return GeneratedMetaAgent(
        name=agent_name,
        class_name=class_name,
        description=agent_description,
        version=clean_version,
        files=files,
        preview=preview,
    )


def _coerce_manifest(manifest: MetaAgentManifest | Mapping[str, Any]) -> MetaAgentManifest:
    if isinstance(manifest, MetaAgentManifest):
        parsed = manifest
    elif isinstance(manifest, Mapping):
        parsed = MetaAgentManifest.from_mapping(manifest)
    else:
        raise MetaAgentGenerationError("manifest must be an object")
    if not parsed.enabled:
        raise MetaAgentGenerationError("meta-agent manifest is empty")
    return parsed


def _name_from_goal(goal: str) -> str:
    words = [part for part in goal.lower().split() if part.strip()]
    if not words:
        return "meta-agent"
    return "-".join(words[:6])


def _description_from_manifest(manifest: MetaAgentManifest) -> str:
    goal = manifest.goal.objective if manifest.goal else ""
    if goal:
        return f"Meta-agent that pursues: {goal}"
    count = len(manifest.composition.sub_agents) if manifest.composition else 0
    return f"Meta-agent composed from {count} existing A2A agent(s)."


def _readme(
    *,
    name: str,
    description: str,
    manifest: MetaAgentManifest,
) -> str:
    sub_agents = manifest.composition.sub_agents if manifest.composition else ()
    lines = [
        f"# {name}",
        "",
        description,
        "",
        "Generated A2APack meta-agent project.",
        "",
        "- Main skill: `pursue`",
        f"- Planning: `{manifest.composition.planning if manifest.composition else 'llm_dag'}`",
        f"- Max nodes: `{manifest.composition.max_nodes if manifest.composition else 8}`",
        f"- Max replans: `{manifest.composition.max_replans if manifest.composition else 1}`",
        "- Declared sub-agents:",
    ]
    for item in sub_agents:
        label = item.name or f"tag:{item.tag}"
        skills = ", ".join(item.skills) if item.skills else "all advertised skills"
        lines.append(f"  - `{label}` skills: {skills}")
    lines.extend(
        [
            "",
            "Edit `meta_agent_manifest.json` and `a2a.yaml` together if you change the composition.",
            "",
        ]
    )
    return "\n".join(lines)


def _agent_py(
    *,
    class_name: str,
    agent_name: str,
    description: str,
    version: str,
    manifest_json: str,
) -> str:
    return f'''from __future__ import annotations

import json

from pydantic import BaseModel

from a2a_pack import (
    EgressPolicy,
    LLMProvisioning,
    MetaAgent,
    MetaAgentManifest,
    NoAuth,
    Resources,
    WorkspaceAccess,
    WorkspaceMode,
)


META_AGENT_MANIFEST = MetaAgentManifest.from_mapping(
    json.loads({json.dumps(manifest_json)})
)


class {class_name}Config(BaseModel):
    pass


class {class_name}(MetaAgent[{class_name}Config, NoAuth]):
    name = {json.dumps(agent_name)}
    description = {json.dumps(description)}
    version = {json.dumps(version)}
    config_model = {class_name}Config
    auth_model = NoAuth
    meta_agent_manifest = META_AGENT_MANIFEST
    llm_provisioning = LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED
    wants_cp_jwt = True
    resources = Resources(cpu="500m", memory="1Gi", max_runtime_seconds=900)
    egress = EgressPolicy(
        allow_internal_services=(
            "control-plane.control-plane.svc.cluster.local",
            "litellm.llm.svc.cluster.local",
        ),
        deny_internet_by_default=False,
    )
    workspace_access = WorkspaceAccess.dynamic(
        max_files=100,
        allowed_modes=(
            WorkspaceMode.READ_ONLY,
            WorkspaceMode.READ_WRITE_OVERLAY,
        ),
        deny_patterns=(
            "secrets/**",
            ".env",
            "**/.env",
            "**/.git/**",
        ),
        require_human_approval=False,
    )
'''


def _a2a_yaml(
    *,
    name: str,
    class_name: str,
    description: str,
    version: str,
    manifest: MetaAgentManifest,
) -> str:
    data: dict[str, Any] = {
        "name": name,
        "version": version,
        "entrypoint": f"agent:{class_name}",
        "description": description,
    }
    manifest_data = manifest.model_dump(mode="json", exclude_none=True)
    for key in ("composition", "goal", "memory"):
        if manifest_data.get(key):
            data[key] = manifest_data[key]
    data["runtime"] = {
        "resources": {
            "cpu": "500m",
            "memory": "1Gi",
            "max_runtime_seconds": 900,
        },
        "llm_provisioning": "platform_or_caller_provided",
        "wants_cp_jwt": True,
        "egress": {
            "allow_internal_services": [
                "control-plane.control-plane.svc.cluster.local",
                "litellm.llm.svc.cluster.local",
            ],
            "deny_internet_by_default": False,
        },
    }
    return yaml.safe_dump(data, sort_keys=False)


def _requirements_txt() -> str:
    return """# a2a-pack is installed by the platform base image.
"""


def _warnings(manifest: MetaAgentManifest) -> list[str]:
    warnings: list[str] = []
    if manifest.memory and "kv" in manifest.memory.tiers:
        warnings.append("KV memory requires caller control-plane JWT forwarding.")
    if manifest.memory and "vector" in manifest.memory.tiers:
        warnings.append("Vector memory requires caller control-plane JWT forwarding.")
    return warnings
