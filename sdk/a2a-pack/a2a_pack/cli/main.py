"""``a2a`` CLI: scaffold, validate, build, deploy."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.markup import escape as escape_markup
from rich.panel import Panel

from . import credentials
from .api_client import ApiError, ControlPlaneClient
from .chat_harness import (
    container_backed_resources,
    normalize_local_llm_env,
    write_chat_compose,
)
from .loader import load_agent_class
from .oauth_login import (
    DEFAULT_OAUTH_CLIENT_ID,
    DEFAULT_OAUTH_ISSUER,
    DEFAULT_OAUTH_SCOPE,
    DEFAULT_REDIRECT_PORT,
    login_with_access_token,
    login_with_browser,
    refresh_credentials_if_needed,
)
from .receipts_cli import receipt_app
from ..frontend import (
    SERVER_RENDERED_FRONTEND_KIND,
    export_frontend_env,
    load_frontend_config,
    packed_frontend_from_env,
    resolve_packed_frontend,
)
from ..openapi import agent_dsl_openapi_spec
from ..dsl import AgentDsl, compile_agent_to_dsl
from ..runtime import LLMProvisioning, apply_project_manifest
from .local import (
    ensure_local_workspace,
    load_env_file,
    load_local_project,
    run_preflight_sync,
)
from . import platform
from .manifests import NAMESPACE, render_manifests

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build, package, and deploy A2A agents.",
)
frontend_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build and inspect packed frontend apps.",
)
app.add_typer(frontend_app, name="frontend")
auth_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Manage imported agent auth.",
)
app.add_typer(auth_app, name="auth")
openapi_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Generate editable agents from OpenAPI specs.",
)
app.add_typer(openapi_app, name="openapi")
# Signed execution receipts: `a2a receipt show|verify|list` (see receipts_cli).
app.add_typer(receipt_app, name="receipt")
console = Console()

DEFAULT_AGENT_BASE_IMAGE = "python:3.11-slim"
SDK_PYPI_PACKAGE = "a2a_pack"
PETSTORE_OPENAPI_URL = "https://petstore3.swagger.io/api/v3/openapi.json"
PROJECT_EXCLUDED_DIRS = {
    "__pycache__",
    ".venv",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".next",
    ".turbo",
    ".vercel",
    "dist",
    "build",
    ".gitea",
    "deploy",
    ".a2a",
    ".claude",  # dev-time coding-agent reference/skills; not needed in the prod image
}


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _fail(msg: str, code: int = 1) -> None:
    console.print(f"[bold red]error:[/] {msg}")
    raise typer.Exit(code)


def _api_error_message(exc: ApiError) -> str:
    return str(exc)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        _fail(f"missing {path.name} (run `a2a init NAME` first)")
    return yaml.safe_load(path.read_text()) or {}


def _read_yaml_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _render_template(template: str, /, **vars: Any) -> str:
    text = (
        resources.files("a2a_pack.cli.templates")
        .joinpath(template)
        .read_text(encoding="utf-8")
    )
    # tiny mustache-style substitution; avoids pulling Jinja just for {{ x }}
    for k, v in vars.items():
        text = text.replace("{{ " + k + " }}", str(v))
    return text


def _slug_to_class(slug: str) -> str:
    parts = re.split(r"[-_\s]+", slug.strip())
    return "".join(p[:1].upper() + p[1:].lower() for p in parts if p) or "MyAgent"


def _git_short_sha(project: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _sdk_source_dir() -> Path:
    """Locate the on-disk a2a_pack source for inclusion in the build context."""
    pkg_dir = Path(__file__).resolve().parents[1]  # .../a2a_pack
    project_root = pkg_dir.parent  # .../apps/a2a
    if not (project_root / "pyproject.toml").exists():
        _fail(f"could not find a2a-pack source root from {pkg_dir}")
    return project_root


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    if cmd and cmd[0] == "docker" and "env" not in kwargs:
        docker_desktop_bin = Path("/Applications/Docker.app/Contents/Resources/bin")
        if docker_desktop_bin.is_dir():
            env = os.environ.copy()
            env["PATH"] = f"{docker_desktop_bin}:{env.get('PATH', '')}"
            kwargs["env"] = env
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def _clean_init_auth(raw: Any) -> str:
    value = str(raw or "inherit").strip().lower()
    if value not in {"inherit", "platform", "public"}:
        _fail("--auth must be one of: inherit, platform, public")
    return value


def _auth_template_type(auth: str) -> str:
    return "PlatformUserAuth" if auth == "platform" else "NoAuth"


def _clean_frontend_mode(raw: Any) -> str:
    value = str(raw or "static").strip().lower()
    if value in {"static", "static-spa", "spa"}:
        return "static"
    if value in {"server-rendered", "server", "ssr"}:
        return SERVER_RENDERED_FRONTEND_KIND
    _fail("--frontend-mode must be static or server-rendered")
    return "static"


def _clean_init_language(raw: Any) -> str:
    value = str(raw or "python").strip().lower()
    if value in {"ts", "node"}:
        value = "typescript"
    if value in {"js", "nodejs"}:
        value = "javascript"
    if value not in {"python", "typescript", "javascript", "go", "rust"}:
        _fail("--language must be one of: python, typescript, javascript, go, rust")
    return value


# --------------------------------------------------------------------------- #
# single-skill language disclosure                                            #
# --------------------------------------------------------------------------- #
#
# The Go and Rust sidecar SDKs are demos of the worker protocol, not general
# agent runtimes. Both hardcode the two-number `sum` contract end to end:
#
#   go/a2apack/a2apack.go     `type Agent interface { Definition(); Sum(...) }`
#                             ServeAgent registers only "/_a2a/invoke/sum"
#                             agentDSL emits a fixed one-entry "skills" array
#   rust/a2a-pack-rs/src/lib.rs
#                             `trait A2AAgent { fn definition(); fn sum(); }`
#                             serve_agent 404s anything that is not
#                             "POST /_a2a/invoke/sum "
#                             agent_dsl is a format! string with one skill
#
# A developer can therefore write a second handler and deploy it, and it will
# never be routed or advertised. The two languages differ in *when* they say so,
# and the disclosure has to be honest about that (verified by building both):
#
#   Go    an extra method on the agent type compiles clean (`go build ./...`
#         succeeds), deploys, and `go run . compile` still emits skills ["sum"].
#         There is no failure signal at all.
#   Rust  a second `fn` inside `impl A2AAgent for ...` is a hard compile error,
#         E0407 "method `x` is not a member of trait `A2AAgent`" — the scaffold
#         ships exactly that one impl block, so this is the path a developer
#         actually takes. Moved to a separate inherent `impl` block it compiles
#         (dead_code warning only) and is then unreachable exactly like Go's.
#
# This is the single source of truth for saying so; the scaffold README template
# and the generated docs pages (web/apps/docs/scripts/gen.py) both render from
# it, and tests/test_cli_init.py pins both the wording and the SDK behaviour it
# describes.
SINGLE_SKILL_LANGUAGES: tuple[str, ...] = ("go", "rust")

SINGLE_SKILL_HEADLINE = "Go and Rust support exactly one skill."

SINGLE_SKILL_PARAGRAPHS: tuple[str, ...] = (
    "The Go and Rust SDKs implement the demo contract only: a single skill named "
    "`sum` that adds two numbers. The worker serves just "
    "`POST /_a2a/invoke/sum`, and the `.a2a/agent.dsl.json` they compile always "
    "declares that one skill.",
    "A second skill is never routed and never advertised on the agent card. In Go "
    "the extra method compiles and deploys with no error anywhere, and is then "
    "simply unreachable. In Rust a second method inside the `impl A2AAgent` block "
    "does not compile at all (`E0407`); moved to a separate inherent `impl` block "
    "it compiles and is then unreachable just like Go's. Treat these two SDKs as a "
    "proof-of-concept for the sidecar worker protocol.",
    "For an agent with more than one tool, use `a2a init --language python` or "
    "`a2a init --language typescript`.",
)


def single_skill_notice_markdown() -> str:
    """The disclosure as Markdown, for READMEs and generated docs pages."""
    return "\n\n".join((f"**{SINGLE_SKILL_HEADLINE}**", *SINGLE_SKILL_PARAGRAPHS))


def single_skill_notice_text() -> str:
    """The same disclosure as plain terminal text (no Markdown ticks).

    Deliberately *unwrapped*: Rich owns the wrapping so the panel reflows to the
    real console width. Pre-wrapping here at a fixed column left orphaned
    fragments on their own lines under 80 columns, because Rich re-wraps the
    already-wrapped lines.
    """
    blocks = [
        block.replace("`", "")
        for block in (SINGLE_SKILL_HEADLINE, *SINGLE_SKILL_PARAGRAPHS)
    ]
    return "\n\n".join(blocks)


def _frontend_block(
    kind: str | None,
    *,
    auth: str = "inherit",
    mode: str = "static",
) -> str:
    if not kind:
        return ""
    normalized = kind.strip().lower()
    frontend_mode = _clean_frontend_mode(mode)
    if normalized == "static":
        if frontend_mode == SERVER_RENDERED_FRONTEND_KIND:
            _fail("--frontend-mode server-rendered is only supported with --frontend nextjs")
        return (
            "\nfrontend:\n"
            "  path: frontend\n"
            "  dist: dist\n"
            "  mount: /\n"
            f"  auth: {auth}\n"
        )
    if normalized == "react":
        if frontend_mode == SERVER_RENDERED_FRONTEND_KIND:
            _fail("--frontend-mode server-rendered is only supported with --frontend nextjs")
        return (
            "\nfrontend:\n"
            "  path: frontend\n"
            "  build: npm run build\n"
            "  dist: dist\n"
            "  mount: /\n"
            f"  auth: {auth}\n"
        )
    if normalized == "nextjs":
        if frontend_mode == SERVER_RENDERED_FRONTEND_KIND:
            return (
                "\nfrontend:\n"
                "  type: server-rendered\n"
                "  framework: nextjs\n"
                "  path: frontend\n"
                "  build: npm run build\n"
                "  start: node server.js\n"
                "  port: 3000\n"
                "  mount: /\n"
                f"  auth: {auth}\n"
            )
        return (
            "\nfrontend:\n"
            "  type: static-spa\n"
            "  path: frontend\n"
            "  build: npm run build\n"
            "  dist: out\n"
            "  mount: /\n"
            f"  auth: {auth}\n"
        )
    _fail("--frontend must be 'static', 'react', or 'nextjs'")
    return ""


def _copy_resource_tree(package: str, source: str, target: Path) -> None:
    root = resources.files(package).joinpath(source)
    if not root.is_dir():
        _fail(f"missing packaged template resource: {package}/{source}")
    target.mkdir(parents=True, exist_ok=True)
    for item in root.iterdir():
        dest = target / item.name
        if item.is_dir():
            _copy_resource_tree(package, f"{source}/{item.name}", dest)
        else:
            dest.write_bytes(item.read_bytes())


def _frontend_gitignore_block(kind: str | None, *, mode: str = "static") -> str:
    """Un-ignore a packed frontend's dist when the scaffold checks one in.

    The generic ``dist/`` rule is right for built output, but `--frontend
    static` writes ``frontend/dist/index.html`` as *source* — and `_make_tarball`
    ships it — so ignoring it would silently drop the UI from the repo.
    """
    if not kind or kind.strip().lower() != "static":
        return ""
    return "\n# Scaffolded static frontend: this dist is source, not build output.\n!frontend/dist/\n"


def _frontend_files(
    kind: str | None,
    name: str,
    *,
    mode: str = "static",
) -> dict[str, str]:
    if not kind:
        return {}
    normalized = kind.strip().lower()
    frontend_mode = _clean_frontend_mode(mode)
    if normalized == "static":
        if frontend_mode == SERVER_RENDERED_FRONTEND_KIND:
            _fail("--frontend-mode server-rendered is only supported with --frontend nextjs")
        return {
            "frontend/dist/index.html": _render_template(
                "frontend/static-index.html.tmpl",
                name=name,
            ),
        }
    if normalized == "react":
        if frontend_mode == SERVER_RENDERED_FRONTEND_KIND:
            _fail("--frontend-mode server-rendered is only supported with --frontend nextjs")
        return {
            "frontend/package.json": _render_template(
                "frontend/react-package.json.tmpl",
                name=name,
            ),
            "frontend/index.html": _render_template(
                "frontend/react-index.html.tmpl",
                name=name,
            ),
            "frontend/vite.config.js": _render_template(
                "frontend/react-vite.config.js.tmpl",
            ),
            "frontend/src/main.jsx": _render_template(
                "frontend/react-main.jsx.tmpl",
                name=name,
            ),
            "frontend/src/App.jsx": _render_template(
                "frontend/react-app.jsx.tmpl",
                name=name,
            ),
            "frontend/src/a2a.js": _render_template(
                "frontend/react-a2a.js.tmpl",
            ),
            "frontend/src/style.css": _render_template(
                "frontend/react-style.css.tmpl",
                name=name,
            ),
        }
    if normalized == "nextjs":
        next_config = (
            '  output: "standalone",'
            if frontend_mode == SERVER_RENDERED_FRONTEND_KIND
            else '  output: "export",\n  trailingSlash: true,'
        )
        next_build = (
            "next build && node prepare-standalone.mjs"
            if frontend_mode == SERVER_RENDERED_FRONTEND_KIND
            else "next build"
        )
        next_start = (
            "node start-standalone.mjs"
            if frontend_mode == SERVER_RENDERED_FRONTEND_KIND
            else "next start"
        )
        files = {
            "frontend/package.json": _render_template(
                "frontend/next-package.json.tmpl",
                name=name,
                next_build=next_build,
                next_start=next_start,
            ),
            "frontend/next.config.mjs": _render_template(
                "frontend/next-config.mjs.tmpl",
                next_config=next_config,
            ),
            "frontend/jsconfig.json": _render_template(
                "frontend/next-jsconfig.json.tmpl",
            ),
            "frontend/app/layout.jsx": _render_template(
                "frontend/next-layout.jsx.tmpl",
                name=name,
            ),
            "frontend/app/page.jsx": _render_template(
                "frontend/next-page.jsx.tmpl",
                name=name,
            ),
            "frontend/app/globals.css": _render_template(
                "frontend/next-globals.css.tmpl",
                name=name,
            ),
            "frontend/public/.gitkeep": "",
        }
        if frontend_mode == SERVER_RENDERED_FRONTEND_KIND:
            files["frontend/prepare-standalone.mjs"] = _render_template(
                "frontend/next-prepare-standalone.mjs.tmpl",
            )
            files["frontend/start-standalone.mjs"] = _render_template(
                "frontend/next-start-standalone.mjs.tmpl",
            )
        return files
    _fail("--frontend must be 'static', 'react', or 'nextjs'")
    return {}


def _docker_env(name: str, value: Any) -> str:
    return f"ENV {name}={json.dumps(str(value))}"


def _frontend_dockerfile_blocks(cfg: dict[str, Any]) -> tuple[str, str]:
    """Return ``(stage, runtime)`` Dockerfile snippets for local builds."""

    try:
        frontend = load_frontend_config(Path("."), cfg)
    except ValueError:
        return "", ""
    if frontend is None:
        return "", ""
    if frontend.is_server_rendered:
        if frontend.framework != "nextjs":
            return "", ""
        server_dir = "/app/.a2a/frontend-server"
        proxy_url = f"http://127.0.0.1:{frontend.port}"
        runtime = "\n".join(
            [
                "COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node",
                f"COPY --from=frontend-build /frontend/.next/standalone {server_dir}",
                f"COPY --from=frontend-build /frontend/.next/static {server_dir}/.next/static",
                f"COPY --from=frontend-build /frontend/public {server_dir}/public",
                _docker_env("A2A_FRONTEND_KIND", "server-rendered"),
                _docker_env("A2A_FRONTEND_FRAMEWORK", frontend.framework or ""),
                _docker_env("A2A_FRONTEND_MOUNT", frontend.mount),
                _docker_env("A2A_FRONTEND_AUTH", frontend.auth),
                _docker_env("A2A_FRONTEND_DOCS_URL", frontend.docs_url),
                _docker_env("A2A_FRONTEND_PROXY_URL", proxy_url),
                _docker_env("A2A_FRONTEND_START", frontend.start or "node server.js"),
                _docker_env("A2A_FRONTEND_WORKDIR", server_dir),
                _docker_env("A2A_FRONTEND_PORT", frontend.port),
            ]
        )
        stage = (
            "FROM node:20-bookworm-slim AS frontend-build\n"
            "WORKDIR /frontend\n"
            f"COPY {frontend.path}/ ./\n"
            "RUN if [ -f package-lock.json ]; then npm ci; "
            "elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; "
            "elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; "
            "else npm install; fi\n"
            f"RUN {frontend.build or 'npm run build'}\n\n"
        )
        return stage, f"\n{runtime}\n"
    dist_env = "/app/.a2a/frontend"
    runtime = "\n".join(
        [
            _docker_env("A2A_FRONTEND_DIST", dist_env),
            _docker_env("A2A_FRONTEND_MOUNT", frontend.mount),
            _docker_env("A2A_FRONTEND_AUTH", frontend.auth),
            _docker_env("A2A_FRONTEND_DOCS_URL", frontend.docs_url),
        ]
    )
    if frontend.build:
        stage = (
            "FROM node:20-bookworm-slim AS frontend-build\n"
            "WORKDIR /frontend\n"
            f"COPY {frontend.path}/ ./\n"
            "RUN if [ -f package-lock.json ]; then npm ci; "
            "elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; "
            "elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; "
            "else npm install; fi\n"
            f"RUN {frontend.build}\n\n"
        )
        source_dist = f"{frontend.path}/{frontend.dist}".strip("/")
        runtime = (
            f"COPY --from=frontend-build /frontend/{frontend.dist} {source_dist}\n"
            f"COPY --from=frontend-build /frontend/{frontend.dist} {dist_env}\n"
            f"{runtime}"
        )
        return stage, f"\n{runtime}\n"
    source_dist = f"{frontend.path}/{frontend.dist}".strip("/")
    runtime = f"COPY {source_dist}/ {dist_env}/\n{runtime}"
    return "", f"\n{runtime}\n"


# --------------------------------------------------------------------------- #
# auth                                                                        #
# --------------------------------------------------------------------------- #


def _client(api: str | None = None) -> ControlPlaneClient:
    api_url = credentials.resolve_api_url(api)
    creds = credentials.load()
    if creds is not None:
        try:
            creds = refresh_credentials_if_needed(creds)
        except RuntimeError as exc:
            _fail(str(exc))
    token = creds.token if creds is not None else None

    def refresh_token() -> str | None:
        latest = credentials.load()
        if latest is None:
            return None
        try:
            refreshed = refresh_credentials_if_needed(latest, force=True)
        except RuntimeError as exc:
            _fail(str(exc))
        return refreshed.token

    return ControlPlaneClient(api_url, token=token, refresh_token=refresh_token)


@app.command()
def signup(
    api: str = typer.Option(credentials.DEFAULT_API_URL, "--api"),
    issuer: str = typer.Option(DEFAULT_OAUTH_ISSUER, "--issuer"),
    client_id: str = typer.Option(DEFAULT_OAUTH_CLIENT_ID, "--client-id"),
    scope: str = typer.Option(DEFAULT_OAUTH_SCOPE, "--scope"),
    port: int = typer.Option(DEFAULT_REDIRECT_PORT, "--port"),
    token: str | None = typer.Option(None, "--token", help="Use an existing Keycloak access token"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the login URL"),
) -> None:
    """Create or sign into a Keycloak account and cache OAuth tokens."""
    _keycloak_login(
        api=api,
        issuer=issuer,
        client_id=client_id,
        scope=scope,
        port=port,
        token=token,
        open_browser=open_browser,
        verb="signed in",
    )


@app.command()
def login(
    api: str = typer.Option(credentials.DEFAULT_API_URL, "--api"),
    issuer: str = typer.Option(DEFAULT_OAUTH_ISSUER, "--issuer"),
    client_id: str = typer.Option(DEFAULT_OAUTH_CLIENT_ID, "--client-id"),
    scope: str = typer.Option(DEFAULT_OAUTH_SCOPE, "--scope"),
    port: int = typer.Option(DEFAULT_REDIRECT_PORT, "--port"),
    token: str | None = typer.Option(None, "--token", help="Use an existing Keycloak access token"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the login URL"),
) -> None:
    """Authenticate with Keycloak and cache OAuth tokens."""
    _keycloak_login(
        api=api,
        issuer=issuer,
        client_id=client_id,
        scope=scope,
        port=port,
        token=token,
        open_browser=open_browser,
        verb="logged in",
    )


def _keycloak_login(
    *,
    api: str,
    issuer: str,
    client_id: str,
    scope: str,
    port: int,
    token: str | None,
    open_browser: bool,
    verb: str,
) -> None:
    api_url = credentials.resolve_api_url(api)
    try:
        if token:
            creds = login_with_access_token(
                token,
                api_url=api_url,
                issuer=issuer,
                client_id=client_id,
                scope=scope,
            )
        else:
            creds = login_with_browser(
                api_url=api_url,
                issuer=issuer,
                client_id=client_id,
                scope=scope,
                port=port,
                open_browser=open_browser,
                on_authorization_url=lambda url: console.print(
                    f"Open this URL to sign in with Keycloak:\n{url}"
                ),
            )
    except (ApiError, RuntimeError, TimeoutError) as exc:
        _fail(str(exc))
    console.print(f"[green]{verb}[/] as [cyan]{creds.email}[/] @ {api_url}")


@app.command()
def logout() -> None:
    """Forget the cached JWT."""
    cleared = credentials.clear()
    console.print("[green]logged out[/]" if cleared else "(not logged in)")


@app.command()
def whoami() -> None:
    """Show the currently logged-in user."""
    creds = credentials.load()
    if creds is None:
        _fail("not logged in (run `a2a login` or `a2a signup`)")
    try:
        me = _client().me()
    except ApiError as exc:
        _fail(str(exc))
    console.print(f"[cyan]{me['email']}[/]  ({creds.api_url})")


@app.command(name="agents")
def list_agents(
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """List agents visible to the current user."""
    try:
        rows = _client(api).list_agents()
    except ApiError as exc:
        _fail(str(exc))
    if not rows:
        console.print("(no agents)")
        return
    for r in rows:
        url = r.get("url") or "-"
        console.print(
            f"  [cyan]{r['name']}[/]  v{r['version']}  [{r['status']}]  {url}"
        )


@app.command(name="import")
def import_agent(
    url: str = typer.Argument(..., help="Base URL of an existing A2A agent"),
    name: str | None = typer.Option(None, "--name", "-n", help="Registry slug to use"),
    public: bool = typer.Option(False, "--public/--private"),
    auth_bearer: str | None = typer.Option(
        None,
        "--auth-bearer",
        help="Bearer token for the imported agent",
    ),
    auth_api_key: str | None = typer.Option(
        None,
        "--auth-api-key",
        help="API key value for the imported agent",
    ),
    auth_name: str | None = typer.Option(
        None,
        "--auth-name",
        help="API-key header or query parameter name",
    ),
    auth_location: str = typer.Option(
        "header",
        "--auth-location",
        help="API-key location: header or query",
    ),
    auth_scheme: str = typer.Option(
        "Bearer",
        "--auth-scheme",
        help="HTTP Authorization scheme",
    ),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Import an already-running A2A agent into the registry."""
    auth = _import_auth_payload(
        auth_bearer=auth_bearer,
        auth_api_key=auth_api_key,
        auth_name=auth_name,
        auth_location=auth_location,
        auth_scheme=auth_scheme,
    )
    try:
        out = _client(api).import_agent(url=url, name=name, public=public, auth=auth)
    except ApiError as exc:
        _fail(str(exc))
    summary = {
        "agent": out["name"],
        "status": out["status"],
        "url": out.get("url"),
        "mcp": out.get("mcp"),
        "card_hash": out.get("card_hash"),
    }
    console.print(
        Panel.fit(
            json.dumps(summary, indent=2),
            title="[bold green]imported[/]",
        )
    )


