from __future__ import annotations

import mimetypes
import os
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from ..agent import A2AAgent
from ..frontend import resolve_packed_frontend
from ..runtime import LLMProvisioning
from ..serve.asgi import build_app
from . import credentials
from .local import ensure_local_workspace, load_env_file, load_local_project


_LOCAL_LLM_TOOL_HINTS = {"deepagents", "langchain", "openapi", "llm"}
_LOCAL_LLM_DEFAULT_URL = "https://api.openai.com/v1"
_LOCAL_LLM_DEFAULT_MODEL = "gpt-4o"


def create_app():
    project = Path(os.environ.get("A2A_PROJECT_DIR", ".")).resolve()
    _ = os.environ["A2A_ENTRYPOINT"]
    env_file = Path(os.environ.get("A2A_ENV_FILE", project / ".env.local"))
    load_env_file(env_file)
    credentials.load_local_llm_into_env()
    local = load_local_project(project)
    credentials.load_agent_setup_into_env(
        local.agent_cls.name,
        _dev_setup_allowed_names_for_class(local.agent_cls),
    )
    workspace = ensure_local_workspace(
        project,
        workspace=Path(os.environ["A2A_LOCAL_WORKSPACE_DIR"])
        if os.environ.get("A2A_LOCAL_WORKSPACE_DIR")
        else None,
    )
    os.environ.setdefault("A2A_LOCAL_DEV", "1")
    os.environ.setdefault("A2A_LOCAL_WORKSPACE_DIR", str(workspace))
    agent = local.agent_cls()
    frontend = resolve_packed_frontend(project, local.config)
    app = build_app(agent, frontend=frontend)
    _mount_dev_ui(app, agent, workspace)
    return app


def _mount_dev_ui(app: Any, agent: A2AAgent, workspace: Path) -> None:
    static_root = resources.files("a2a_pack.cli.dev_ui_static")
    workspace_root = workspace.resolve()

    @app.get("/_dev", include_in_schema=False)
    @app.get("/_dev/", include_in_schema=False)
    async def dev_ui() -> HTMLResponse:
        return HTMLResponse(
            static_root.joinpath("index.html").read_text(encoding="utf-8")
        )

    @app.get("/_dev/assets/{path:path}", include_in_schema=False)
    async def dev_asset(path: str) -> FileResponse:
        target = _safe_static_path(static_root, path)
        if not target.is_file():
            raise HTTPException(404, "asset not found")
        return FileResponse(
            target,
            media_type=mimetypes.guess_type(target.name)[0]
            or "application/octet-stream",
        )

    @app.get("/_dev/api/card", include_in_schema=False)
    async def dev_card() -> dict[str, Any]:
        return agent.card().model_dump(mode="json")

    @app.get("/_dev/api/setup", include_in_schema=False)
    async def dev_setup() -> dict[str, Any]:
        return _dev_setup_payload(agent)

    @app.post("/_dev/api/setup", include_in_schema=False)
    async def dev_save_setup(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "expected JSON body") from exc
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise HTTPException(400, "expected values object")
        allowed = _dev_setup_allowed_names(agent)
        agent_values: dict[str, str] = {}
        for key, value in values.items():
            if key not in allowed:
                continue
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            os.environ[key] = text
            if not key.startswith("AGENT_LLM_"):
                agent_values[key] = text
        if agent_values:
            credentials.save_agent_setup(type(agent).name, agent_values)
        if os.environ.get("AGENT_LLM_KEY"):
            os.environ.setdefault("AGENT_LLM_URL", _LOCAL_LLM_DEFAULT_URL)
            os.environ.setdefault("AGENT_LLM_MODEL", _LOCAL_LLM_DEFAULT_MODEL)
            credentials.save_local_llm(
                api_key=os.environ["AGENT_LLM_KEY"],
                base_url=os.environ["AGENT_LLM_URL"],
                model=os.environ["AGENT_LLM_MODEL"],
            )
        return _dev_setup_payload(agent)

    @app.get("/_dev/api/files", include_in_schema=False)
    async def dev_files() -> list[dict[str, Any]]:
        return _list_workspace_files(workspace_root)

    @app.post("/_dev/api/files", include_in_schema=False)
    async def dev_upload_file(
        file: UploadFile = File(...),
        prefix: str = Form("inputs"),
    ) -> dict[str, Any]:
        filename = Path(file.filename or "upload.bin").name
        if not filename:
            raise HTTPException(400, "missing filename")
        rel = f"{prefix.strip('/')}/{filename}" if prefix.strip("/") else filename
        target = _safe_workspace_path(workspace_root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await file.read())
        return _file_meta(workspace_root, target)

    @app.get("/_dev/api/files/{path:path}", include_in_schema=False)
    async def dev_download_file(path: str) -> FileResponse:
        target = _safe_workspace_path(workspace_root, path)
        if not target.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(
            target,
            filename=target.name,
            media_type=mimetypes.guess_type(target.name)[0]
            or "application/octet-stream",
        )


def _dev_setup_payload(agent: A2AAgent) -> dict[str, Any]:
    fields = _dev_setup_fields(agent)
    missing = [
        field["name"]
        for field in fields
        if field["required"] and not field["configured"]
    ]
    return {
        "blocking": bool(missing),
        "missing": missing,
        "fields": fields,
    }


