from __future__ import annotations

import asyncio
import mimetypes
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .agent import A2AAgent
from .auth import PlatformUserAuth
from .runtime import LLMProvisioning

DEFAULT_FRONTEND_MOUNT = "/"
FRONTEND_AGENT_API_PREFIX = "/_a2a"
from .cli.platform import docs_url as _platform_docs_url

DEFAULT_DOCS_URL = _platform_docs_url()
STATIC_FRONTEND_KINDS = {"static", "static-spa", "spa"}
SERVER_RENDERED_FRONTEND_KINDS = {"server-rendered", "server", "ssr"}
SERVER_RENDERED_FRONTEND_KIND = "server-rendered"
DEFAULT_FRONTEND_SERVER_PORT = 3000


@dataclass(frozen=True)
class FrontendConfig:
    """Manifest declaration for a frontend packed with an agent."""

    path: str = "frontend"
    dist: str = "dist"
    mount: str = DEFAULT_FRONTEND_MOUNT
    build: str | None = None
    auth: str = "inherit"
    kind: str = "static"
    docs_url: str = DEFAULT_DOCS_URL
    framework: str | None = None
    start: str | None = None
    port: int = DEFAULT_FRONTEND_SERVER_PORT

    @property
    def enabled(self) -> bool:
        return self.is_static or self.is_server_rendered

    @property
    def is_static(self) -> bool:
        return self.kind in STATIC_FRONTEND_KINDS

    @property
    def is_server_rendered(self) -> bool:
        return self.kind == SERVER_RENDERED_FRONTEND_KIND

    def source_dir(self, project: Path) -> Path:
        return _safe_project_path(project, self.path)

    def dist_dir(self, project: Path) -> Path:
        dist = Path(self.dist)
        if dist.is_absolute():
            return dist.resolve()
        return _safe_child_path(self.source_dir(project), self.dist)

    def server_ready_path(self, project: Path) -> Path:
        if self.framework == "nextjs":
            source = self.source_dir(project)
            direct = _safe_child_path(source, ".next/standalone/server.js")
            if direct.exists():
                return direct
            standalone = _safe_child_path(source, ".next/standalone")
            if standalone.is_dir():
                matches = sorted(standalone.glob("*/server.js"))
                if matches:
                    return matches[0]
            return direct
        return self.source_dir(project)


@dataclass(frozen=True)
class PackedFrontend:
    """Resolved frontend ready to serve or proxy from the agent process."""

    dist_dir: Path | None = None
    mount: str = DEFAULT_FRONTEND_MOUNT
    auth: str = "inherit"
    docs_url: str = DEFAULT_DOCS_URL
    kind: str = "static-spa"
    framework: str | None = None
    proxy_url: str | None = None
    start: str | None = None
    workdir: Path | None = None
    port: int = DEFAULT_FRONTEND_SERVER_PORT

    @property
    def is_static(self) -> bool:
        return self.kind in STATIC_FRONTEND_KINDS

    @property
    def is_server_rendered(self) -> bool:
        return self.kind == SERVER_RENDERED_FRONTEND_KIND

    @property
    def config_path(self) -> str:
        return _mount_path(self.mount, "config.json")

    @property
    def client_path(self) -> str:
        return _mount_path(self.mount, "a2a-client.js")


def load_frontend_config(
    project: Path,
    config: dict[str, Any] | None = None,
) -> FrontendConfig | None:
    """Read the optional ``frontend`` section from ``a2a.yaml``."""

    data = config if config is not None else _read_project_config(project)
    raw = data.get("frontend")
    if raw in (None, False):
        return None
    if raw is True:
        raw = {}
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict):
        raise ValueError("frontend must be an object, string path, true, or false")

    kind = _clean_frontend_kind(raw.get("type") or raw.get("kind") or "static")
    framework = _clean_optional_string(raw.get("framework"))
    if framework is not None:
        framework = framework.lower()
    if kind == SERVER_RENDERED_FRONTEND_KIND:
        framework = framework or "nextjs"
        if framework != "nextjs":
            raise ValueError("frontend.framework currently supports only nextjs")

    return FrontendConfig(
        path=_clean_relpath(str(raw.get("path") or "frontend")),
        dist=_clean_relpath(str(raw.get("dist") or "dist")),
        mount=_normalize_mount(str(raw.get("mount") or DEFAULT_FRONTEND_MOUNT)),
        build=_clean_optional_string(raw.get("build")),
        auth=_clean_auth(raw.get("auth")),
        kind=kind,
        docs_url=_clean_docs_url(raw.get("docs_url") or raw.get("docsUrl")),
        framework=framework,
        start=_clean_optional_string(raw.get("start")),
        port=_clean_port(raw.get("port")),
    )