def _import_auth_payload(
    *,
    auth_bearer: str | None,
    auth_api_key: str | None,
    auth_name: str | None,
    auth_location: str,
    auth_scheme: str,
) -> dict[str, str] | None:
    if auth_bearer and auth_api_key:
        _fail("use either --auth-bearer or --auth-api-key, not both")
    if auth_bearer:
        scheme = auth_scheme.strip() or "Bearer"
        return {"type": "bearer", "value": auth_bearer, "scheme": scheme}
    if auth_api_key:
        location = auth_location.strip().lower()
        if location not in {"header", "query"}:
            _fail("--auth-location must be 'header' or 'query'")
        name = (auth_name or "").strip()
        if not name:
            _fail("--auth-name is required with --auth-api-key")
        return {
            "type": "api_key",
            "value": auth_api_key,
            "location": location,
            "name": name,
        }
    if auth_name or auth_location.strip().lower() != "header" or auth_scheme.strip() != "Bearer":
        _fail("auth options require --auth-bearer or --auth-api-key")
    return None


@openapi_app.command(name="preview")
def preview_openapi(
    url: str = typer.Argument(PETSTORE_OPENAPI_URL, help="OpenAPI JSON/YAML URL"),
    name: str | None = typer.Option(None, "--name", "-n", help="Registry slug to generate"),
    description: str | None = typer.Option(None, "--description", "-d"),
    base_url: str | None = typer.Option(None, "--base-url", help="Override the API server URL"),
    # Preview creates nothing, so this value is inert here; it mirrors
    # `a2a openapi generate` so the preview describes the run you would get.
    public: bool | None = typer.Option(
        None,
        "--public/--private",
        help=(
            "Listing the matching `a2a openapi generate` would use. Generated "
            "agents start unlisted unless you pass --public."
        ),
    ),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Preview generated tools, setup fields, and source files."""
    try:
        out = _client(api).preview_openapi_agent(
            url=url,
            name=name,
            description=description,
            public=bool(public),
            base_url=base_url,
        )
    except ApiError as exc:
        _fail(str(exc))
    console.print_json(json.dumps(out, indent=2))


@openapi_app.command(name="generate")
def generate_openapi(
    url: str = typer.Argument(PETSTORE_OPENAPI_URL, help="OpenAPI JSON/YAML URL"),
    name: str | None = typer.Option(None, "--name", "-n", help="Registry slug to generate"),
    description: str | None = typer.Option(None, "--description", "-d"),
    base_url: str | None = typer.Option(None, "--base-url", help="Override the API server URL"),
    # `POST /v1/agents/from-openapi` writes this straight onto `agents.public`,
    # and the CLI never sends `refresh_existing`, so this command only ever
    # creates agents (an existing name 409s). Creating one is not consent to
    # publish it, so the default is unlisted.
    public: bool | None = typer.Option(
        None,
        "--public/--private",
        help=(
            "List the generated agent in the platform's public registry. "
            "Generated agents start unlisted."
        ),
    ),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Generate an editable A2APack source repo and start deployment."""
    is_public = bool(public)
    try:
        out = _client(api).from_openapi(
            url=url,
            name=name,
            description=description,
            public=is_public,
            base_url=base_url,
        )
    except ApiError as exc:
        _fail(str(exc))
    summary = {
        "agent": out["name"],
        "version": out["version"],
        "status": out["status"],
        "repo_url": out.get("repo_url"),
        "url": out.get("expected_url"),
        "deployment_id": out.get("deployment_id"),
        "operations": out.get("preview", {}).get("operation_count"),
    }
    console.print(
        Panel.fit(
            json.dumps(summary, indent=2),
            title="[bold green]generated[/]",
        )
    )
    _print_listing_outcome(
        is_public,
        "flag" if public is not None else "default",
        list_cmd="a2a openapi generate --public",
        unlist_cmd="a2a openapi generate --private",
    )


@auth_app.command(name="status")
def auth_status(
    agent: str = typer.Argument(..., help="Imported agent name"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Show detected auth requirements and configured connections."""
    try:
        out = _client(api).get_agent_auth(agent)
    except ApiError as exc:
        _fail(str(exc))
    console.print(Panel.fit(json.dumps(out, indent=2, default=str), title="[cyan]auth[/]"))


@auth_app.command(name="bearer")
def auth_bearer(
    agent: str = typer.Argument(..., help="Imported agent name"),
    token: str = typer.Option(..., "--token", prompt=True, hide_input=True),
    scheme_name: str | None = typer.Option(None, "--scheme-name"),
    scheme: str = typer.Option("Bearer", "--scheme"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Connect HTTP/Bearer auth for an imported agent."""
    body = {
        "scheme_type": "http",
        "scheme_name": scheme_name,
        "token": token,
        "scheme": scheme,
    }
    _print_auth_connection(agent, body, api)


@auth_app.command(name="api-key")
def auth_api_key(
    agent: str = typer.Argument(..., help="Imported agent name"),
    value: str = typer.Option(..., "--value", prompt=True, hide_input=True),
    name: str = typer.Option(..., "--name", help="Header or query parameter name"),
    location: str = typer.Option("header", "--location", help="header or query"),
    scheme_name: str | None = typer.Option(None, "--scheme-name"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Connect API-key auth for an imported agent."""
    body = {
        "scheme_type": "api_key",
        "scheme_name": scheme_name,
        "value": value,
        "name": name,
        "location": location,
    }
    _print_auth_connection(agent, body, api)


@auth_app.command(name="oauth-token")
def auth_oauth_token(
    agent: str = typer.Argument(..., help="Imported agent name"),
    access_token: str = typer.Option(..., "--access-token", prompt=True, hide_input=True),
    scheme_type: str = typer.Option("oauth2", "--scheme-type", help="oauth2 or oidc"),
    scheme_name: str | None = typer.Option(None, "--scheme-name"),
    refresh_token: str | None = typer.Option(None, "--refresh-token"),
    token_url: str | None = typer.Option(None, "--token-url"),
    client_id: str | None = typer.Option(None, "--client-id"),
    client_secret: str | None = typer.Option(None, "--client-secret"),
    expires_in: int | None = typer.Option(None, "--expires-in"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Connect OAuth/OIDC with an existing access token and optional refresh config."""
    body = {
        "scheme_type": scheme_type,
        "scheme_name": scheme_name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_url": token_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "expires_in": expires_in,
    }
    _print_auth_connection(agent, body, api)


@auth_app.command(name="mtls")
def auth_mtls(
    agent: str = typer.Argument(..., help="Imported agent name"),
    cert: Path = typer.Option(..., "--cert", exists=True, readable=True),
    key: Path = typer.Option(..., "--key", exists=True, readable=True),
    ca: Path | None = typer.Option(None, "--ca", exists=True, readable=True),
    scheme_name: str | None = typer.Option(None, "--scheme-name"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Connect mTLS client certificate auth for an imported agent."""
    body = {
        "scheme_type": "mtls",
        "scheme_name": scheme_name,
        "cert_pem": cert.read_text(encoding="utf-8"),
        "key_pem": key.read_text(encoding="utf-8"),
        "ca_pem": ca.read_text(encoding="utf-8") if ca else None,
    }
    _print_auth_connection(agent, body, api)


@auth_app.command(name="delete")
def auth_delete(
    agent: str = typer.Argument(..., help="Imported agent name"),
    connection_id: int = typer.Argument(..., help="Auth connection id"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Remove an imported-agent auth connection."""
    try:
        _client(api).delete_agent_auth(agent, connection_id)
    except ApiError as exc:
        _fail(str(exc))
    console.print("[green]deleted[/]")


def _print_auth_connection(agent: str, body: dict[str, Any], api: str | None) -> None:
    clean = {k: v for k, v in body.items() if v is not None}
    try:
        out = _client(api).connect_agent_auth(agent, clean)
    except ApiError as exc:
        _fail(str(exc))
    console.print(Panel.fit(json.dumps(out, indent=2, default=str), title="[green]connected[/]"))


# --------------------------------------------------------------------------- #
# init                                                                        #
# --------------------------------------------------------------------------- #


@app.command()
def init(
    name: str = typer.Argument(..., help="Agent / project slug, e.g. research-agent"),
    description: str = typer.Option("A new A2A agent", "--description", "-d"),
    target: Path = typer.Option(Path("."), "--target", "-t", help="Parent dir for the new project"),
    language: str = typer.Option(
        "python",
        "--language",
        "-l",
        help=(
            "Template language. python and typescript/javascript are the "
            "full multi-tool SDKs. go and rust are single-skill demos of the "
            "sidecar worker protocol: they route only the built-in 'sum' skill."
        ),
    ),
    frontend: str | None = typer.Option(
        None,
        "--frontend",
        help="Add a packed frontend scaffold: static, react, or nextjs",
    ),
    frontend_mode: str = typer.Option(
        "static",
        "--frontend-mode",
        help="Frontend deployment mode: static or server-rendered",
    ),
    auth: str = typer.Option(
        "inherit",
        "--auth",
        help="App auth mode for scaffolded agents/frontends: inherit, platform, or public",
    ),
) -> None:
    """Scaffold a new agent project."""
    project = target / name
    if project.exists():
        _fail(f"{project} already exists")
    project.mkdir(parents=True)
    class_name = _slug_to_class(name)
    frontend_kind = frontend if isinstance(frontend, str) else None
    frontend_mode_clean = _clean_frontend_mode(
        frontend_mode if isinstance(frontend_mode, str) else "static"
    )
    auth_mode = _clean_init_auth(auth if isinstance(auth, str) else "inherit")
    language_mode = _clean_init_language(language if isinstance(language, str) else "python")
    auth_type = _auth_template_type(auth_mode)

    if language_mode == "python":
        files = {
            "README.md": _render_template(
                "README.md.tmpl",
                name=name,
                class_name=class_name,
                description=description,
            ),
            "agent.py": _render_template(
                "agent.py.tmpl",
                name=name,
                class_name=class_name,
                description=description,
                auth_type=auth_type,
            ),
            "a2a.yaml": _render_template(
                "a2a.yaml.tmpl",
                name=name,
                class_name=class_name,
                frontend_block=_frontend_block(
                    frontend_kind,
                    auth=auth_mode,
                    mode=frontend_mode_clean,
                ),
            ),
            "requirements.txt": _render_template("requirements.txt.tmpl"),
            # Reference so coding agents (Claude Code, Codex, Cursor) know how to
            # build on the platform. AGENTS.md is the source of truth; CLAUDE.md
            # imports it; the .claude skill is the invokable playbook.
            "AGENTS.md": _render_template(
                "AGENTS.md.tmpl",
                name=name,
                class_name=class_name,
                description=description,
            ),
            "CLAUDE.md": _render_template(
                "CLAUDE.md.tmpl",
                name=name,
            ),
            ".claude/skills/build-a2a-agent/SKILL.md": _render_template(
                "claude-skill.SKILL.md.tmpl",
            ),
        }
        next_steps = (
            "  python -m pip install -r requirements.txt\n"
            "  a2a dev --local # iterate on this machine\n"
            "  a2a dev         # or sync to a public cloud dev box\n"
            "  a2a test        # run preflight checks\n"
            "  a2a deploy      # ship it"
        )
    elif language_mode in {"typescript", "javascript"}:
        if frontend_kind is not None and frontend_kind.strip().lower() != "nextjs":
            _fail(f"--language {language_mode} currently supports --frontend nextjs")
        if auth_mode != "inherit":
            _fail("--auth is only supported for the python template today")
        is_typescript = language_mode == "typescript"
        template_dir = "typescript" if is_typescript else "javascript"
        entrypoint = "node dist/worker.js" if is_typescript else "node src/worker.js"
        entrypoint_command = (
            '["node", "dist/worker.js"]'
            if is_typescript
            else '["node", "src/worker.js"]'
        )
        files = {
            "README.md": _render_template(
                f"{template_dir}/README.md.tmpl",
                name=name,
                class_name=class_name,
                description=description,
            ),
            "a2a.yaml": _render_template(
                f"{template_dir}/a2a.yaml.tmpl",
                name=name,
                frontend_block=_frontend_block(
                    frontend_kind,
                    auth="public",
                    mode=frontend_mode_clean,
                ),
                entrypoint=entrypoint,
            ),
            "package.json": _render_template(
                f"{template_dir}/package.json.tmpl",
                name=name,
            ),
            f"src/agent.{'ts' if is_typescript else 'js'}": _render_template(
                f"{template_dir}/agent.{'ts' if is_typescript else 'js'}.tmpl",
                name=name,
                class_name=class_name,
                description=description,
                entrypoint_command=entrypoint_command,
            ),
            f"src/worker.{'ts' if is_typescript else 'js'}": _render_template(
                f"{template_dir}/worker.{'ts' if is_typescript else 'js'}.tmpl",
                class_name=class_name,
            ),
        }
        if is_typescript:
            files["tsconfig.json"] = _render_template("typescript/tsconfig.json.tmpl")
        next_steps = (
            "  npm install\n"
            "  npm run compile  # writes .a2a/agent.dsl.json\n"
            "  npm run worker   # run native worker locally\n"
            "  a2a deploy       # ship it"
        )
    elif language_mode in {"go", "rust"}:
        if frontend_kind is not None and frontend_kind.strip().lower() != "nextjs":
            _fail(f"--language {language_mode} currently supports --frontend nextjs")
        if auth_mode != "inherit":
            _fail("--auth is only supported for the python template today")
        files = {
            "README.md": _render_template(
                f"{language_mode}/README.md.tmpl",
                name=name,
                class_name=class_name,
                description=description,
                single_skill_notice=single_skill_notice_markdown(),
            ),
            "a2a.yaml": _render_template(
                f"{language_mode}/a2a.yaml.tmpl",
                name=name,
                frontend_block=_frontend_block(
                    frontend_kind,
                    auth="public",
                    mode=frontend_mode_clean,
                ),
            ),
        }
        if language_mode == "go":
            files["go.mod"] = _render_template("go/go.mod.tmpl", name=name)
            files["main.go"] = _render_template(
                "go/main.go.tmpl",
                name=name,
                class_name=class_name,
                description=description,
            )
            next_steps = (
                "  go run . compile  # writes .a2a/agent.dsl.json\n"
                "  go run . worker   # run native worker locally\n"
                "  a2a deploy        # ship it"
            )
        else:
            files["Cargo.toml"] = _render_template("rust/Cargo.toml.tmpl", name=name)
            files["src/main.rs"] = _render_template(
                "rust/main.rs.tmpl",
                name=name,
                class_name=class_name,
                description=description,
            )
            next_steps = (
                "  cargo run -- compile  # writes .a2a/agent.dsl.json\n"
                "  cargo run -- worker   # run native worker locally\n"
                "  a2a deploy            # ship it"
            )
    else:
        raise AssertionError(f"unsupported language mode: {language_mode}")
    files.update(_frontend_files(frontend_kind, name, mode=frontend_mode_clean))
    # Every scaffold ships one: the README tells developers to put secrets in
    # .env.local, so an un-ignored working tree is a leak waiting to happen.
    files[".gitignore"] = _render_template(
        "gitignore.tmpl",
        frontend_ignore_block=_frontend_gitignore_block(
            frontend_kind, mode=frontend_mode_clean
        ),
    )
    for relpath, content in files.items():
        target_path = project / relpath
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content)
        console.print(f"  [green]+[/] {project}/{relpath}")
    if language_mode in {"typescript", "javascript"}:
        vendor_target = project / "vendor" / "a2a-pack-ts"
        _copy_resource_tree("a2a_pack", "typescript", vendor_target)
        console.print(f"  [green]+[/] {vendor_target}")
    if language_mode == "go":
        vendor_target = project / "third_party" / "a2a-pack-go"
        _copy_resource_tree("a2a_pack", "go", vendor_target)
        console.print(f"  [green]+[/] {vendor_target}")
    if language_mode == "rust":
        vendor_target = project / "third_party" / "a2a-pack-rs"
        _copy_resource_tree("a2a_pack", "rust", vendor_target)
        console.print(f"  [green]+[/] {vendor_target}")

    console.print(
        Panel.fit(
            f"[bold]{name}[/] scaffolded at [cyan]{project}[/]\n\n"
            "next:\n"
            f"  cd {project}\n"
            f"{next_steps}",
            title="ok",
        )
    )
    if language_mode in SINGLE_SKILL_LANGUAGES:
        # Last thing on screen, so it is read before any code is written.
        console.print(
            Panel.fit(
                escape_markup(single_skill_notice_text()),
                title="[yellow]read this first[/]",
                border_style="yellow",
            )
        )


# --------------------------------------------------------------------------- #
# validate / card / run                                                       #
# --------------------------------------------------------------------------- #


@app.command()
def validate(
    project: Path = typer.Option(Path("."), "--project", "-p"),
) -> None:
    """Load the agent and print its Card schema. Exits non-zero on errors."""
    cfg = _read_yaml(project / "a2a.yaml")
    cls = load_agent_class(cfg["entrypoint"], project_dir=project)
    apply_project_manifest(cls, cfg)
    console.print(f"[green]ok[/] {cls.name} v{cls.version} ({len(cls._skills)} tools)")


@app.command()
def card(
    project: Path = typer.Option(Path("."), "--project", "-p"),
) -> None:
    """Print the Agent Card JSON for the project's agent."""
    cfg = _read_yaml(project / "a2a.yaml")
    cls = load_agent_class(cfg["entrypoint"], project_dir=project)
    apply_project_manifest(cls, cfg)
    console.print_json(cls().card().model_dump_json())


@openapi_app.command(name="spec")
def openapi_spec(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Write OpenAPI JSON here instead of stdout",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Optional server URL to include in the OpenAPI servers list",
    ),
    require_bearer: bool | None = typer.Option(
        None,
        "--require-bearer/--public",
        help=(
            "Override transport auth in the generated spec. By default this is "
            "derived from the agent auth_model."
        ),
    ),
) -> None:
    """Generate an OpenAPI spec for this agent's direct tool APIs."""
    _, dsl = _compile_project_dsl(project)
    payload = json.dumps(
        agent_dsl_openapi_spec(
            dsl,
            base_url=base_url,
            require_bearer_auth=require_bearer,
        ),
        indent=2,
    )
    if out is None:
        console.print_json(payload)
        return
    out_path = out if out.is_absolute() else project / out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n", encoding="utf-8")
    console.print(f"[green]wrote[/] OpenAPI spec -> [cyan]{out_path}[/]")


def _filter_openapi_spec_skills(spec: dict[str, Any], skills: tuple[str, ...]) -> dict[str, Any]:
    selected = tuple(skill.strip() for skill in skills if skill.strip())
    if not selected:
        return spec

    available = {
        path.removeprefix("/invoke/")
        for path in spec.get("paths", {})
        if isinstance(path, str) and path.startswith("/invoke/")
    }
    missing = sorted(set(selected) - available)
    if missing:
        _fail(
            "unknown tool(s): "
            + ", ".join(missing)
            + f". Available: {', '.join(sorted(available)) or 'none'}"
        )

    selected_paths = {f"/invoke/{skill}" for skill in selected}
    filtered = dict(spec)
    filtered["paths"] = {
        path: value
        for path, value in spec.get("paths", {}).items()
        if path in selected_paths
    }
    filtered["x-a2a-exported-skills"] = list(selected)
    return filtered


def _run_hey_openapi_ts(*, input_path: Path, out_path: Path) -> None:
    cmd = [
        "npx",
        "--yes",
        "@hey-api/openapi-ts",
        "-i",
        str(input_path),
        "-o",
        str(out_path),
    ]
    try:
        _run(cmd)
    except FileNotFoundError:
        _fail("npx is required to run @hey-api/openapi-ts. Install Node.js/npm and retry.")
    except subprocess.CalledProcessError as exc:
        _fail(f"@hey-api/openapi-ts failed with exit code {exc.returncode}")


@openapi_app.command(name="client")
def openapi_client(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    out: Path = typer.Option(
        Path("frontend/src/a2a-client"),
        "--out",
        "-o",
        help="Directory where @hey-api/openapi-ts writes the generated client",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Optional server URL to include in the generated OpenAPI spec",
    ),
    skill: list[str] | None = typer.Option(
        None,
        "--skill",
        "-s",
        help="Tool to include. Repeat to export multiple tools. Defaults to all tools.",
    ),
    require_bearer: bool | None = typer.Option(
        None,
        "--require-bearer/--public",
        help=(
            "Override transport auth in the generated spec. By default this is "
            "derived from the agent auth_model."
        ),
    ),
    spec_out: Path | None = typer.Option(
        None,
        "--spec-out",
        help="Also write the intermediate OpenAPI JSON spec to this path after generation",
    ),
) -> None:
    """Generate a TypeScript client for this agent's tool APIs."""
    _, dsl = _compile_project_dsl(project)
    spec = agent_dsl_openapi_spec(
        dsl,
        base_url=base_url,
        require_bearer_auth=require_bearer,
    )
    spec = _filter_openapi_spec_skills(spec, tuple(skill or ()))

    out_path = out if out.is_absolute() else project / out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".openapi.json",
        delete=False,
    )
    try:
        with tmp:
            json.dump(spec, tmp, indent=2)
            tmp.write("\n")
        generator_input = Path(tmp.name)
        _run_hey_openapi_ts(input_path=generator_input, out_path=out_path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if spec_out is not None:
        spec_path = spec_out if spec_out.is_absolute() else project / spec_out
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]wrote[/] OpenAPI spec -> [cyan]{spec_path}[/]")
    console.print(f"[green]wrote[/] TypeScript client -> [cyan]{out_path}[/]")


def _compile_project_dsl(project: Path) -> tuple[dict[str, Any], AgentDsl]:
    cfg = _read_yaml(project / "a2a.yaml")
    language = str(cfg.get("language") or "python").strip().lower()
    if language in {"ts", "node"}:
        language = "typescript"
    if language in {"js", "nodejs"}:
        language = "javascript"
    if language in {"typescript", "javascript", "go", "rust"}:
        return cfg, _compile_external_project_dsl(project, language=language)
    if language != "python":
        _fail(f"unsupported a2a.yaml language: {language}")
    entrypoint = cfg["entrypoint"]
    cls = load_agent_class(entrypoint, project_dir=project)
    apply_project_manifest(cls, cfg)
    dsl = compile_agent_to_dsl(
        cls,
        language="python",
        entrypoint=entrypoint,
        metadata={
            "source": "python-a2a-pack",
            "project_manifest": {
                key: value
                for key, value in cfg.items()
                if key in {"name", "version", "entrypoint", "frontend"}
            },
        },
    )
    return cfg, dsl


def _compile_external_project_dsl(project: Path, *, language: str) -> AgentDsl:
    if language in {"typescript", "javascript"}:
        command = ["npm", "run", "compile"]
        missing_tool = "npm"
    elif language == "go":
        command = ["go", "run", ".", "compile"]
        missing_tool = "go"
    elif language == "rust":
        command = ["cargo", "run", "--quiet", "--", "compile"]
        missing_tool = "cargo"
    else:
        _fail(f"unsupported external Agent DSL language: {language}")
    try:
        subprocess.run(
            command,
            cwd=project,
            check=True,
            text=True,
        )
    except FileNotFoundError as exc:
        _fail(f"{missing_tool} is required to compile {language} Agent DSL")
        raise AssertionError("unreachable") from exc
    except subprocess.CalledProcessError as exc:
        _fail(f"{language} DSL compile failed with exit code {exc.returncode}")
    dsl_path = project / ".a2a" / "agent.dsl.json"
    if not dsl_path.exists():
        _fail(f"{language} compile did not write {dsl_path}")
    try:
        dsl = AgentDsl.model_validate_json(dsl_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _fail(f"invalid compiled Agent DSL: {exc}")
    if dsl.language != language:
        _fail(f"compiled Agent DSL language {dsl.language!r} does not match {language!r}")
    return dsl


@app.command(name="compile")
def compile_dsl(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Write DSL JSON here (defaults to PROJECT/.a2a/agent.dsl.json)",
    ),
    stdout: bool = typer.Option(
        False,
        "--stdout",
        help="Print DSL JSON instead of writing a file",
    ),
) -> None:
    """Compile the project declaration to the sidecar Agent DSL."""
    _, dsl = _compile_project_dsl(project)
    payload = dsl.model_dump_json(indent=2)
    if stdout:
        console.print_json(payload)
        return
    out_path = out or (project / ".a2a" / "agent.dsl.json")
    if not out_path.is_absolute():
        out_path = project / out_path if out is not None else out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload + "\n", encoding="utf-8")
    console.print(f"[green]compiled[/] sidecar DSL -> [cyan]{out_path}[/]")


_LOCAL_LLM_TOOL_HINTS = {"deepagents", "langchain", "openapi", "llm"}


def _local_dev_warnings(agent_cls: type[Any]) -> list[str]:
    warnings: list[str] = []

    missing_runtime = [
        key
        for key in (*agent_cls.required_env, *agent_cls.required_secrets)
        if not os.environ.get(key)
    ]
    if missing_runtime:
        warnings.append(
            "runtime env missing "
            + ", ".join(dict.fromkeys(missing_runtime))
            + " (add to .env.local)"
        )

    setup_fields = tuple(getattr(agent_cls.consumer_setup, "fields", ()) or ())
    missing_setup = [
        field.name
        for field in setup_fields
        if field.required and not os.environ.get(field.name)
    ]
    if missing_setup:
        warnings.append(
            "setup missing "
            + ", ".join(missing_setup)
        )
        warnings.append("setup fix: open dev ui or add to .env.local")

    if _local_dev_likely_needs_llm(agent_cls) and not (
        os.environ.get("AGENT_LLM_KEY") or os.environ.get("A2A_LITELLM_KEY")
    ):
        warnings.append("llm missing AGENT_LLM_KEY or A2A_LITELLM_KEY")
        warnings.append("llm fix: open dev ui or set local ctx.llm env")

    return warnings


def _local_dev_likely_needs_llm(agent_cls: type[Any]) -> bool:
    provisioning = getattr(agent_cls, "llm_provisioning", None)
    if provisioning not in {
        LLMProvisioning.PLATFORM,
        LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED,
        LLMProvisioning.CALLER_PROVIDED,
        LLMProvisioning.AGENT_BYOK,
    }:
        return False
    tools = {str(tool).lower() for tool in getattr(agent_cls, "tools_used", ())}
    return bool(tools.intersection(_LOCAL_LLM_TOOL_HINTS))


def _dev_image_ref(name: str) -> str:
    safe = re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip(".-") or "agent"
    return f"a2a-dev-{safe}:local"


def _stage_default_dockerfile(
    ctx: Path,
    cfg: dict[str, Any],
    *,
    base_image: str = DEFAULT_AGENT_BASE_IMAGE,
) -> None:
    if (ctx / "Dockerfile").exists():
        return
    frontend_stage, frontend_runtime = _frontend_dockerfile_blocks(cfg)
    (ctx / "Dockerfile").write_text(
        _render_template(
            "Dockerfile.tmpl",
            entrypoint=cfg["entrypoint"],
            base_image=base_image,
            frontend_stage=frontend_stage,
            frontend_runtime=frontend_runtime,
        )
    )


def _docker_passthrough_env(agent_cls: type[Any], loaded: dict[str, str]) -> list[str]:
    names = set(loaded)
    names.update(agent_cls.required_env)
    names.update(agent_cls.required_secrets)
    names.update(field.name for field in agent_cls.consumer_setup.fields)
    names.update(
        {
            "A2A_LITELLM_URL",
            "A2A_LITELLM_KEY",
            "A2A_LITELLM_MODEL",
            "AGENT_LLM_URL",
            "AGENT_LLM_KEY",
            "AGENT_LLM_MODEL",
        }
    )
    return sorted(name for name in names if os.environ.get(name) is not None)


def _run_docker_dev(
    *,
    local: Any,
    project: Path,
    env_path: Path,
    workspace_root: Path,
    loaded: dict[str, str],
    host: str,
    port: int,
    reload: bool,
) -> None:
    image = _dev_image_ref(local.agent_cls.name)
    with tempfile.TemporaryDirectory(prefix="a2a-dev-build-") as tmp:
        ctx = Path(tmp)
        _stage_build_context(project, ctx)
        _stage_default_dockerfile(ctx, local.config)
        _run(["docker", "build", "-t", image, str(ctx)])

    try:
        container_env_path = f"/app/{env_path.resolve().relative_to(project.resolve())}"
    except ValueError:
        container_env_path = "/app/.env.local"

    cmd = [
        "docker",
        "run",
        "--rm",
        "-p",
        f"{host}:{port}:8000",
        "-v",
        f"{project.resolve()}:/app",
        "-v",
        f"{workspace_root}:/workspace",
        "-e",
        "A2A_PROJECT_DIR=/app",
        "-e",
        f"A2A_ENTRYPOINT={local.entrypoint}",
        "-e",
        f"A2A_ENV_FILE={container_env_path}",
        "-e",
        "A2A_LOCAL_DEV=1",
        "-e",
        "A2A_LOCAL_WORKSPACE_DIR=/workspace",
    ]
    if env_path.exists():
        cmd.extend(["--env-file", str(env_path.resolve())])
    for name in _docker_passthrough_env(local.agent_cls, loaded):
        cmd.extend(["-e", name])
    cmd.extend(
        [
            image,
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
    )
    if reload:
        cmd.extend(["--reload", "--reload-dir", "/app"])
    _run(cmd)


@app.command()
def run(
    entrypoint: str = typer.Option(..., "--entrypoint", "-e", help="module:Class"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
    project: Path = typer.Option(Path("."), "--project", "-p"),
) -> None:
    """Run the agent's HTTP server locally (used inside the container too)."""
    from ..serve import serve

    cfg = _read_yaml_optional(project / "a2a.yaml")
    cls = load_agent_class(entrypoint, project_dir=project)
    apply_project_manifest(cls, cfg)
    frontend = packed_frontend_from_env() or resolve_packed_frontend(project, cfg)
    export_frontend_env(frontend)
    serve(cls(), host=host, port=port, frontend=frontend)


@app.command(name="sidecar")
def sidecar(
    dsl: Path = typer.Option(
        Path(".a2a/agent.dsl.json"),
        "--dsl",
        help="Compiled Agent DSL JSON path",
    ),
    worker_url: str = typer.Option(
        "http://127.0.0.1:9001",
        "--worker-url",
        help="Native language worker base URL",
    ),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the common sidecar runtime for a compiled Agent DSL."""
    from ..sidecar import serve_sidecar

    serve_sidecar(dsl_path=dsl, worker_url=worker_url, host=host, port=port)


@app.command()
def dev(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(True, "--reload/--no-reload"),
    env_file: Path = typer.Option(Path(".env.local"), "--env-file"),
    workspace: Path | None = typer.Option(None, "--workspace"),
    docker: bool = typer.Option(
        False,
        "--docker/--host-runtime",
        help=(
            "Run dev mode in the agent container image instead of this Python "
            "process. Defaults to the in-process runtime, which needs no Docker."
        ),
    ),
    local: bool = typer.Option(
        False,
        "--local",
        help="Run on this machine instead of a cloud dev box (offline / air-gapped).",
    ),
    agent: str | None = typer.Option(
        None, "--agent", help="Agent name for the cloud dev box (default: a2a.yaml name)."
    ),
    api: str | None = typer.Option(None, "--api", hidden=True),
) -> None:
    """Edit locally, run in the cloud: sync this project to the agent's scale-to-zero
    dev box, hot-reload it there, and serve it on a public URL. Use --local to run
    on this machine instead."""
    # Inside a dev box the agent must run locally — the cloud path is what put us
    # here. Force it so `a2a dev` (bare) can't recurse into a box-inside-a-box.
    if os.environ.get("A2A_DEVBOX_PORT"):
        local = True
    if not local:
        _dev_cloud(project=project, agent=agent, env_file=env_file, api=api)
        return
    _dev_local(
        project=project,
        host=host,
        port=port,
        reload=reload,
        env_file=env_file,
        workspace=workspace,
        docker=docker,
    )


def _dev_local(
    *,
    project: Path,
    host: str,
    port: int,
    reload: bool,
    env_file: Path,
    workspace: Path | None,
    docker: bool,
) -> None:
    """Run the agent on this machine with .env.local, workspace, and hot reload."""
    local = load_local_project(project)
    env_path = env_file if env_file.is_absolute() else project / env_file
    workspace_root = ensure_local_workspace(project, workspace=workspace)
    loaded = load_env_file(env_path)
    credentials.load_local_llm_into_env()
    credentials.load_agent_setup_into_env(
        local.agent_cls.name,
        set(local.agent_cls.required_env)
        | set(local.agent_cls.required_secrets)
        | {field.name for field in local.agent_cls.consumer_setup.fields},
    )
    frontend_cfg = load_frontend_config(project, local.config)
    frontend_runtime = resolve_packed_frontend(project, local.config)
    export_frontend_env(frontend_runtime)
    os.environ["A2A_PROJECT_DIR"] = str(project.resolve())
    os.environ["A2A_ENTRYPOINT"] = local.entrypoint
    os.environ["A2A_ENV_FILE"] = str(env_path.resolve())
    os.environ["A2A_LOCAL_DEV"] = "1"
    os.environ["A2A_LOCAL_WORKSPACE_DIR"] = str(workspace_root)
    warnings = _local_dev_warnings(local.agent_cls)
    # Declared Qdrant/Postgres are stood up by `a2a chat`, not by either dev
    # runtime — say so once rather than letting the agent fail against a
    # backing service that was never started.
    container_resources = container_backed_resources(local.agent_cls)
    if container_resources:
        warnings.append(
            "this project declares "
            + "; ".join(container_resources)
            + " — `a2a dev` runs the agent alone; use `a2a chat` (docker compose) "
            "to run it with those services"
        )
    runtime_label = "docker" if docker else "host"

    console.print(
        Panel.fit(
            "\n".join(
                [
                    f"agent: {local.agent_cls.name} v{local.agent_cls.version}",
                    f"runtime: {runtime_label}",
                    f"url: http://{host}:{port}",
                    f"dev ui: http://{host}:{port}/_dev",
                    f"card: http://{host}:{port}/.well-known/agent-card",
                    f"workspace: {workspace_root}",
                    f"env: {env_path} ({len(loaded)} loaded)",
                    (
                        f"frontend: http://{host}:{port}{frontend_runtime.mount}"
                        if frontend_runtime is not None
                        else (
                            "frontend: declared, build dist first with `a2a frontend build`"
                            if frontend_cfg is not None
                            else "frontend: none"
                        )
                    ),
                    *(
                        ["", *[f"warning: {warning}" for warning in warnings]]
                        if warnings
                        else []
                    ),
                ]
            ),
            title="[bold green]local dev[/]",
        )
    )

    if docker:
        _run_docker_dev(
            local=local,
            project=project,
            env_path=env_path,
            workspace_root=workspace_root,
            loaded=loaded,
            host=host,
            port=port,
            reload=reload,
        )
        return

    import uvicorn

    uvicorn.run(
        "a2a_pack.cli.dev_server:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        reload_dirs=[str(project.resolve())] if reload else None,
        log_level="info",
    )


# The agent's dev server listens here inside the box; the devbox bridge fronts
# it on the public port (see apps/devbox bridge.py + control-plane devbox.py).
_DEVBOX_AGENT_PORT = 8001
# The box's public bridge port. Set inline on the remote dev server so the box's
# own `a2a dev` takes the local path (anti box-in-box recursion), since sshd
# sessions don't inherit the container env that would otherwise carry it.
_DEVBOX_BRIDGE_PORT = 8000
# Never sync these to the box — noise, secrets-in-vcs, or huge.
_DEV_SYNC_EXCLUDES = (
    ".git",
    "__pycache__",
    "*.pyc",
    ".venv",
    "venv",
    "node_modules",
    ".a2a",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
)


def _rsync_up(project: Path, host_alias: str, remote_dir: str) -> None:
    """Mirror the local project into the box over the ssh ProxyCommand alias.
    --delete so a locally removed file disappears on the box too."""
    if not shutil.which("rsync"):
        _fail("a2a dev (cloud) needs `rsync` on PATH; install it or use `a2a dev --local`")
    src = f"{str(project.resolve()).rstrip('/')}/"
    # --temp-dir keeps rsync's dot-temp files out of the synced tree: the box's
    # file-watcher otherwise reloads on the temp file (old code), and the real
    # rename lands inside the restart window where it goes unseen.
    cmd = ["rsync", "-az", "--delete", "--temp-dir=/tmp"]
    for pat in _DEV_SYNC_EXCLUDES:
        cmd += ["--exclude", pat]
    cmd += ["-e", "ssh -o BatchMode=yes", src, f"{host_alias}:{remote_dir}/"]
    subprocess.run(cmd, check=True)


def _project_fingerprint(project: Path) -> dict[str, float]:
    """path -> mtime for every synced file, so a poll loop can tell what
    changed without a native file-watcher dependency."""
    excl_dirs = {e for e in _DEV_SYNC_EXCLUDES if "." not in e[1:] and "*" not in e}
    fp: dict[str, float] = {}
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(project).parts)
        if parts & excl_dirs or path.suffix == ".pyc":
            continue
        try:
            fp[str(path)] = path.stat().st_mtime
        except OSError:
            pass
    return fp


def _dev_cloud(
    *, project: Path, agent: str | None, env_file: Path, api: str | None
) -> None:
    """Sync the project to the agent's cloud dev box, run its hot-reloading dev
    server there, and serve it on the box's public URL. Local edits rsync up on
    save; the box's `uvicorn --reload` picks them up."""
    import time

    if agent is None:
        agent = _infer_agent_name()
    if agent is None:
        _fail("no agent name; pass --agent NAME or run in a project with an a2a.yaml")

    if credentials.load() is None:
        _fail(
            "not logged in — cloud dev needs an account (run `a2a login`).\n"
            "To run on this machine instead: `a2a dev --local`."
        )

    client = _client(api)  # refreshes + persists the token before we ship it up
    key, pub = _ensure_devbox_key(agent)
    host_alias = _write_devbox_ssh_config(agent, key)
    remote_dir = f"/home/dev/{agent}"

    # Throwaway boxes rotate their SSH host key (image rolls / secret regen), so a
    # pinned entry from a previous session trips "REMOTE HOST IDENTIFICATION HAS
    # CHANGED" and StrictHostKeyChecking=accept-new refuses a *changed* key. Drop
    # the stale pin so this session re-pins the current key. Transport is still
    # authenticated by the per-session WSS grant and our ephemeral key.
    (_A2A_SSH_DIR / f"{agent}.known_hosts").unlink(missing_ok=True)

    # Provision (or wake) the box and learn its public host. This also mints the
    # per-session grant that the ProxyCommand rsync/ssh below ride on.
    console.print(f"[dim]waking {agent} dev box (cold start takes a few seconds)...[/]")
    info = client.agent_ssh(
        name=agent, public_key=pub, credentials_json=_devbox_credentials_json()
    )
    box_host = info.get("host")
    if not box_host:
        _fail(
            "this platform doesn't support cloud dev yet (no box host in the API "
            "response — control plane is older than the CLI).\n"
            "Run on this machine instead: `a2a dev --local`."
        )
    public_url = f"https://{box_host}/"

    console.print("[dim]syncing project to the box...[/]")
    # Provisioning a warm box rolls a new revision carrying this session's key;
    # until it's ready, the old pod answers and rejects the key. Retry through
    # the rollout window instead of failing on the first handshake.
    last_exc: subprocess.CalledProcessError | None = None
    for attempt in range(8):
        if attempt:
            time.sleep(8)
            console.print(f"[dim]box not ready yet, retrying sync ({attempt + 1}/8)...[/]")
        try:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", host_alias, f"mkdir -p {remote_dir}"],
                check=True,
            )
            _rsync_up(project, host_alias, remote_dir)
            last_exc = None
            break
        except subprocess.CalledProcessError as exc:
            last_exc = exc
    if last_exc is not None:
        _fail(
            f"could not sync to the {agent} dev box (exit {last_exc.returncode}).\n"
            "Common causes: the box is still cold-starting (retry), or your network "
            "blocks the connection. Run on this machine instead: `a2a dev --local`."
        )

    # Best-effort dep install, then the box's own hot-reloading dev server, bound
    # to the internal agent port the bridge proxies. We set A2A_DEVBOX_PORT inline
    # (sshd sessions don't inherit the container env, so we can't rely on the
    # box's own env): new a2a-pack sees it and forces the local path, preventing a
    # box-in-box recursion; old a2a-pack ignores it and defaults to local anyway.
    # No --local flag, so this works regardless of the box's a2a-pack version.
    remote_cmd = (
        f"cd {remote_dir} && "
        # a dropped ssh session can orphan the previous dev server on the box;
        # clear it or uvicorn dies with 'address already in use'. The name
        # pattern also appears in this wrapper shell's own command line, so
        # exclude $$ or the cleanup takes the session down with it.
        "pgrep -f 'a2a dev --host-runtime' | grep -vx $$ | xargs -r kill 2>/dev/null; sleep 1; "
        "if [ -f requirements.txt ]; then pip install -q -r requirements.txt || true; fi && "
        # WATCHFILES_FORCE_POLLING: rsync-updated files on the box's FUSE-backed
        # filesystem emit no inotify events, so without polling the reloader
        # never sees synced edits and hot reload silently does nothing.
        f"exec env A2A_DEVBOX_PORT={_DEVBOX_BRIDGE_PORT} WATCHFILES_FORCE_POLLING=true "
        f"a2a dev --host-runtime "
        f"--host 127.0.0.1 --port {_DEVBOX_AGENT_PORT} --env-file {env_file}"
    )
    server = subprocess.Popen(["ssh", host_alias, remote_cmd])

    console.print(
        Panel.fit(
            "\n".join(
                [
                    f"agent : {agent}",
                    f"url   : [bold]{public_url}[/]  (public, shareable)",
                    f"box   : {info['host']}  (scales to zero when idle)",
                    f"sync  : {project.resolve()} → {remote_dir}",
                    "",
                    "edit locally — saves sync up and hot-reload on the box.",
                    "Ctrl-C to stop.",
                ]
            ),
            title="[bold green]a2a cloud dev[/]",
        )
    )

    prev = _project_fingerprint(project)
    try:
        while True:
            if server.poll() is not None:
                _fail("dev box server exited; see the log above")
            time.sleep(0.6)
            cur = _project_fingerprint(project)
            if cur != prev:
                prev = cur
                try:
                    _rsync_up(project, host_alias, remote_dir)
                    console.print("[dim]synced[/]")
                except subprocess.CalledProcessError as exc:
                    console.print(f"[yellow]sync failed: {exc}[/]")
    except KeyboardInterrupt:
        console.print("\n[dim]stopping (box scales to zero shortly)...[/]")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


@app.command(name="chat")
def chat(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(True, "--reload/--no-reload"),
    env_file: Path = typer.Option(Path(".env.local"), "--env-file"),
    workspace: Path | None = typer.Option(None, "--workspace"),
    build_image: bool = typer.Option(
        True,
        "--build/--no-build",
        help="Build the local agent image before starting compose.",
    ),
    up: bool = typer.Option(
        True,
        "--up/--no-up",
        help="Start the compose harness after writing it.",
    ),
    detach: bool = typer.Option(
        False,
        "--detach/--foreground",
        help="Run compose in the background.",
    ),
    down: bool = typer.Option(
        False,
        "--down",
        help="Stop the existing chat harness for this project.",
    ),
    volumes: bool = typer.Option(
        False,
        "--volumes",
        help="With --down, also remove the chat harness data volumes.",
    ),
    print_compose: bool = typer.Option(
        False,
        "--print-compose",
        help="Print the generated compose file.",
    ),
    base_image: str = typer.Option(
        DEFAULT_AGENT_BASE_IMAGE,
        "--base-image",
        help="Base image for the generated local Dockerfile.",
    ),
) -> None:
    """Run one agent plus declared local Qdrant/Postgres resources."""
    local = load_local_project(project)
    project_name = (
        f"a2a-chat-{re.sub(r'[^a-z0-9]+', '-', local.agent_cls.name.lower()).strip('-') or 'agent'}"
    )[:63].strip("-")
    compose_path = project / ".a2a" / "chat" / "docker-compose.yml"
    if down:
        if not compose_path.exists():
            _fail(f"missing {compose_path}; run `a2a chat` first")
        cmd = ["docker", "compose", "-p", project_name, "-f", str(compose_path), "down"]
        if volumes:
            cmd.append("--volumes")
        _run(cmd)
        console.print(f"[green]stopped[/] {project_name}")
        return

    env_path = env_file if env_file.is_absolute() else project / env_file
    workspace_root = ensure_local_workspace(project, workspace=workspace)
    loaded = load_env_file(env_path)
    credentials.load_local_llm_into_env()
    normalize_local_llm_env()
    credentials.load_agent_setup_into_env(
        local.agent_cls.name,
        set(local.agent_cls.required_env)
        | set(local.agent_cls.required_secrets)
        | {field.name for field in local.agent_cls.consumer_setup.fields},
    )
    image = _dev_image_ref(local.agent_cls.name)
    if build_image:
        with tempfile.TemporaryDirectory(prefix="a2a-chat-build-") as tmp:
            ctx = Path(tmp)
            _stage_build_context(project, ctx)
            _stage_default_dockerfile(ctx, local.config, base_image=base_image)
            _run(["docker", "build", "-t", image, str(ctx)])

    plan = write_chat_compose(
        project=project,
        local=local,
        image=image,
        env_path=env_path,
        workspace_root=workspace_root,
        host=host,
        port=port,
        reload=reload,
        passthrough_env=_docker_passthrough_env(local.agent_cls, loaded),
    )
    warnings = _local_dev_warnings(local.agent_cls)
    console.print(
        Panel.fit(
            "\n".join(
                [
                    f"agent: {local.agent_cls.name} v{local.agent_cls.version}",
                    f"url: http://{host}:{port}",
                    f"chat ui: http://{host}:{port}/_dev",
                    f"compose: {plan.compose_path}",
                    f"workspace: {workspace_root}",
                    f"resources: {', '.join(plan.resource_summary)}",
                    *(
                        ["", *[f"warning: {warning}" for warning in warnings]]
                        if warnings
                        else []
                    ),
                ]
            ),
            title="[bold green]docker chat[/]",
        )
    )
    if print_compose:
        console.print(plan.compose_path.read_text(encoding="utf-8"))
    if not up:
        return
    cmd = [
        "docker",
        "compose",
        "-p",
        plan.project_name,
        "-f",
        str(plan.compose_path),
        "up",
        "--remove-orphans",
    ]
    if detach:
        cmd.append("-d")
    _run(cmd)


@app.command(name="test")
def test_agent(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    env_file: Path = typer.Option(Path(".env.local"), "--env-file"),
    workspace: Path | None = typer.Option(None, "--workspace"),
    invoke: bool = typer.Option(False, "--invoke/--no-invoke", help="Run a local tool call"),
    skill: str | None = typer.Option(None, "--skill", help="Tool to invoke"),
    args_json: str | None = typer.Option(None, "--args-json", help="JSON object args for --invoke"),
) -> None:
    """Run local preflight checks before deploying."""
    env_path = env_file if env_file.is_absolute() else project / env_file
    try:
        checks, result = run_preflight_sync(
            project,
            env_file=env_path,
            workspace=workspace,
            invoke=invoke,
            skill_name=skill,
            args_json=args_json,
        )
    except ValueError as exc:
        # Bad --skill/--args-json is a usage error, not a broken project; the
        # message already names the valid skills, so don't bury it.
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(f"local test failed to start: {exc}")

    failed = [check for check in checks if not check.ok]
    for check in checks:
        icon = "[green]✓[/]" if check.ok else "[red]✗[/]"
        console.print(f"{icon} [bold]{check.name}[/] {check.message}")
    if result is not None:
        console.print_json(json.dumps(result, default=str))
    if failed:
        raise typer.Exit(1)
    console.print("[green]local checks passed[/]")


@app.command(name="mcp-url")
def mcp_url(
    name: str | None = typer.Argument(
        None, help="Agent name (defaults to a2a.yaml in --project)"
    ),
    project: Path = typer.Option(Path("."), "--project", "-p"),
    api: str | None = typer.Option(None, "--api"),
) -> None:
    """Print the MCP connect config for a deployed agent.

    Every shipped agent auto-exposes ``POST /mcp`` (Streamable HTTP).
    This command prints the JSON snippet you paste into Claude Code,
    Cursor, or any other MCP client to wire up the agent.
    """
    if name is None:
        cfg = _read_yaml(project / "a2a.yaml")
        name = cfg["name"]
    try:
        rows = _client(api).list_agents()
    except ApiError as exc:
        _fail(str(exc))
    row = next((r for r in rows if r["name"] == name), None)
    if row is None:
        _fail(f"agent {name!r} not found (run `a2a deploy` first)")
    url = row.get("url")
    if not url:
        _fail(f"agent {name!r} has no public URL yet")

    snippet = {
        "mcpServers": {
            name: {
                "type": "http",
                "url": f"{url.rstrip('/')}/mcp",
            }
        }
    }
    console.print_json(json.dumps(snippet))


# --------------------------------------------------------------------------- #
# frontend                                                                    #
# --------------------------------------------------------------------------- #


@frontend_app.command(name="build")
def build_frontend(
    project: Path = typer.Option(Path("."), "--project", "-p"),
) -> None:
    """Build the packed frontend declared in a2a.yaml."""
    cfg = _read_yaml(project / "a2a.yaml")
    try:
        frontend = load_frontend_config(project, cfg)
    except ValueError as exc:
        _fail(str(exc))
    if frontend is None:
        _fail("a2a.yaml has no frontend section")

    source_dir = frontend.source_dir(project)
    if not source_dir.is_dir():
        _fail(f"frontend.path does not exist: {source_dir}")
    if frontend.build:
        _run(["/bin/sh", "-lc", frontend.build], cwd=str(source_dir))

    if frontend.is_server_rendered:
        ready_path = frontend.server_ready_path(project)
        if not ready_path.is_file():
            _fail(f"frontend server output is missing: {ready_path}")
        console.print(f"[green]frontend server ready[/] {ready_path} -> {frontend.mount}")
        return

    dist_dir = frontend.dist_dir(project)
    if not (dist_dir / "index.html").is_file():
        _fail(f"frontend dist is missing index.html: {dist_dir}")
    console.print(f"[green]frontend ready[/] {dist_dir} -> {frontend.mount}")


@frontend_app.command(name="info")
def frontend_info(
    project: Path = typer.Option(Path("."), "--project", "-p"),
) -> None:
    """Print the packed frontend config resolved from a2a.yaml."""
    cfg = _read_yaml(project / "a2a.yaml")
    try:
        frontend = load_frontend_config(project, cfg)
    except ValueError as exc:
        _fail(str(exc))
    if frontend is None:
        _fail("a2a.yaml has no frontend section")
    ready_path = (
        frontend.server_ready_path(project)
        if frontend.is_server_rendered
        else frontend.dist_dir(project) / "index.html"
    )
    payload = {
        "type": frontend.kind,
        "framework": frontend.framework,
        "path": str(frontend.source_dir(project)),
        "dist": str(frontend.dist_dir(project)) if frontend.is_static else None,
        "mount": frontend.mount,
        "build": frontend.build,
        "start": frontend.start,
        "port": frontend.port,
        "auth": frontend.auth,
        "docs_url": frontend.docs_url,
        "ready": ready_path.is_file(),
        "ready_path": str(ready_path),
    }
    console.print_json(json.dumps(payload))


# --------------------------------------------------------------------------- #
# build / deploy                                                              #
# --------------------------------------------------------------------------- #


def _resolve_image_ref(name: str, version: str, project: Path, registry: str) -> str:
    sha = _git_short_sha(project)
    tag = f"{version}-{sha}" if sha else version
    return f"{registry}/agents/{name}:{tag}"


def _stage_build_context(project: Path, dst: Path) -> None:
    """Copy project files + bundled SDK source into a temp build dir."""
    for item in project.iterdir():
        if item.name in {*PROJECT_EXCLUDED_DIRS, "_a2a_sdk"}:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=shutil.ignore_patterns(*PROJECT_EXCLUDED_DIRS),
            )
        else:
            shutil.copy2(item, target)
    sdk_src = _sdk_source_dir()
    sdk_dst = dst / "_a2a_sdk"
    shutil.copytree(
        sdk_src,
        sdk_dst,
        ignore=shutil.ignore_patterns(
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".venv",
            "__pycache__",
            "*.egg-info",
            "build",
            "dist",
            "node_modules",
        ),
    )


@app.command()
def build(
    project: Path = typer.Option(Path("."), "--project", "-p"),
    registry: str | None = typer.Option(
        None, "--registry", help="Image registry host (default: registry.<platform domain>)"
    ),
    push: bool = typer.Option(False, "--push", help="Also push the built image"),
) -> None:
    """Build (and optionally push) the container image for the agent."""
    cfg = _read_yaml(project / "a2a.yaml")
    image = _resolve_image_ref(
        cfg["name"], cfg["version"], project, registry or platform.registry_host()
    )

    with tempfile.TemporaryDirectory(prefix="a2a-build-") as tmp:
        ctx = Path(tmp)
        _stage_build_context(project, ctx)
        if not (ctx / "Dockerfile").exists():
            frontend_stage, frontend_runtime = _frontend_dockerfile_blocks(cfg)
            (ctx / "Dockerfile").write_text(
                _render_template(
                    "Dockerfile.tmpl",
                    entrypoint=cfg["entrypoint"],
                    base_image=DEFAULT_AGENT_BASE_IMAGE,
                    frontend_stage=frontend_stage,
                    frontend_runtime=frontend_runtime,
                )
            )
        _run(["docker", "build", "-t", image, str(ctx)])

    console.print(f"[green]built[/] [cyan]{image}[/]")
    if push:
        _run(["docker", "push", image])
        console.print(f"[green]pushed[/] [cyan]{image}[/]")


def _make_tarball(project: Path, *, agent_dsl: AgentDsl | None = None) -> bytes:
    """Tar.gz the user's project directory.

    Excludes platform/dev artifacts so build images stay small. The
    platform stamps in Dockerfile/workflow/manifests on the server side.
    """
    import io
    import tarfile

    cfg = _read_yaml_optional(project / "a2a.yaml")
    frontend = load_frontend_config(project, cfg)
    prebuilt_frontend_dist: Path | None = None
    build_managed_frontend_dist: Path | None = None
    if frontend is not None and not frontend.build:
        try:
            prebuilt_frontend_dist = frontend.dist_dir(project).resolve().relative_to(project.resolve())
        except ValueError:
            prebuilt_frontend_dist = None
    if frontend is not None and frontend.build:
        try:
            build_managed_frontend_dist = (
                frontend.dist_dir(project).resolve().relative_to(project.resolve())
            )
        except ValueError:
            build_managed_frontend_dist = None
    package_includes = _package_include_paths(cfg)

    excluded_dirs = PROJECT_EXCLUDED_DIRS
    excluded_files = {"Dockerfile", ".dockerignore", ".env", ".env.local"}

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(project):
            root_path = Path(root)
            kept_dirs: list[str] = []
            for dirname in dirs:
                rel_dir = (root_path / dirname).resolve().relative_to(project.resolve())
                if dirname in excluded_dirs and not _is_frontend_dist_path(
                    rel_dir,
                    prebuilt_frontend_dist,
                ) and not _is_package_include_path(
                    rel_dir,
                    package_includes,
                ):
                    continue
                if _is_frontend_dist_path(
                    rel_dir,
                    build_managed_frontend_dist,
                ) and not _is_package_include_path(
                    rel_dir,
                    package_includes,
                ):
                    continue
                kept_dirs.append(dirname)
            dirs[:] = kept_dirs
            for fname in files:
                fpath = Path(root) / fname
                arcname = fpath.relative_to(project)
                inside_excluded_dir = any(
                    part in excluded_dirs for part in arcname.parts[:-1]
                )
                if (
                    inside_excluded_dir
                    and not _is_frontend_dist_path(arcname, prebuilt_frontend_dist)
                    and not _is_package_include_path(arcname, package_includes)
                ):
                    continue
                if (
                    _is_frontend_dist_path(arcname, build_managed_frontend_dist)
                    and not _is_package_include_path(arcname, package_includes)
                ):
                    continue
                if (
                    (fname in excluded_files or fname.endswith(".pyc"))
                    and not _is_package_include_path(arcname, package_includes)
                ):
                    continue
                tar.add(fpath, arcname=str(arcname))
        if agent_dsl is not None:
            body = (agent_dsl.model_dump_json(indent=2) + "\n").encode("utf-8")
            info = tarfile.TarInfo(".a2a/agent.dsl.json")
            info.size = len(body)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _tarball_size_report(tarball: bytes, *, limit: int = 8) -> str:
    """Return a short human-readable summary of what a source tarball contains."""

    import io
    import tarfile

    total_mb = len(tarball) / 1024 / 1024
    files: list[tuple[int, str]] = []
    directories: dict[str, int] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            files.append((member.size, member.name))
            parts = Path(member.name).parts
            if parts:
                directories[parts[0]] = directories.get(parts[0], 0) + member.size
            if len(parts) >= 2:
                key = f"{parts[0]}/{parts[1]}"
                directories[key] = directories.get(key, 0) + member.size

    largest_files = sorted(files, reverse=True)[:limit]
    largest_dirs = sorted(
        ((size, name) for name, size in directories.items()),
        reverse=True,
    )[:limit]
    lines = [f"Packaged source size: {total_mb:.2f}MB compressed."]
    if largest_files:
        lines.append("Largest included files:")
        for size, name in largest_files:
            lines.append(f"  {size / 1024 / 1024:.2f}MB  {name}")
    if largest_dirs:
        lines.append("Largest included paths:")
        for size, name in largest_dirs:
            lines.append(f"  {size / 1024 / 1024:.2f}MB  {name}/")
    return "\n".join(lines)


def _upload_error_message(exc: ApiError, tarball: bytes) -> str:
    message = _api_error_message(exc)
    if exc.status != 413:
        return message
    return f"{message}\n\n{_tarball_size_report(tarball)}"


def _is_frontend_dist_path(path: Path, frontend_dist: Path | None) -> bool:
    if frontend_dist is None:
        return False
    return path == frontend_dist or frontend_dist in path.parents


def _package_include_paths(cfg: dict[str, Any]) -> tuple[Path, ...]:
    package = cfg.get("package")
    raw: Any = None
    if isinstance(package, dict):
        raw = package.get("include")
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        _fail("package.include must be a string or list of relative paths")
    out: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            _fail("package.include entries must be strings")
        value = item.strip().replace("\\", "/").strip("/")
        parts = Path(value).parts
        if not value or value == "." or Path(value).is_absolute() or ".." in parts:
            _fail(f"invalid package.include path: {item!r}")
        key = Path(value).as_posix()
        if key not in seen:
            seen.add(key)
            out.append(Path(value))
    return tuple(out)


def _is_package_include_path(path: Path, includes: tuple[Path, ...]) -> bool:
    return any(
        path == include or include in path.parents or path in include.parents
        for include in includes
    )


def _wait_for_url(url: str, timeout: int = 180) -> bool:
    import time

    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=3.0) as c:
                if c.get(f"{url}/healthz").status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)
    return False


# Mirrors control_plane.deployments.ACTIVE_DEPLOY_STATUSES. Anything else --
# "live", "failed" -- is terminal, so the wait loop stops and reports a verdict
# instead of shrugging with "still building".
ACTIVE_DEPLOY_STATUSES = frozenset({"queued", "building", "deploying", "verifying"})
# Enough of the tail to show the compiler/pip error that actually killed the
# build without dumping a full Gitea Actions transcript into the terminal.
DEPLOY_FAILURE_LOG_LINES = 40


def _deployment_stage_lines(events: Any) -> list[str]:
    """One `stage status message` line per deployment event, oldest first."""
    if not isinstance(events, list):
        return []
    out: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "?")
        status = str(event.get("status") or "?")
        message = str(event.get("message") or "").strip()
        out.append(f"{stage} {status}" + (f": {message}" if message else ""))
    return out


def _fetch_deployment_logs(
    client: ControlPlaneClient,
    name: str,
    deploy_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Best effort: a deploy verdict must never be swallowed by a log fetch.

    Catches broadly on purpose — the control plane proxies Gitea/Argo/pod logs
    inline on this call, so it is the request most likely to time out, and a
    failed deploy still has to print "deploy failed" and exit non-zero.

    Returns ``(logs, failure)``. ``failure`` is the reason the fetch did not
    happen, or ``None`` when the control plane answered. Callers must report
    that reason rather than guessing why the tail is empty — a caller with org
    (not owner) access gets 403 here while the deployment route above answers
    fine, so "no logs" and "not allowed to read logs" are different outcomes.
    """
    try:
        payload = client.get_agent_deployment_logs(name, deploy_id)
    except ApiError as exc:
        detail = (exc.message or "").strip() or f"HTTP {exc.status}"
        if exc.status == 403:
            # GET .../deployments/{id} is org-wide; .../logs is owner-only.
            detail = f"{detail} (build logs are readable by the agent owner only)"
        return [], f"{exc.status}: {detail}"
    except Exception as exc:  # noqa: BLE001 — httpx transport errors, timeouts
        return [], f"{type(exc).__name__}: {exc}"
    logs = payload.get("logs") if isinstance(payload, dict) else None
    items = [item for item in logs if isinstance(item, dict)] if isinstance(logs, list) else []
    return items, None


def _print_deployment_log_tail(
    logs: list[dict[str, Any]],
    *,
    lines: int = DEPLOY_FAILURE_LOG_LINES,
) -> bool:
    """Print the last ``lines`` of each captured log stream. Returns whether
    anything was printed."""
    printed = False
    for entry in logs:
        content = str(entry.get("content") or "").rstrip()
        if not content:
            continue
        stage = str(entry.get("stage") or "?")
        source = str(entry.get("source") or "?")
        tail = content.splitlines()[-lines:]
        console.print(f"[bold]--- {stage} ({source}) last {len(tail)} lines ---[/]")
        for line in tail:
            console.print(f"  {escape_markup(line)}")
        printed = True
    return printed


def _wait_for_deployment(
    client: ControlPlaneClient,
    name: str,
    deploy_id: str,
    *,
    timeout: int = 600,
    poll_seconds: float = 4.0,
) -> dict[str, Any] | None:
    """Poll one deployment until it reaches a terminal status.

    Returns the final deployment payload, or ``None`` if the poll timed out
    (the deploy is still running server-side; it was not judged).
    """
    import time

    deadline = time.monotonic() + timeout
    seen_stages = 0
    latest: dict[str, Any] | None = None
    # A deployment row can lag its own create response by a poll or two; a
    # persistent 404 is a real error, so give up rather than burn the timeout.
    misses = 0
    while time.monotonic() < deadline:
        try:
            latest = client.get_agent_deployment(name, deploy_id)
        except ApiError as exc:
            if exc.status == 404:
                misses += 1
                if misses > 5:
                    _fail(f"deployment {deploy_id} not found; try `a2a logs {name}`")
            elif exc.status < 500:
                # ``_fail`` prints through Rich; the server detail is free text.
                _fail(
                    f"could not read deployment {deploy_id}: "
                    f"{escape_markup(exc.message)}"
                )
            # 5xx is a control-plane hiccup: keep waiting, the build is fine.
            time.sleep(poll_seconds)
            continue
        except Exception:  # noqa: BLE001 — httpx transport errors, DNS, etc.
            # A local network blip says nothing about the build. Keep polling;
            # the timeout is the backstop.
            time.sleep(poll_seconds)
            continue
        stage_lines = _deployment_stage_lines(latest.get("events"))
        for line in stage_lines[seen_stages:]:
            console.print(f"[dim]  {escape_markup(line)}[/]")
        seen_stages = len(stage_lines)
        if str(latest.get("status") or "") not in ACTIVE_DEPLOY_STATUSES:
            return latest
        time.sleep(poll_seconds)
    return None


def _report_deployment_result(
    client: ControlPlaneClient,
    name: str,
    deploy_id: str,
    deployment: dict[str, Any] | None,
    *,
    url: str | None,
) -> None:
    """Print the deploy verdict. Exits non-zero when the build failed."""
    if deployment is None:
        console.print(
            f"[yellow]still building[/] after the wait window; "
            f"follow it with `a2a logs {name} --follow`"
        )
        return

    status = str(deployment.get("status") or "")
    if status == "live":
        live_url = deployment.get("agent_url") or url
        console.print(
            f"[green]live[/]: {escape_markup(str(live_url))}"
            if live_url
            else "[green]live[/]"
        )
        return

    # Both fields are server-supplied free text (the control plane formats
    # `error` from an exception), so they can contain `[...]` that Rich would
    # read as markup and raise on — escape before printing.
    error = escape_markup(str(deployment.get("error") or "").strip())
    console.print(
        f"[bold red]deploy failed[/] ({escape_markup(status)})"
        + (f": {error}" if error else "")
    )
    for line in _deployment_stage_lines(deployment.get("events")):
        if " failed" in line:
            console.print(f"  [red]{escape_markup(line)}[/]")
    logs, log_error = _fetch_deployment_logs(client, name, deploy_id)
    if not _print_deployment_log_tail(logs):
        if log_error:
            console.print(f"[dim]could not read build logs: {escape_markup(log_error)}[/]")
        else:
            console.print(
                "[dim]no build logs were captured for this deploy; "
                f"retry with `a2a logs {name} --deploy {deploy_id}`[/]"
            )
    console.print(f"[dim]full logs: `a2a logs {name} --deploy {deploy_id}`[/]")
    raise typer.Exit(1)


@app.command()
def logs(
    agent: str | None = typer.Argument(
        None, help="Agent name (defaults to a2a.yaml in --project)"
    ),
    deploy: str | None = typer.Option(
        None, "--deploy", help="Deployment id (default: the most recent deploy)"
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Keep polling until the deploy reaches a terminal state"
    ),
    tail: int = typer.Option(
        200, "--tail", "-n", help="Lines to show from the end of each log stream"
    ),
    project: Path = typer.Option(Path("."), "--project", "-p"),
    api: str | None = typer.Option(None, "--api", help="Override control plane URL"),
) -> None:
    """Read the build and runtime logs behind a deploy.

    This is what to run when `a2a deploy` says a build failed: it prints the
    same Gitea Actions / pod / Argo output the dashboard's deployment timeline
    shows, for the deployment you name or the most recent one.
    """
    import time

    if agent is None:
        cfg = _read_yaml_optional(project / "a2a.yaml")
        agent = str(cfg.get("name") or "").strip() or None
    if not agent:
        _fail("no agent name (pass AGENT or run inside a project with a2a.yaml)")

    client = _client(api)
    deploy_id = deploy
    if deploy_id is None:
        try:
            rows = client.list_agent_deployments(agent)
        except ApiError as exc:
            _fail(escape_markup(_api_error_message(exc)))
        if not rows:
            _fail(f"no deployments recorded for {agent!r} yet (run `a2a deploy` first)")
        deploy_id = str(rows[0].get("deploy_id") or "")
        if not deploy_id:
            _fail(f"latest deployment for {agent!r} has no id")

    try:
        deployment = client.get_agent_deployment(agent, deploy_id)
    except ApiError as exc:
        _fail(escape_markup(_api_error_message(exc)))

    status = str(deployment.get("status") or "")
    console.print(
        f"[bold]{escape_markup(agent)}[/] deploy [cyan]{escape_markup(deploy_id)}[/] "
        f"status=[bold]{escape_markup(status)}[/]"
    )
    for line in _deployment_stage_lines(deployment.get("events")):
        console.print(f"[dim]  {escape_markup(line)}[/]")

    if follow and status in ACTIVE_DEPLOY_STATUSES:
        seen = len(_deployment_stage_lines(deployment.get("events")))
        while status in ACTIVE_DEPLOY_STATUSES:
            time.sleep(4)
            try:
                deployment = client.get_agent_deployment(agent, deploy_id)
            except ApiError as exc:
                _fail(escape_markup(_api_error_message(exc)))
            stage_lines = _deployment_stage_lines(deployment.get("events"))
            for line in stage_lines[seen:]:
                console.print(f"[dim]  {escape_markup(line)}[/]")
            seen = len(stage_lines)
            status = str(deployment.get("status") or "")
        console.print(f"[bold]status=[/]{escape_markup(status)}")

    fetched, log_error = _fetch_deployment_logs(client, agent, deploy_id)
    if not _print_deployment_log_tail(fetched, lines=tail):
        if log_error:
            console.print(f"[dim]could not read build logs: {escape_markup(log_error)}[/]")
        elif status == "live":
            # deployments.py::collect_deployment_logs returns early once the
            # deploy is live, so a live deploy with no persisted tail has none.
            console.print(
                "[dim]no raw logs captured for this deploy "
                "(the control plane stops proxying build logs once an agent is live)[/]"
            )
        else:
            console.print("[dim]no raw logs captured for this deploy yet[/]")
    error = str(deployment.get("error") or "").strip()
    if error:
        console.print(f"[red]error:[/] {escape_markup(error)}")
    if status not in ACTIVE_DEPLOY_STATUSES and status != "live":
        raise typer.Exit(1)


# --- registry listing -------------------------------------------------------
#
# Whether a deploy lists the agent in the platform's public registry has
# three possible inputs, in precedence order:
#
#   1. ``a2a deploy --public`` / ``--private``: an explicit choice for this run.
#   2. ``expose.public`` in a2a.yaml: an explicit choice recorded in the project.
#   3. Neither: *unspecified*. An absent key is not consent to publish, so the
#      CLI asserts nothing new — it reuses the listing the agent already has,
#      and only falls back to unlisted for a name the registry has never seen.
#
# (3) has to be resolved client-side. ``POST /v1/agents/from-tarball`` takes
# ``public`` as a plain form field that is written onto ``agents.public`` on
# every upload, and the field defaults to True when omitted, so there is no
# "leave unchanged" on the wire. Reading the current value before uploading is
# what keeps an already-listed agent from being silently unlisted by a redeploy.


class ListingDeclarationError(ValueError):
    """`expose.public` is present in a2a.yaml but is not a boolean."""


def _declared_listing(cfg: dict[str, Any]) -> bool | None:
    """``expose.public`` as written in a2a.yaml, or None when it is absent.

    Only a real YAML boolean counts. Coercing with ``bool()`` would make
    ``public: "false"`` (a non-empty string) publish the agent, and an empty
    ``public:`` unlist one, in both cases while the CLI announced a value the
    file does not contain. An ambiguous declaration is an error, not a guess.
    ``public:`` with no value is the one exception: YAML reads it as null, which
    is genuinely "no value written", so it means unspecified like an absent key.
    """
    expose = cfg.get("expose")
    if not isinstance(expose, dict) or "public" not in expose:
        return None
    value = expose["public"]
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ListingDeclarationError(
            f"a2a.yaml: `expose.public` must be true or false, got {value!r}. "
            "Quoted strings are not booleans - write `public: false`, not "
            "`public: \"false\"`. Delete the key to keep whatever listing the "
            "agent already has."
        )
    return value


def _registered_listing(client: ControlPlaneClient, name: str) -> bool | None:
    """Current registry listing for NAME, or None if the CP has no such agent.

    403 is folded into "no such agent": the name belongs to someone else, and
    the upload that follows will fail its own access check with a better
    message. Every other error is raised, so callers can refuse to turn an
    unreadable answer into a visibility change.
    """
    try:
        agent = client.get_agent(name)
    except ApiError as exc:
        if exc.status in (403, 404):
            return None
        raise
    value = agent.get("public")
    return None if value is None else bool(value)


def _resolve_listing(
    *,
    flag: bool | None,
    declared: bool | None,
    registered: bool | None,
) -> tuple[bool, str]:
    """Return ``(listed, why)`` for one deploy. Pure; precedence is above."""
    if flag is not None:
        return flag, "flag"
    if declared is not None:
        return declared, "manifest"
    if registered is not None:
        return registered, "unchanged"
    return False, "new"


def _listing_why(is_public: bool, why: str) -> str:
    """Plain-English reason for the listing this deploy chose."""
    if why == "flag":
        return "you passed `--public`" if is_public else "you passed `--private`"
    if why == "manifest":
        value = "true" if is_public else "false"
        return f"a2a.yaml sets `expose.public: {value}`"
    if why == "unchanged":
        return (
            "a2a.yaml declares no `expose.public`, so this deploy kept the "
            "listing the agent already had"
        )
    if why == "default":
        return (
            "you passed neither `--public` nor `--private`, and a generated "
            "agent starts unlisted"
        )
    return (
        "a2a.yaml declares no `expose.public` and this agent is not in the "
        "registry yet, so it starts unlisted"
    )


def _listing_lookup_failure(name: str, exc: BaseException) -> str:
    """Explain a failed listing lookup, and only offer advice that can work.

    The flags resolve the listing question locally, so they unblock a deploy
    only when the lookup route is the thing that failed. When the session is
    invalid or the control plane is unreachable, the upload would fail the same
    way moments later, and telling the user to re-run with a flag would send
    them straight back into the same wall.
    """
    prefix = f"could not read the current registry listing for {name!r}"
    if isinstance(exc, ApiError):
        reason = escape_markup(_api_error_message(exc))
        if exc.status == 401:
            return (
                f"{prefix}: {reason}. Your session is not valid, so the upload "
                "would fail the same way — run `a2a login` and deploy again."
            )
        return (
            f"{prefix}: {reason}. a2a.yaml declares no `expose.public`, and this "
            "deploy will not guess whether to list your agent. Re-run with "
            "`a2a deploy --public` or `a2a deploy --private`, or set "
            "`expose.public` in a2a.yaml."
        )
    return (
        f"{prefix}: {escape_markup(str(exc))}. The control plane could not be "
        "reached, so the upload would fail the same way — check the connection "
        "(or `--api`) and deploy again."
    )


def _deploy_listing(
    client: ControlPlaneClient,
    *,
    name: str,
    flag: bool | None,
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """Decide this deploy's listing, asking the control plane only if needed."""
    try:
        declared = _declared_listing(cfg)
    except ListingDeclarationError as exc:
        _fail(escape_markup(str(exc)))
        raise  # unreachable; _fail raises. Keeps `declared` definitely bound.
    registered: bool | None = None
    if flag is None and declared is None:
        try:
            registered = _registered_listing(client, name)
        except Exception as exc:  # noqa: BLE001 - transport errors count too
            _fail(_listing_lookup_failure(name, exc))
    return _resolve_listing(flag=flag, declared=declared, registered=registered)


def _print_listing_outcome(
    is_public: bool,
    why: str,
    *,
    list_cmd: str = "a2a deploy --public",
    unlist_cmd: str = "a2a deploy --private",
) -> None:
    """One line, every command that writes a listing, so it is never a surprise."""
    suffix = f" ({_listing_why(is_public, why)})"
    if is_public:
        console.print(
            f"[bold]listing:[/] listed in the public registry at {platform.platform_domain()}"
            f"{suffix}. To unlist it, set `expose.public: false` in a2a.yaml or "
            f"run `{unlist_cmd}`."
        )
        return
    console.print(
        f"[bold]listing:[/] unlisted — kept out of the public registry at "
        f"{platform.platform_domain()}{suffix}. This is a listing choice, not access control — "
        "the agent still gets its canonical URL, and who may call it is decided "
        "by its auth model. To list it, set `expose.public: true` in a2a.yaml or "
        f"run `{list_cmd}`."
    )


@app.command()
def deploy(
    project_path: Annotated[
        Path | None,
        typer.Argument(
            metavar="PROJECT",
            help="Project directory containing a2a.yaml",
        ),
    ] = None,
    project: Path = typer.Option(Path("."), "--project", "-p"),
    public: bool | None = typer.Option(
        None,
        "--public/--private",
        help=(
            "List/unlist the agent in the platform's public registry. "
            "Defaults to `expose.public` from a2a.yaml; with no such key the "
            "agent keeps its current listing, and a brand-new agent is unlisted."
        ),
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Poll until URL is live"),
    api: str | None = typer.Option(None, "--api", help="Override control plane URL"),
) -> None:
    """Ship the agent.

    Tarballs your source, uploads to the control plane, and prints the
    URL when it's live. No local docker. No git. No knowledge of how the
    platform builds or deploys.
    """
    if project_path is not None and project != Path("."):
        _fail("pass either PROJECT or --project, not both")
    project_dir = project_path or project

    creds = credentials.load()
    if creds is None:
        _fail("not logged in (run `a2a signup` or `a2a login`)")

    cfg, dsl = _compile_project_dsl(project_dir)
    description = cfg.get("description", dsl.description or "")

    client = _client(api)
    is_public, listing_why = _deploy_listing(
        client, name=dsl.name, flag=public, cfg=cfg
    )

    console.print("[dim]packaging source...[/]")
    tarball = _make_tarball(project_dir, agent_dsl=dsl)
    size_kb = len(tarball) / 1024
    console.print(f"[dim]uploading {size_kb:.1f}KB to {credentials.resolve_api_url(api)}...[/]")

    try:
        out = client.from_tarball(
            name=dsl.name,
            version=dsl.version,
            entrypoint=cfg["entrypoint"],
            description=description,
            public=is_public,
            tarball=tarball,
            agent_dsl=dsl.model_dump(mode="json"),
        )
    except ApiError as exc:
        _fail(_upload_error_message(exc, tarball))

    summary = {
        "agent": out["name"],
        "version": out["version"],
        "status": out["status"],
        "url": out.get("url"),
        "listed": is_public,
    }
    console.print(
        Panel.fit(
            json.dumps(summary, indent=2),
            title="[bold green]shipped[/]",
        )
    )
    _print_listing_outcome(is_public, listing_why)

    url = out.get("url")
    if not wait:
        return

    deploy_id = out.get("deployment_id")
    if deploy_id:
        console.print(f"[dim]waiting for deploy {deploy_id} ...[/]")
        deployment = _wait_for_deployment(client, out["name"], str(deploy_id))
        _report_deployment_result(
            client, out["name"], str(deploy_id), deployment, url=url
        )
        return
    # Control planes older than deployment tracking only hand back a URL.
    if url:
        console.print(f"[dim]waiting for {url} ...[/]")
        if _wait_for_url(url):
            console.print(f"[green]live[/]: {url}")
        else:
            console.print(
                f"[yellow]still building[/]; follow it with `a2a logs {out['name']} --follow`"
            )


_A2A_SSH_DIR = Path.home() / ".a2a" / "ssh"


def _infer_agent_name() -> str | None:
    """Walk up from cwd looking for an a2a.yaml and return its ``name``."""
    for d in (Path.cwd(), *Path.cwd().parents):
        name = _read_yaml_optional(d / "a2a.yaml").get("name")
        if name:
            return str(name)
    return None


def _a2a_executable() -> str:
    """Absolute path to the running ``a2a`` entrypoint, so the ProxyCommand in
    ~/.ssh/config doesn't resolve to a stale install elsewhere on PATH."""
    exe = Path(sys.argv[0])
    if exe.stem == "a2a" and exe.exists():
        return str(exe.resolve())
    return shutil.which("a2a") or "a2a"


def _devbox_credentials_json() -> str | None:
    """The caller's CLI login as JSON, preloaded into the dev box so `a2a`
    there is already authenticated. Call after _client() so a refreshed token
    has been persisted."""
    creds = credentials.load()
    if creds is None:
        return None
    from dataclasses import asdict

    return json.dumps({k: v for k, v in asdict(creds).items() if v is not None})


def _ensure_devbox_key(agent: str) -> tuple[Path, str]:
    """Ensure a per-agent ephemeral keypair exists under ~/.a2a/ssh and return
    (private_key_path, public_key_text)."""
    _A2A_SSH_DIR.mkdir(parents=True, exist_ok=True)
    key = _A2A_SSH_DIR / agent
    pub = Path(f"{key}.pub")
    if not key.exists() or not pub.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
             "-C", f"a2a-devbox:{agent}", "-f", str(key)],
            check=True,
        )
    try:
        key.chmod(0o600)
    except OSError:
        pass
    return key.resolve(), pub.read_text().strip()


def _devbox_conn_details(
    agent: str, key: Path, host_alias: str, *, host: str | None = None, port: int | None = None
) -> dict[str, Any]:
    """Connection facts for AGENT's dev box, all paths fully resolved — the
    single source for --print, --tunnel, and --json output."""
    details: dict[str, Any] = {
        "agent": agent,
        "host_alias": host_alias,
        "user": "dev",
        "auth": "publickey",  # sshd has PasswordAuthentication no
        "identity_file": str(key.resolve()),
        "public_key_file": str(key.resolve()) + ".pub",
        "known_hosts_file": str((_A2A_SSH_DIR / f"{agent}.known_hosts").resolve()),
        "ssh_config_file": str((Path.home() / ".ssh" / "config").resolve()),
        "proxy_command": f'"{_a2a_executable()}" ssh-proxy {agent}',
    }
    if port is not None:
        details["host"] = host or "127.0.0.1"
        details["port"] = port
    return details


def _write_devbox_ssh_config(agent: str, key: Path) -> str:
    """Add/replace a Host block in ~/.ssh/config so `ssh`, VS Code Remote-SSH,
    scp, etc. reach the dev box via the `a2a ssh-proxy` ProxyCommand. Returns
    the host alias."""
    host_alias = f"{agent}.a2a"
    known = (_A2A_SSH_DIR / f"{agent}.known_hosts").resolve()
    begin = f"# >>> a2a devbox: {agent} >>>"
    end = f"# <<< a2a devbox: {agent} <<<"
    block = (
        f"{begin}\n"
        f"Host {host_alias}\n"
        f"    User dev\n"
        f"    IdentityFile {key.resolve()}\n"
        f"    IdentitiesOnly yes\n"
        f'    ProxyCommand "{_a2a_executable()}" ssh-proxy {agent}\n'
        f"    StrictHostKeyChecking accept-new\n"
        f"    UserKnownHostsFile {known}\n"
        f"{end}"
    )
    cfg = Path.home() / ".ssh" / "config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    existing = cfg.read_text() if cfg.exists() else ""
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
    cleaned = pattern.sub("", existing).rstrip()
    body = f"{cleaned}\n\n{block}\n" if cleaned else f"{block}\n"
    cfg.write_text(body)
    try:
        cfg.chmod(0o600)
    except OSError:
        pass
    return host_alias


@app.command()
def ssh(
    agent: Annotated[
        str | None,
        typer.Argument(help="Agent name (defaults to a2a.yaml in the current repo)"),
    ] = None,
    print_only: Annotated[
        bool, typer.Option("--print", help="Print connection info instead of opening a shell")
    ] = False,
    tunnel: Annotated[
        bool,
        typer.Option(
            "--tunnel",
            help="Serve the dev box on a local TCP port for SSH tools that "
            "can't use ~/.ssh/config (GUIs, IDEs, sftp clients)",
        ),
    ] = False,
    port: Annotated[
        int, typer.Option("--port", help="Local port for --tunnel (0 = pick a free one)")
    ] = 0,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print connection details as JSON (implies --print)")
    ] = False,
    api: Annotated[str | None, typer.Option("--api", hidden=True)] = None,
) -> None:
    """Open a shell in a throwaway dev box for AGENT.

    The box has node, python, a2a-pack, and the agent repo already loaded, and
    scales to zero when you disconnect. Also usable from VS Code Remote-SSH,
    Cursor, scp, and rsync via the host alias it writes to ~/.ssh/config.
    """
    if agent is None:
        agent = _infer_agent_name()
        if agent is None:
            _fail("no agent name given and no a2a.yaml found in this directory or above")
    key, _pub = _ensure_devbox_key(agent)
    host_alias = _write_devbox_ssh_config(agent, key)
    if tunnel:
        _serve_devbox_tunnel(agent, port, key, api, json_output=json_output)
        return
    if json_output:
        print(json.dumps(_devbox_conn_details(agent, key, host_alias), indent=2))
        return
    if print_only:
        info = _devbox_conn_details(agent, key, host_alias)
        console.print(
            Panel.fit(
                f"Host alias : [bold]{host_alias}[/]\n"
                f"Shell      : ssh {host_alias}\n"
                f"User       : {info['user']} (pubkey auth only, no password)\n"
                f"Key file   : {info['identity_file']}\n"
                f"VS Code    : Remote-SSH -> Connect to Host... -> {host_alias}\n"
                f"Copy files : scp ./file {host_alias}:~/\n"
                f"Other tools: a2a ssh {agent} --tunnel  (gives a local host:port)\n"
                f"Scriptable : a2a ssh {agent} --json",
                title="[bold green]a2a devbox ready[/]",
            )
        )
        return
    console.print(f"[dim]connecting to {agent} dev box (waking it if idle)...[/]")
    os.execvp("ssh", ["ssh", host_alias])