def _dev_setup_allowed_names(agent: A2AAgent) -> set[str]:
    names = _dev_setup_allowed_names_for_class(type(agent))
    if _dev_agent_likely_needs_llm(agent):
        names.update({"AGENT_LLM_KEY", "AGENT_LLM_URL", "AGENT_LLM_MODEL"})
    return names


def _dev_setup_allowed_names_for_class(agent_cls: type[A2AAgent]) -> set[str]:
    names = {field.name for field in agent_cls.consumer_setup.fields}
    names.update(agent_cls.required_env)
    names.update(agent_cls.required_secrets)
    return names


def _dev_setup_fields(agent: A2AAgent) -> list[dict[str, Any]]:
    agent_cls = type(agent)
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()

    for name in agent_cls.required_env:
        fields.append(_env_setup_field(name, kind="config", group="Runtime env"))
        seen.add(name)
    for name in agent_cls.required_secrets:
        fields.append(_env_setup_field(name, kind="secret", group="Runtime env"))
        seen.add(name)

    for field in agent_cls.consumer_setup.fields:
        if field.name in seen:
            continue
        value = os.environ.get(field.name)
        item: dict[str, Any] = {
            "name": field.name,
            "label": field.label or field.name,
            "description": field.description,
            "kind": field.kind,
            "input_type": field.input_type,
            "required": field.required,
            "configured": bool(value),
            "group": "Agent setup",
        }
        if field.options:
            item["options"] = list(field.options)
        if field.kind != "secret" and value is not None:
            item["value"] = value
        fields.append(item)
        seen.add(field.name)

    if _dev_agent_likely_needs_llm(agent):
        llm_configured = bool(
            os.environ.get("AGENT_LLM_KEY")
            or os.environ.get("A2A_LITELLM_KEY")
        )
        fields.extend(
            [
                {
                    "name": "AGENT_LLM_KEY",
                    "label": "LLM API key",
                    "description": "OpenAI-compatible key used by local ctx.llm.",
                    "kind": "secret",
                    "input_type": "password",
                    "required": True,
                    "configured": llm_configured,
                    "group": "LLM",
                },
                {
                    "name": "AGENT_LLM_URL",
                    "label": "LLM base URL",
                    "description": "OpenAI-compatible API base URL.",
                    "kind": "config",
                    "input_type": "url",
                    "required": False,
                    "configured": True,
                    "value": os.environ.get("AGENT_LLM_URL", _LOCAL_LLM_DEFAULT_URL),
                    "group": "LLM",
                },
                {
                    "name": "AGENT_LLM_MODEL",
                    "label": "LLM model",
                    "description": "Model name passed to local ctx.llm.",
                    "kind": "config",
                    "input_type": "text",
                    "required": False,
                    "configured": True,
                    "value": os.environ.get(
                        "AGENT_LLM_MODEL",
                        _LOCAL_LLM_DEFAULT_MODEL,
                    ),
                    "group": "LLM",
                },
            ]
        )

    return fields


def _env_setup_field(name: str, *, kind: str, group: str) -> dict[str, Any]:
    value = os.environ.get(name)
    item: dict[str, Any] = {
        "name": name,
        "label": name,
        "description": "",
        "kind": kind,
        "input_type": "password" if kind == "secret" else "text",
        "required": True,
        "configured": bool(value),
        "group": group,
    }
    if kind != "secret" and value is not None:
        item["value"] = value
    return item


def _dev_agent_likely_needs_llm(agent: A2AAgent) -> bool:
    agent_cls = type(agent)
    provisioning = agent_cls.llm_provisioning
    if provisioning not in {
        LLMProvisioning.PLATFORM,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED,
        LLMProvisioning.CALLER_PROVIDED,
        LLMProvisioning.AGENT_BYOK,
    }:
        return False
    tools = {str(tool).lower() for tool in agent_cls.tools_used}
    return bool(tools.intersection(_LOCAL_LLM_TOOL_HINTS))


def _list_workspace_files(root: Path) -> list[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    return sorted(
        (_file_meta(root, path) for path in root.rglob("*") if path.is_file()),
        key=lambda item: item["path"],
    )


def _file_meta(root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    rel = path.resolve().relative_to(root).as_posix()
    return {
        "path": rel,
        "size_bytes": stat.st_size,
        "content_type": mimetypes.guess_type(path.name)[0]
        or "application/octet-stream",
        "updated_at": stat.st_mtime,
    }


def _safe_workspace_path(root: Path, relpath: str) -> Path:
    clean = relpath.replace("\\", "/").lstrip("/")
    if not clean or clean == ".":
        raise HTTPException(400, "path is required")
    target = (root / clean).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "unsafe workspace path") from exc
    return target


def _safe_static_path(root: Any, relpath: str) -> Path:
    clean = relpath.replace("\\", "/").lstrip("/")
    if not clean or clean == ".":
        raise HTTPException(400, "path is required")
    base = Path(str(root)).resolve()
    target = (base / clean).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(400, "unsafe asset path") from exc
    return target