def resolve_packed_frontend(
    project: Path,
    config: dict[str, Any] | None = None,
    *,
    require_dist: bool = True,
) -> PackedFrontend | None:
    cfg = load_frontend_config(project, config)
    if cfg is None or not cfg.enabled:
        return None
    if cfg.is_server_rendered:
        return PackedFrontend(
            mount=cfg.mount,
            auth=cfg.auth,
            docs_url=cfg.docs_url,
            kind=cfg.kind,
            framework=cfg.framework,
            proxy_url=f"http://127.0.0.1:{cfg.port}",
            start=None,
            workdir=cfg.source_dir(project),
            port=cfg.port,
        )
    dist_dir = cfg.dist_dir(project)
    if require_dist and not (dist_dir / "index.html").is_file():
        return None
    return PackedFrontend(
        dist_dir=dist_dir,
        mount=cfg.mount,
        auth=cfg.auth,
        docs_url=cfg.docs_url,
        kind=cfg.kind,
    )


def packed_frontend_from_env() -> PackedFrontend | None:
    kind = _clean_frontend_kind(os.environ.get("A2A_FRONTEND_KIND") or "static-spa")
    proxy_url = _clean_optional_string(os.environ.get("A2A_FRONTEND_PROXY_URL"))
    if kind == SERVER_RENDERED_FRONTEND_KIND or proxy_url:
        port = _clean_port(os.environ.get("A2A_FRONTEND_PORT"))
        return PackedFrontend(
            mount=_normalize_mount(os.environ.get("A2A_FRONTEND_MOUNT") or DEFAULT_FRONTEND_MOUNT),
            auth=_clean_auth(os.environ.get("A2A_FRONTEND_AUTH")),
            docs_url=_clean_docs_url(os.environ.get("A2A_FRONTEND_DOCS_URL")),
            kind=SERVER_RENDERED_FRONTEND_KIND,
            framework=_clean_optional_string(os.environ.get("A2A_FRONTEND_FRAMEWORK")),
            proxy_url=proxy_url or f"http://127.0.0.1:{port}",
            start=_clean_optional_string(os.environ.get("A2A_FRONTEND_START")),
            workdir=Path(os.environ["A2A_FRONTEND_WORKDIR"]).resolve()
            if os.environ.get("A2A_FRONTEND_WORKDIR")
            else None,
            port=port,
        )
    dist = os.environ.get("A2A_FRONTEND_DIST")
    if not dist:
        return None
    return PackedFrontend(
        dist_dir=Path(dist).resolve(),
        mount=_normalize_mount(os.environ.get("A2A_FRONTEND_MOUNT") or DEFAULT_FRONTEND_MOUNT),
        auth=_clean_auth(os.environ.get("A2A_FRONTEND_AUTH")),
        docs_url=_clean_docs_url(os.environ.get("A2A_FRONTEND_DOCS_URL")),
        kind=kind,
    )


