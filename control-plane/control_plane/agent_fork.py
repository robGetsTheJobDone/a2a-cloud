"""Create a sanitized source fork without copying tenant runtime state."""

from __future__ import annotations

import io
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml
from a2a_pack import AgentDsl, AgentDslAuth, AgentDslEntrypoint, AgentDslSkill

from .gitea import source_tarball_from_repo


_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DENIED_PARTS = frozenset({".git", ".agent-studio", "secrets", "secret", "memory", "data"})
_DENIED_NAMES = frozenset({".env", ".env.local", ".env.production"})
_DENIED_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12")


@dataclass(frozen=True)
class ForkBundle:
    tarball: bytes
    dsl: AgentDsl
    source_sha: str
    skipped_paths: tuple[str, ...]


def build_sanitized_fork_bundle(
    *,
    source_name: str,
    source_owner: str | None,
    target_name: str,
    target_version: str,
    description: str,
    source_card: dict[str, object],
) -> ForkBundle:
    raw_bundle, source_sha = source_tarball_from_repo(
        source_name,
        owner=source_owner,
    )
    files, skipped = _read_sanitized_files(raw_bundle)
    manifest_path = next(
        (name for name in ("a2a.yaml", "a2a.yml") if name in files),
        None,
    )
    if manifest_path is None:
        raise ValueError("source fork requires an a2a.yaml manifest")
    try:
        manifest = yaml.safe_load(files[manifest_path].decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("source a2a.yaml is not valid UTF-8 YAML") from exc
    if not isinstance(manifest, dict):
        raise ValueError("source a2a.yaml must be an object")
    entrypoint = str(manifest.get("entrypoint") or "").strip()
    module, separator, class_name = entrypoint.partition(":")
    module = module.strip()
    class_name = class_name.strip() if separator else ""
    if not module or not class_name or not _MODULE_RE.fullmatch(module) or not _CLASS_RE.fullmatch(class_name):
        raise ValueError("source entrypoint cannot be safely wrapped for a fork")

    wrapper_module = "_a2a_studio_fork"
    wrapper_class = "StudioForkedAgent"
    files[f"{wrapper_module}.py"] = _wrapper_source(
        module=module,
        class_name=class_name,
        wrapper_class=wrapper_class,
        target_name=target_name,
        target_version=target_version,
        description=description,
    ).encode("utf-8")
    manifest["name"] = target_name
    manifest["version"] = target_version
    manifest["description"] = description
    manifest["entrypoint"] = f"{wrapper_module}:{wrapper_class}"
    files[manifest_path] = yaml.safe_dump(
        manifest,
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    target_dsl = _dsl_from_card(
        card=source_card,
        manifest=manifest,
        target_name=target_name,
        target_version=target_version,
        description=description,
        wrapper_module=wrapper_module,
        wrapper_class=wrapper_class,
    )
    return ForkBundle(
        tarball=_write_tarball(files),
        dsl=target_dsl,
        source_sha=source_sha,
        skipped_paths=tuple(sorted(skipped)),
    )


def _dsl_from_card(
    *,
    card: dict[str, object],
    manifest: dict[str, object],
    target_name: str,
    target_version: str,
    description: str,
    wrapper_module: str,
    wrapper_class: str,
) -> AgentDsl:
    """Project the stored, already-sanitized card back into deployment DSL.

    Source is never imported in the control-plane process.  Forks add a
    platform-user auth boundary even when the parent was public, so a missing
    auth signal cannot accidentally weaken access in the child.
    """
    raw_skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    skills: list[AgentDslSkill] = []
    for raw in raw_skills:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("id") or "").strip()
        if not name:
            continue
        input_schema = raw.get("input_schema")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            input_schema = {"type": "object", "properties": {}, "required": []}
        else:
            input_schema = dict(input_schema)
            input_schema.setdefault("properties", {})
            input_schema.setdefault("required", [])
        output_schema = raw.get("output_schema")
        if not isinstance(output_schema, dict) or not output_schema:
            output_schema = {"type": "object"}
        skills.append(
            AgentDslSkill(
                name=name,
                description=str(raw.get("description") or ""),
                handler=name,
                tags=tuple(str(item) for item in raw.get("tags") or []),
                scopes=tuple(str(item) for item in raw.get("scopes") or []),
                stream=bool(raw.get("stream")),
                policy=raw.get("policy") if isinstance(raw.get("policy"), dict) else {},
                input_schema=input_schema,
                output_schema=output_schema,
            )
        )
    if not skills:
        raise ValueError("source card has no callable skills to fork")
    language = str(manifest.get("language") or "python").strip().lower()
    if language != "python":
        raise ValueError("source forks currently require a Python agent")
    return AgentDsl(
        language="python",
        name=target_name,
        description=description,
        version=target_version,
        entrypoint=AgentDslEntrypoint(
            module=wrapper_module,
            class_name=wrapper_class,
        ),
        skills=tuple(skills),
        capabilities=dict(card.get("capabilities") or {})
        if isinstance(card.get("capabilities"), dict)
        else {},
        input_modes=tuple(str(item) for item in card.get("input_modes") or ["application/json"]),
        output_modes=tuple(str(item) for item in card.get("output_modes") or ["application/json"]),
        required_secrets=tuple(str(item) for item in card.get("required_secrets") or []),
        required_env=tuple(str(item) for item in card.get("required_env") or []),
        consumer_setup=card.get("consumer_setup") or {},
        runtime=card.get("runtime") or {},
        template_lineage=card.get("template_lineage"),
        state_schema=card.get("state_schema")
        if isinstance(card.get("state_schema"), dict)
        else None,
        workspace_access=card.get("workspace_access") or {},
        auth=AgentDslAuth(
            model="a2a_pack.auth.PlatformUserAuth",
            strategy="platform_user",
            principal_schema={"type": "object", "properties": {}},
            required=True,
        ),
        metadata={
            "source": "agent-studio-fork",
            "source_agent": str(card.get("name") or ""),
        },
    )


def _read_sanitized_files(bundle: bytes) -> tuple[dict[str, bytes], set[str]]:
    files: dict[str, bytes] = {}
    skipped: set[str] = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            clean = path.as_posix()
            while clean.startswith("./"):
                clean = clean[2:]
            if not clean or path.is_absolute() or ".." in path.parts:
                raise ValueError("source archive contains an unsafe path")
            if not member.isfile():
                if member.issym() or member.islnk():
                    raise ValueError("source archive links are not forkable")
                continue
            if _is_tenant_state_path(path):
                skipped.add(clean)
                continue
            total += max(0, int(member.size))
            if total > 25 * 1024 * 1024:
                raise ValueError("source fork exceeds the 25 MiB source limit")
            source = archive.extractfile(member)
            if source is None:
                continue
            files[clean] = source.read()
    return files, skipped


def _is_tenant_state_path(path: PurePosixPath) -> bool:
    lowered = tuple(part.lower() for part in path.parts)
    name = path.name.lower()
    return (
        bool(set(lowered) & _DENIED_PARTS)
        or name in _DENIED_NAMES
        or name.startswith(".env.")
        or name.endswith(_DENIED_SUFFIXES)
    )


def _wrapper_source(
    *,
    module: str,
    class_name: str,
    wrapper_class: str,
    target_name: str,
    target_version: str,
    description: str,
) -> str:
    return (
        "from a2a_pack import PlatformUserAuth\n"
        f"from {module} import {class_name} as _SourceAgent\n\n\n"
        f"class {wrapper_class}(_SourceAgent):\n"
        f"    name = {json.dumps(target_name)}\n"
        f"    version = {json.dumps(target_version)}\n"
        f"    description = {json.dumps(description)}\n"
        "    auth_model = PlatformUserAuth\n"
    )


def _write_tarball(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in sorted(files):
            body = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()
