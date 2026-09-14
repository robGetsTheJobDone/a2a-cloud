"""Generate a complete control-plane route inventory from FastAPI source.

This intentionally uses Python's AST instead of importing the control plane.
That keeps docs generation deterministic and avoids requiring databases,
Kubernetes configuration, or control-plane runtime dependencies.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve()
MONOREPO_ROOT = HERE.parents[4]
DEFAULT_ROUTES = MONOREPO_ROOT / "control-plane" / "control_plane" / "routes"
DEFAULT_OUT = HERE.parents[1] / "content" / "reference" / "control-plane-api.md"
HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put"}


@dataclass(frozen=True)
class Router:
    name: str
    prefix: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    tag: str
    summary: str
    source: str
    line: int
    hidden: bool


def _literal(node: ast.AST | None):
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _router_assignment(node: ast.stmt) -> tuple[str, ast.Call] | None:
    target: ast.expr | None = None
    value: ast.expr | None = None
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.AnnAssign):
        target, value = node.target, node.value
    if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
        return None
    func = value.func
    if not (
        isinstance(func, ast.Name) and func.id == "APIRouter"
        or isinstance(func, ast.Attribute) and func.attr == "APIRouter"
    ):
        return None
    return target.id, value


def _routers(tree: ast.Module, source: Path) -> dict[str, Router]:
    routers: dict[str, Router] = {}
    for node in tree.body:
        assignment = _router_assignment(node)
        if assignment is None:
            continue
        name, call = assignment
        prefix = _literal(_keyword(call, "prefix"))
        tags = _literal(_keyword(call, "tags"))
        if prefix is None:
            prefix = ""
        if not isinstance(prefix, str):
            raise ValueError(f"{source}:{node.lineno}: APIRouter prefix must be a literal string")
        if tags is None:
            tags = []
        if not isinstance(tags, (list, tuple)) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"{source}:{node.lineno}: APIRouter tags must be literal strings")
        routers[name] = Router(name=name, prefix=prefix, tags=tuple(tags))
    return routers


def _full_path(prefix: str, path: str) -> str:
    if not path:
        return prefix or "/"
    if not prefix:
        return path if path.startswith("/") else f"/{path}"
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def _summary(node: ast.AsyncFunctionDef | ast.FunctionDef, decorator: ast.Call) -> str:
    explicit = _literal(_keyword(decorator, "summary"))
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    docstring = ast.get_docstring(node, clean=True)
    if docstring:
        return docstring.splitlines()[0].strip()
    return node.name.replace("_", " ").strip().capitalize()


def _routes_from_file(path: Path, routes_root: Path) -> list[Route]:
    tree = ast.parse(path.read_text(), filename=str(path))
    routers = _routers(tree, path)
    source = (
        Path("control-plane")
        / "control_plane"
        / "routes"
        / path.relative_to(routes_root)
    ).as_posix()
    routes: list[Route] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.lower()
            if method not in HTTP_METHODS or not isinstance(decorator.func.value, ast.Name):
                continue
            router = routers.get(decorator.func.value.id)
            if router is None:
                raise ValueError(
                    f"{path}:{node.lineno}: route uses unknown router "
                    f"{decorator.func.value.id!r}"
                )
            raw_path = _literal(decorator.args[0] if decorator.args else None)
            if not isinstance(raw_path, str):
                raise ValueError(f"{path}:{node.lineno}: route path must be a literal string")
            include_in_schema = _literal(_keyword(decorator, "include_in_schema"))
            routes.append(
                Route(
                    method=method.upper(),
                    path=_full_path(router.prefix, raw_path),
                    tag=(router.tags[0] if router.tags else path.stem).strip(),
                    summary=_summary(node, decorator),
                    source=source,
                    line=node.lineno,
                    hidden=include_in_schema is False,
                )
            )
    return routes


def collect_routes(routes_root: Path) -> list[Route]:
    routes: list[Route] = []
    for path in sorted(routes_root.glob("*.py")):
        if path.name == "__init__.py":
            continue
        routes.extend(_routes_from_file(path, routes_root))
    routes.sort(key=lambda route: (route.tag, route.path, route.method, route.source, route.line))
    if not routes:
        raise RuntimeError(f"no FastAPI routes found under {routes_root}")
    return routes


def _scope(path: str, tag: str) -> str:
    if path.startswith("/v1/public/"):
        return "Public"
    if path.startswith("/v1/admin/") or "admin" in tag:
        return "Operator"
    if path.startswith("/v1/platform/") or "/webhook" in path:
        return "Platform"
    if path.startswith("/v1/scim/"):
        return "SCIM"
    if path.startswith("/v1/me"):
        return "Account"
    return "Authenticated"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _source_fingerprint(routes: list[Route]) -> str:
    digest = hashlib.sha256()
    for route in routes:
        for value in (
            route.method,
            route.path,
            route.tag,
            route.summary,
            route.source,
            str(route.hidden),
        ):
            digest.update(value.encode())
            digest.update(b"\0")
    return digest.hexdigest()


def render(routes_root: Path) -> str:
    routes = collect_routes(routes_root)
    visible_count = sum(not route.hidden for route in routes)
    tag_count = len({route.tag for route in routes})
    lines = [
        "# Control-plane API",
        "",
        "The control plane powers the dashboard, CLI, agent lifecycle,",
        "governance, workspaces, and orchestration. This inventory is generated",
        "directly from the FastAPI route declarations in the matching monorepo",
        "revision; it is not a separately maintained endpoint list.",
        "",
        "## Canonical schema and clients",
        "",
        "- Base URL: `https://api.a2acloud.io`",
        "- [OpenAPI 3 schema](https://api.a2acloud.io/openapi.json)",
        "- [Swagger UI](https://api.a2acloud.io/docs)",
        "- [ReDoc](https://api.a2acloud.io/redoc)",
        "- Python automation should normally use the `a2a` CLI or",
        "  `a2a_pack.cli.api_client.ControlPlaneClient`.",
        "",
        "The OpenAPI document is canonical for request and response schemas. This",
        "page is the reviewable route map and names the monorepo file each route is",
        "declared in. The monorepo is not public, so those paths are identifiers,",
        "not links.",
        "",
        "## Authentication and safety",
        "",
        "Send `Authorization: Bearer <token>` for authenticated API calls. The CLI",
        "obtains and refreshes this token through `a2a login`. Browser-session",
        "mutations also enforce CSRF protection. `/v1/public/*` is intentionally",
        "public; account, authenticated, SCIM, platform, and operator routes require",
        "the corresponding identity or service authority.",
        "",
        "Do not call **Platform** or **Operator** routes from ordinary integrations.",
        "They are listed so the platform contract is complete, not because they are",
        "public extension points.",
        "",
        "## Route inventory",
        "",
        f"- {len(routes)} route declarations across {tag_count} API groups",
        f"- {visible_count} included in OpenAPI; {len(routes) - visible_count} hidden/internal",
        f"- Route-contract fingerprint: `{_source_fingerprint(routes)}`",
        "",
    ]
    current_tag = ""
    for route in routes:
        if route.tag != current_tag:
            current_tag = route.tag
            lines.extend(
                [
                    f"### {current_tag}",
                    "",
                    "| Method | Path | Scope | Purpose | Source |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
        hidden = " (hidden)" if route.hidden else ""
        lines.append(
            f"| `{route.method}` | `{route.path}` | {_scope(route.path, route.tag)}{hidden} "
            f"| {_escape(route.summary)} | `{route.source}` |"
        )
    lines.extend(
        [
            "",
            "## Error shape",
            "",
            "Validation and HTTP failures use an HTTP status plus a JSON `detail`",
            "field. Platform middleware also emits a structured `error` object when",
            "available. Treat status codes as the stable branch condition and render",
            "the server message for operators rather than parsing human text.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--routes", default=str(DEFAULT_ROUTES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    routes_root = Path(args.routes).resolve()
    out = Path(args.out)
    rendered = render(routes_root)
    if args.check:
        if not out.exists() or out.read_text() != rendered:
            print(
                "error: stale generated control-plane API reference; run "
                "`python web/apps/docs/scripts/gen_control_plane_api.py`",
                file=sys.stderr,
            )
            return 1
        print("generated control-plane API reference matches FastAPI route source")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)
    print(f"generated {len(collect_routes(routes_root))} routes in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