def export_frontend_env(frontend: PackedFrontend | None) -> None:
    """Expose a resolved frontend to ``build_app`` across uvicorn reloads."""

    if frontend is None:
        for key in (
            "A2A_FRONTEND_DIST",
            "A2A_FRONTEND_KIND",
            "A2A_FRONTEND_MOUNT",
            "A2A_FRONTEND_AUTH",
            "A2A_FRONTEND_DOCS_URL",
            "A2A_FRONTEND_FRAMEWORK",
            "A2A_FRONTEND_PROXY_URL",
            "A2A_FRONTEND_START",
            "A2A_FRONTEND_WORKDIR",
            "A2A_FRONTEND_PORT",
        ):
            os.environ.pop(key, None)
        return
    os.environ["A2A_FRONTEND_KIND"] = frontend.kind
    if frontend.dist_dir is not None:
        os.environ["A2A_FRONTEND_DIST"] = str(frontend.dist_dir)
    else:
        os.environ.pop("A2A_FRONTEND_DIST", None)
    os.environ["A2A_FRONTEND_MOUNT"] = frontend.mount
    os.environ["A2A_FRONTEND_AUTH"] = frontend.auth
    os.environ["A2A_FRONTEND_DOCS_URL"] = frontend.docs_url
    for key in (
        "A2A_FRONTEND_FRAMEWORK",
        "A2A_FRONTEND_PROXY_URL",
        "A2A_FRONTEND_START",
        "A2A_FRONTEND_WORKDIR",
    ):
        os.environ.pop(key, None)
    if frontend.framework:
        os.environ["A2A_FRONTEND_FRAMEWORK"] = frontend.framework
    if frontend.proxy_url:
        os.environ["A2A_FRONTEND_PROXY_URL"] = frontend.proxy_url
    if frontend.start:
        os.environ["A2A_FRONTEND_START"] = frontend.start
    if frontend.workdir:
        os.environ["A2A_FRONTEND_WORKDIR"] = str(frontend.workdir)
    os.environ["A2A_FRONTEND_PORT"] = str(frontend.port)


def _agent_session_authorize_url() -> str | None:
    """The platform hand-off URL that mints this origin's session.

    Pre-bound to this agent's name so the gateway knows which audience to
    scope the resulting token to.
    """
    base = _clean_optional_string(os.environ.get("A2A_AGENT_SESSION_AUTHORIZE_URL"))
    name = _clean_optional_string(os.environ.get("A2A_AGENT_NAME"))
    if not base or not name:
        return None
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}agent={quote(name, safe='')}"


def frontend_auth_metadata(frontend: PackedFrontend, request: Request | None = None) -> dict[str, Any]:
    requires_session = frontend_requires_session(frontend)
    return {
        "mode": frontend.auth,
        "flow": "platform-browser-session" if requires_session else "public",
        "sessionTransport": "cookie",
        "sessionCookieName": os.environ.get("A2A_SESSION_COOKIE_NAME") or "a2a_session",
        "sessionUrl": _request_url(request, "/auth/session"),
        "loginUrl": _clean_optional_string(os.environ.get("A2A_LOGIN_URL")),
        "authorizeUrl": _agent_session_authorize_url(),
        "callbackUrl": _request_url(request, "/auth/callback"),
        "requiresSession": requires_session,
    }


def frontend_ui_metadata(frontend: PackedFrontend, request: Request | None = None) -> dict[str, Any]:
    entry = frontend.mount if frontend.is_server_rendered else _mount_path(frontend.mount, "index.html")
    return {
        "url": _request_url(request, frontend.mount),
        "type": frontend.kind,
        "framework": frontend.framework,
        "entry": entry,
        "config": _request_url(request, frontend.config_path),
        "client": _request_url(request, frontend.client_path),
        "requiresAuth": frontend_requires_session(frontend),
        "auth": frontend_auth_metadata(frontend, request),
    }