def _serve_devbox_tunnel(
    agent: str, port: int, key: Path, api: str | None, *, json_output: bool = False
) -> None:
    """Listen on 127.0.0.1:PORT and bridge each TCP connection to the dev box
    WebSocket, so any plain SSH client can connect with host/port/key."""
    try:
        from websockets.sync.client import connect
    except ImportError:  # pragma: no cover
        _fail("a2a ssh needs 'websockets' - reinstall a2a-pack (pip install -U a2a-pack)")

    import socket
    import threading

    _key, pub = _ensure_devbox_key(agent)
    client = _client(api)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", port))
    except OSError as exc:
        _fail(f"cannot listen on 127.0.0.1:{port}: {exc}")
    srv.listen(8)
    lport = srv.getsockname()[1]
    info = _devbox_conn_details(agent, key, f"{agent}.a2a", port=lport)
    if json_output:
        print(json.dumps(info, indent=2), flush=True)
    else:
        console.print(
            Panel.fit(
                f"Host       : 127.0.0.1\n"
                f"Port       : [bold]{lport}[/]\n"
                f"User       : {info['user']}\n"
                f"Auth       : key file (no password) -> {info['identity_file']}\n"
                f"Example    : ssh -i {info['identity_file']} -p {lport} dev@127.0.0.1",
                title=f"[bold green]a2a devbox tunnel: {agent}[/]",
            )
        )
        console.print("[dim]tunnel open - leave this running; Ctrl-C to close[/]")

    def handle(conn: socket.socket) -> None:
        ws = None
        try:
            # fresh grant per connection: tokens are short-TTL and the tunnel
            # may outlive them
            info = client.agent_ssh(
                name=agent, public_key=pub, credentials_json=_devbox_credentials_json()
            )
            ws = _connect_ws_retry(connect, info["wss_url"], info["access_token"])

            def ws_to_sock() -> None:
                try:
                    for msg in ws:
                        conn.sendall(msg if isinstance(msg, bytes) else msg.encode())
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            threading.Thread(target=ws_to_sock, daemon=True).start()
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                ws.send(data)
        except Exception as exc:  # noqa: BLE001 - one bad conn must not kill the tunnel
            console.print(f"[yellow]connection dropped: {exc}[/]")
        finally:
            for closer in (conn.close, getattr(ws, "close", lambda: None)):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    try:
        while True:
            conn, _addr = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        console.print("[dim]tunnel closed[/]")
    finally:
        srv.close()