def frontend_config_payload(
    agent: A2AAgent,
    frontend: PackedFrontend,
    request: Request,
) -> dict[str, Any]:
    card = agent.card().model_dump(mode="json")
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    return {
        "agent": {
            "name": card.get("name") or type(agent).name,
            "description": card.get("description") or type(agent).description,
            "version": card.get("version") or type(agent).version,
        },
        "mount": frontend.mount,
        "ui": frontend_ui_metadata(frontend, request),
        "auth": {
            **frontend_auth_metadata(frontend, request),
            "invokeRequiresSession": _agent_requires_platform_session(agent),
        },
        "endpoints": {
            "a2a": _request_url(request, frontend_agent_api_path(frontend, "/")),
            "agentCard": _request_url(
                request,
                frontend_agent_api_path(frontend, "/.well-known/agent-card.json"),
            ),
            "legacyAgentCard": _request_url(
                request,
                frontend_agent_api_path(frontend, "/.well-known/agent-card"),
            ),
            "skills": _request_url(
                request,
                frontend_agent_api_path(frontend, "/.well-known/a2a-skills.json"),
            ),
            "invoke": _request_url(request, frontend_agent_api_path(frontend, "/invoke")),
            "mcp": _request_url(request, frontend_agent_api_path(frontend, "/mcp")),
            "session": _request_url(request, frontend_agent_api_path(frontend, "/auth/session")),
        },
        "skills": skills,
        "docs": {
            "base": frontend.docs_url,
            "install": f"{frontend.docs_url.rstrip('/')}/",
        },
    }


def frontend_agent_api_path(frontend: PackedFrontend | FrontendConfig | None, path: str) -> str:
    clean = path if path.startswith("/") else f"/{path}"
    if frontend is not None and frontend.mount == "/":
        if clean == "/":
            return FRONTEND_AGENT_API_PREFIX
        return f"{FRONTEND_AGENT_API_PREFIX}{clean}"
    return clean


def skills_payload(agent: A2AAgent, request: Request | None = None) -> dict[str, Any]:
    card = agent.card().model_dump(mode="json")
    invoke_path = _request_agent_api_path(request, "/invoke")
    mcp_path = _request_agent_api_path(request, "/mcp")
    return {
        "agent": {
            "name": card.get("name") or type(agent).name,
            "version": card.get("version") or type(agent).version,
        },
        "skills": card.get("skills") if isinstance(card.get("skills"), list) else [],
        "invokeUrl": _request_url(request, invoke_path),
        "mcpUrl": _request_url(request, mcp_path),
        "docsUrl": DEFAULT_DOCS_URL,
    }


def mount_packed_frontend(
    app: FastAPI,
    agent: A2AAgent,
    frontend: PackedFrontend,
    *,
    require_session: Callable[[Request], Awaitable[Response | None]] | None = None,
) -> None:
    app.state.a2a_frontend = frontend

    if frontend.is_server_rendered:
        _mount_server_rendered_frontend(
            app,
            agent,
            frontend,
            require_session=FrontEndSessionHook(require_session),
        )
        return

    if frontend.dist_dir is None:
        raise ValueError("static frontend requires dist_dir")
    root = frontend.dist_dir.resolve()

    async def authorize(request: Request) -> Response | None:
        if require_session is not None and frontend_requires_session(frontend):
            return await require_session(request)
        return None

    @app.get(frontend.config_path, include_in_schema=False, response_model=None)
    async def packed_frontend_config(request: Request) -> Any:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return frontend_config_payload(agent, frontend, request)

    @app.get(frontend.client_path, include_in_schema=False)
    async def packed_frontend_client(request: Request) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return Response(_FRONTEND_CLIENT_JS, media_type="text/javascript; charset=utf-8")

    async def packed_frontend_index(request: Request) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return _index_response(root)

    async def packed_frontend_redirect(request: Request) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        return RedirectResponse(f"{frontend.mount}/", status_code=307)

    async def packed_frontend_asset(request: Request, path: str) -> Response:
        auth_response = await authorize(request)
        if auth_response is not None:
            return auth_response
        if path in {"", "/", "index.html"}:
            return _index_response(root)
        target = _safe_asset_path(root, path)
        if target.is_file():
            return FileResponse(
                target,
                media_type=mimetypes.guess_type(target.name)[0]
                or "application/octet-stream",
            )
        return _index_response(root)

    if frontend.mount == "/":
        app.add_api_route(
            "/",
            packed_frontend_index,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
    else:
        app.add_api_route(
            frontend.mount,
            packed_frontend_redirect,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
        app.add_api_route(
            f"{frontend.mount}/",
            packed_frontend_index,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
    catchall = "/{path:path}" if frontend.mount == "/" else f"{frontend.mount}/{{path:path}}"
    app.add_api_route(
        catchall,
        packed_frontend_asset,
        methods=["GET"],
        include_in_schema=False,
        response_model=None,
    )


class FrontEndSessionHook:
    def __init__(
        self,
        require_session: Callable[[Request], Awaitable[Response | None]] | None,
    ) -> None:
        self.require_session = require_session

    async def __call__(self, frontend: PackedFrontend, request: Request) -> Response | None:
        if self.require_session is not None and frontend_requires_session(frontend):
            return await self.require_session(request)
        return None


def _mount_server_rendered_frontend(
    app: FastAPI,
    agent: A2AAgent,
    frontend: PackedFrontend,
    *,
    require_session: FrontEndSessionHook,
) -> None:
    start_frontend_process(app, frontend)

    @app.get(frontend.config_path, include_in_schema=False, response_model=None)
    async def server_frontend_config(request: Request) -> Any:
        auth_response = await require_session(frontend, request)
        if auth_response is not None:
            return auth_response
        return frontend_config_payload(agent, frontend, request)

    @app.get(frontend.client_path, include_in_schema=False)
    async def server_frontend_client(request: Request) -> Response:
        auth_response = await require_session(frontend, request)
        if auth_response is not None:
            return auth_response
        return Response(_FRONTEND_CLIENT_JS, media_type="text/javascript; charset=utf-8")

    async def server_frontend_redirect(request: Request) -> Response:
        auth_response = await require_session(frontend, request)
        if auth_response is not None:
            return auth_response
        return RedirectResponse(f"{frontend.mount}/", status_code=307)

    async def server_frontend_proxy(request: Request, path: str = "") -> Response:
        auth_response = await require_session(frontend, request)
        if auth_response is not None:
            return auth_response
        return await proxy_frontend_request(frontend, request)

    if frontend.mount != "/":
        app.add_api_route(
            frontend.mount,
            server_frontend_redirect,
            methods=["GET"],
            include_in_schema=False,
            response_model=None,
        )
    catchall = "/{path:path}" if frontend.mount == "/" else f"{frontend.mount}/{{path:path}}"
    app.add_api_route(
        catchall,
        server_frontend_proxy,
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
        response_model=None,
    )


def start_frontend_process(app: FastAPI, frontend: PackedFrontend) -> None:
    if not frontend.is_server_rendered or not frontend.start:
        return

    @app.on_event("startup")
    async def _start_frontend_server() -> None:
        if getattr(app.state, "a2a_frontend_process", None) is not None:
            return
        env = os.environ.copy()
        env["PORT"] = str(frontend.port)
        env["HOSTNAME"] = "127.0.0.1"
        process = await asyncio.create_subprocess_shell(
            frontend.start,
            cwd=str(frontend.workdir) if frontend.workdir else None,
            env=env,
        )
        app.state.a2a_frontend_process = process
        app.state.a2a_frontend_stopping = False

        async def _monitor_frontend_server() -> None:
            code = await process.wait()
            if not getattr(app.state, "a2a_frontend_stopping", False):
                os._exit(code if code else 1)

        app.state.a2a_frontend_monitor = asyncio.create_task(_monitor_frontend_server())

    @app.on_event("shutdown")
    async def _stop_frontend_server() -> None:
        process = getattr(app.state, "a2a_frontend_process", None)
        if process is None:
            return
        app.state.a2a_frontend_stopping = True
        if process.returncode is None:
            process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
        if process.returncode is None:
            process.kill()
            await process.wait()
        monitor = getattr(app.state, "a2a_frontend_monitor", None)
        if monitor is not None:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor


async def proxy_frontend_request(frontend: PackedFrontend, request: Request) -> Response:
    if not frontend.proxy_url:
        raise HTTPException(502, "frontend proxy is not configured")
    path = request.url.path
    if frontend.mount != "/" and path.startswith(frontend.mount):
        path = path[len(frontend.mount):] or "/"
    target = f"{frontend.proxy_url.rstrip('/')}{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            proxied = await client.request(
                request.method,
                target,
                content=await request.body(),
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"frontend proxy failed: {exc}") from exc
    response_headers = {
        key: value
        for key, value in proxied.headers.items()
        if key.lower()
        not in {"connection", "content-length", "transfer-encoding", "content-encoding"}
    }
    return Response(
        content=proxied.content,
        status_code=proxied.status_code,
        headers=response_headers,
        media_type=response_headers.get("content-type"),
    )


def _read_project_config(project: Path) -> dict[str, Any]:
    yaml_path = project / "a2a.yaml"
    if not yaml_path.exists():
        return {}
    data = yaml.safe_load(yaml_path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _normalize_mount(raw: str) -> str:
    value = raw.strip() or DEFAULT_FRONTEND_MOUNT
    if not value.startswith("/"):
        value = f"/{value}"
    if len(value) > 1:
        value = value.rstrip("/")
    return value


def _mount_path(mount: str, child: str) -> str:
    clean = child.strip("/")
    if mount == "/":
        return f"/{clean}"
    return f"{mount}/{clean}"


def _request_url(request: Request | None, path: str) -> str:
    if request is None:
        return path
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    base = f"{scheme}://{host}".rstrip("/")
    clean = path if path.startswith("/") else f"/{path}"
    return f"{base}{clean}"


def _request_agent_api_path(request: Request | None, path: str) -> str:
    clean = path if path.startswith("/") else f"/{path}"
    if request is not None and (
        request.url.path == FRONTEND_AGENT_API_PREFIX
        or request.url.path.startswith(f"{FRONTEND_AGENT_API_PREFIX}/")
    ):
        return f"{FRONTEND_AGENT_API_PREFIX}{clean}"
    return clean


def _clean_relpath(raw: str) -> str:
    value = raw.strip().replace("\\", "/").strip("/")
    if not value or value == "." or ".." in Path(value).parts:
        raise ValueError(f"unsafe frontend path: {raw!r}")
    return value


def _safe_project_path(project: Path, relpath: str) -> Path:
    return _safe_child_path(project.resolve(), relpath)


def _safe_child_path(root: Path, relpath: str) -> Path:
    clean = relpath.replace("\\", "/").lstrip("/")
    target = (root / clean).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"unsafe frontend path: {relpath!r}") from exc
    return target


def _safe_asset_path(root: Path, relpath: str) -> Path:
    clean = relpath.replace("\\", "/").lstrip("/")
    if not clean or clean == ".":
        raise HTTPException(400, "path is required")
    target = (root / clean).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "unsafe asset path") from exc
    return target


def _index_response(root: Path) -> HTMLResponse:
    index = root / "index.html"
    if not index.is_file():
        raise HTTPException(404, "frontend index.html not found")
    return HTMLResponse(index.read_text(encoding="utf-8"))


def _clean_optional_string(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _clean_auth(raw: Any) -> str:
    value = str(raw or "inherit").strip().lower()
    return value if value in {"inherit", "platform", "public"} else "inherit"


def frontend_requires_session(frontend: PackedFrontend | FrontendConfig) -> bool:
    if frontend.auth == "public":
        return False
    if frontend.auth == "platform":
        return True
    public_env = os.environ.get("A2A_AGENT_PUBLIC", "").strip().lower()
    return public_env in {"false", "0", "no"}


def _agent_requires_platform_session(agent: A2AAgent) -> bool:
    account_access = getattr(type(agent), "account_access", None)
    if bool(getattr(account_access, "required", False)):
        return True
    if type(agent).auth_model is PlatformUserAuth:
        return True
    provisioning = type(agent).llm_provisioning
    value = (
        provisioning.value
        if isinstance(provisioning, LLMProvisioning)
        else str(provisioning)
    )
    return value in {
        LLMProvisioning.PLATFORM.value,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED.value,
    }


def _clean_docs_url(raw: Any) -> str:
    value = str(raw or DEFAULT_DOCS_URL).strip()
    return value or DEFAULT_DOCS_URL


def _clean_frontend_kind(raw: Any) -> str:
    value = str(raw or "static").strip().lower()
    if value in STATIC_FRONTEND_KINDS:
        return value
    if value in SERVER_RENDERED_FRONTEND_KINDS:
        return SERVER_RENDERED_FRONTEND_KIND
    raise ValueError("frontend.type must be static-spa or server-rendered")


def _clean_port(raw: Any) -> int:
    if raw in (None, ""):
        return DEFAULT_FRONTEND_SERVER_PORT
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("frontend.port must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("frontend.port must be between 1 and 65535")
    return port


_FRONTEND_CLIENT_JS = """
export async function loadA2AConfig(configUrl = "./config.json") {
  const response = await fetch(configUrl, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`failed to load A2A config: ${response.status}`);
  return response.json();
}

export function unwrapInvokeResponse(payload) {
  if (payload && typeof payload === "object" && "result" in payload) {
    return payload.result;
  }
  return payload;
}

export async function createA2AClient(options = {}) {
  const config = options.config || await loadA2AConfig(options.configUrl || "./config.json");
  const fetchImpl = options.fetch || fetch;
  const credentials = options.credentials || "same-origin";

  async function requestJson(url, init = {}) {
    const response = await fetchImpl(url, { credentials, ...init });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const detail = data && data.detail;
      const message = (detail && typeof detail === "object"
        ? detail.message
        : data && (detail || data.message)) || `request failed: ${response.status}`;
      throw new Error(message);
    }
    return data;
  }

  async function requireSession() {
    const session = await requestJson(config.endpoints.session);
    if (!session || !session.authenticated) {
      // Go to the platform's agent-session gateway, not the bare dashboard.
      // The dashboard's cookie is host-locked to its own origin, so signing in
      // there alone would bounce straight back here still signed out. The
      // gateway hands this origin a code it can trade for its own session.
      const authorizeUrl = config.auth && config.auth.authorizeUrl;
      const loginUrl = config.auth && config.auth.loginUrl;
      const target = authorizeUrl || loginUrl;
      if (target && typeof window !== "undefined") {
        const next = encodeURIComponent(
          window.location.pathname + window.location.search + window.location.hash,
        );
        window.location.assign(`${target}${target.includes("?") ? "&" : "?"}next=${next}`);
      }
      throw new Error("sign in required");
    }
    return session;
  }

  return {
    config,
    requireSession,
    skills: Object.fromEntries(
      (config.skills || []).map((skill) => [
        skill.name,
        async (args = {}, opts = {}) => {
          if (opts.requireSession || config.auth?.invokeRequiresSession) {
            await requireSession();
          }
          const payload = await requestJson(
            `${config.endpoints.invoke}/${encodeURIComponent(skill.name)}`,
            {
              method: "POST",
              headers: { "content-type": "application/json", ...(opts.headers || {}) },
              body: JSON.stringify({ arguments: args, ...opts.body }),
            },
          );
          return opts.rawResponse ? payload : unwrapInvokeResponse(payload);
        },
      ]),
    ),
    async call(skill, args = {}, opts = {}) {
      if (opts.requireSession || config.auth?.invokeRequiresSession) {
        await requireSession();
      }
      const payload = await requestJson(
        `${config.endpoints.invoke}/${encodeURIComponent(skill)}`,
        {
          method: "POST",
          headers: { "content-type": "application/json", ...(opts.headers || {}) },
          body: JSON.stringify({ arguments: args, ...opts.body }),
        },
      );
      return opts.rawResponse ? payload : unwrapInvokeResponse(payload);
    },
    async callEnvelope(skill, args = {}, opts = {}) {
      return this.call(skill, args, { ...opts, rawResponse: true });
    },
    card() {
      return requestJson(config.endpoints.agentCard);
    },
    session() {
      return requestJson(config.endpoints.session);
    },
  };
}

if (typeof window !== "undefined") {
  window.A2ACloud = { createA2AClient, loadA2AConfig, unwrapInvokeResponse };
}
""".lstrip()