@app.command(name="ssh-proxy", hidden=True)
def ssh_proxy(
    agent: Annotated[str, typer.Argument()],
    api: Annotated[str | None, typer.Option("--api", hidden=True)] = None,
) -> None:
    """Internal: stdio<->wss bridge used as an ssh ProxyCommand."""
    try:
        from websockets.sync.client import connect
    except ImportError:  # pragma: no cover
        _fail("a2a ssh needs 'websockets' - reinstall a2a-pack (pip install -U a2a-pack)")

    import time

    _key, pub = _ensure_devbox_key(agent)
    client = _client(api)
    creds_json = _devbox_credentials_json()
    info: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            info = client.agent_ssh(name=agent, public_key=pub, credentials_json=creds_json)
            break
        except ApiError as exc:
            if attempt == 2:
                _fail(f"could not open dev box for {agent}: {exc}")
            time.sleep(2)
    assert info is not None
    _bridge_stdio_ws(connect, info["wss_url"], info["access_token"])


def _connect_ws_retry(connect: Any, wss_url: str, token: str) -> Any:
    """Open the devbox WebSocket, retrying while the box scales up from zero.
    Raises RuntimeError if it never becomes reachable."""
    import time

    deadline = time.monotonic() + 90.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return connect(
                wss_url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=None,
                open_timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - cold start: keep retrying
            last = exc
            time.sleep(2)
    raise RuntimeError(f"dev box did not become reachable: {last}")


def _bridge_stdio_ws(connect: Any, wss_url: str, token: str) -> None:
    """Pipe this process's stdin/stdout to the devbox WebSocket. Retries the
    connect while the box scales up from zero."""
    import threading

    try:
        ws = _connect_ws_retry(connect, wss_url, token)
    except RuntimeError as exc:
        _fail(str(exc))

    def pump_out() -> None:  # ws -> stdout (what ssh reads)
        try:
            for msg in ws:
                data = msg if isinstance(msg, bytes) else msg.encode()
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        except Exception:  # noqa: BLE001
            pass
        finally:
            os._exit(0)  # box closed the tunnel -> end the ProxyCommand

    threading.Thread(target=pump_out, daemon=True).start()
    try:
        while True:
            data = os.read(sys.stdin.fileno(), 65536)  # returns on first bytes
            if not data:
                break
            ws.send(data)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


@app.command(name="local-deploy")
def local_deploy(
    project_path: Annotated[
        Path | None,
        typer.Argument(
            metavar="PROJECT",
            help="Project directory containing a2a.yaml",
        ),
    ] = None,
    project: Path = typer.Option(Path("."), "--project", "-p"),
    public: bool | None = typer.Option(None, "--public/--private"),
    api: str | None = typer.Option(None, "--api", help="Local control plane URL"),
    token: str | None = typer.Option(None, "--token", help="Bearer token override"),
    token_email: str = typer.Option(
        "local@example.com",
        "--token-email",
        help="Local user email used when minting a local token with docker exec",
    ),
    docker_token: bool = typer.Option(
        True,
        "--docker-token/--no-docker-token",
        help="Mint a local CP token from the a2a-control-plane container when none is supplied",
    ),
    wait_control_plane: bool = typer.Option(
        True,
        "--wait-control-plane/--no-wait-control-plane",
        help="Wait for the local control plane /healthz before uploading",
    ),
    wait_agent: bool = typer.Option(
        False,
        "--wait-agent/--no-wait-agent",
        help="Wait for the deployed agent /healthz URL after upload",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Deploy an agent to the local devcontainer control plane.

    This is the non-interactive local harness for tests and generated agents:
    it mints an e2e bearer token from the local control-plane container when
    no token env var is present, then uses the same tarball upload path as
    `a2a deploy`.
    """
    if project_path is not None and project != Path("."):
        _fail("pass either PROJECT or --project, not both")
    project_dir = project_path or project
    try:
        from .local_harness import LocalHarnessError, deploy_local_agent

        result = deploy_local_agent(
            project_dir,
            api_url=api,
            token=token,
            public=public,
            token_email=token_email,
            allow_docker_token=docker_token,
            wait_control_plane_ready=wait_control_plane,
            wait_agent_ready=wait_agent,
        )
    except (ApiError, LocalHarnessError) as exc:
        _fail(str(exc))

    if json_output:
        # `listing_public` / `listing_why` are in as_dict(), so the JSON caller
        # sees the same answer the human-readable line below states.
        console.print_json(json.dumps(result.as_dict()))
        return
    console.print(
        Panel.fit(
            json.dumps(result.as_dict(), indent=2),
            title="[bold green]local deploy[/]",
        )
    )
    _print_listing_outcome(
        result.listing_public,
        result.listing_why,
        list_cmd="a2a local-deploy --public",
        unlist_cmd="a2a local-deploy --private",
    )


@app.command(name="local-cleanup")
def local_cleanup(
    name: str = typer.Argument(..., help="Agent name to delete from the local control plane"),
    api: str | None = typer.Option(None, "--api", help="Local control plane URL"),
    token: str | None = typer.Option(None, "--token", help="Bearer token override"),
    token_email: str = typer.Option(
        "local@example.com",
        "--token-email",
        help="Local user email used when minting a local token with docker exec",
    ),
    docker_token: bool = typer.Option(
        True,
        "--docker-token/--no-docker-token",
        help="Mint a local CP token from the a2a-control-plane container when none is supplied",
    ),
    wait_control_plane: bool = typer.Option(
        True,
        "--wait-control-plane/--no-wait-control-plane",
        help="Wait for the local control plane /healthz before deleting",
    ),
    ignore_missing: bool = typer.Option(
        False,
        "--ignore-missing",
        help="Return success when the agent is already absent",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Delete a local devcontainer agent and managed local resources."""
    try:
        from .local_harness import LocalHarnessError, cleanup_local_agent

        result = cleanup_local_agent(
            name,
            api_url=api,
            token=token,
            token_email=token_email,
            allow_docker_token=docker_token,
            wait_control_plane_ready=wait_control_plane,
            ignore_missing=ignore_missing,
        )
    except (ApiError, LocalHarnessError) as exc:
        _fail(str(exc))

    if json_output:
        console.print_json(json.dumps(result.as_dict()))
        return
    console.print(
        Panel.fit(
            json.dumps(result.as_dict(), indent=2),
            title="[bold green]local cleanup[/]",
        )
    )


# Consume published agents as typed CLI commands (`a2a use` / `a2a call` +
# dynamically mounted `a2a <agent> <skill>` sub-apps from ~/.a2a/agents).
from .agents_cli import register_agent_stubs, register_commands  # noqa: E402

register_commands(app)
register_agent_stubs(app)


# Used as `python -m a2a_pack.cli.main`
if __name__ == "__main__":  # pragma: no cover
    app()
