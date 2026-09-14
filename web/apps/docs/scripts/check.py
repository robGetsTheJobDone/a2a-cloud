"""Documentation integrity gate for source, generated references, and links."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
DEFAULT_ROOT = HERE.parents[4]
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", flags=re.M)
ROUTE_IDS_RE = re.compile(
    r"export type DashboardRouteId\s*=\s*(.*?);",
    flags=re.S,
)

SPECIAL_ROUTES = {"/", "/llms.txt", "/llms-full.txt"}
PLATFORM_SURFACE_NAMES = {
    "workspace": "Workspace",
    "trials": "Trials",
    "activity": "Activity",
    "schedules": "Schedules",
    "simulations": "Simulations",
    "my-agents": "My Agents",
    "marketplace": "Marketplace",
    "studio": "Studio",
    "compose": "Compose",
    "installed-agents": "Installed Setup",
    "bounties": "Bounties",
    "access": "Access",
    "keys": "LLM Keys",
    "organization": "Organization",
    "compliance": "Compliance",
    "runtime": "Runtime",
}


def _slug_for(path: Path, content_root: Path) -> str:
    rel = path.relative_to(content_root).as_posix()
    slug = rel.removesuffix(".md")
    if slug == "index":
        return ""
    if slug.endswith("/index"):
        return slug.removesuffix("/index")
    return slug


def _heading_slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value.lower())
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _target_value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")]
    return raw.split(maxsplit=1)[0]


def _page_for_route(route: str, pages: dict[str, Path]) -> Path | None:
    if route.startswith("/raw/"):
        raw_slug = route.removeprefix("/raw/").removesuffix(".md")
        return pages.get("" if raw_slug == "index" else raw_slug)
    return pages.get(route.removeprefix("/").rstrip("/"))


def check_content(root: Path) -> list[str]:
    issues: list[str] = []
    content_root = root / "web" / "apps" / "docs" / "content"
    pages: dict[str, Path] = {}
    headings: dict[str, set[str]] = {}
    for path in sorted(content_root.rglob("*.md")):
        slug = _slug_for(path, content_root)
        if slug in pages:
            issues.append(f"duplicate page slug {slug!r}: {pages[slug]} and {path}")
            continue
        source = path.read_text()
        pages[slug] = path
        found_headings = HEADING_RE.findall(source)
        if not found_headings or len(found_headings[0][0]) != 1:
            issues.append(f"{path}: page must start with an H1")
        headings[slug] = {_heading_slug(title) for _, title in found_headings}

    for slug, path in sorted(pages.items()):
        source = path.read_text()
        for match in LINK_RE.finditer(source):
            target = _target_value(match.group(1))
            if not target.startswith("/"):
                continue
            route, _, fragment = target.partition("#")
            if route in SPECIAL_ROUTES:
                continue
            target_path = _page_for_route(route, pages)
            line = source.count("\n", 0, match.start()) + 1
            if target_path is None:
                issues.append(f"{path}:{line}: broken internal link {target}")
                continue
            if fragment:
                target_slug = _slug_for(target_path, content_root)
                if fragment not in headings[target_slug]:
                    issues.append(f"{path}:{line}: missing heading #{fragment} in {target_path}")

    platform_source = "\n".join(
        path.read_text()
        for slug, path in pages.items()
        if slug == "platform" or slug.startswith("platform/")
    )
    navigation = (root / "web" / "apps" / "dashboard" / "src" / "navigation.ts").read_text()
    match = ROUTE_IDS_RE.search(navigation)
    if not match:
        issues.append("could not read DashboardRouteId from dashboard navigation")
    else:
        route_ids = set(re.findall(r'"([^"]+)"', match.group(1)))
        mapped_ids = set(PLATFORM_SURFACE_NAMES)
        for route_id in sorted(route_ids - mapped_ids):
            issues.append(f"new dashboard route {route_id!r} needs documentation coverage mapping")
        for route_id in sorted(mapped_ids - route_ids):
            issues.append(f"stale documentation route mapping {route_id!r}")
        for route_id in sorted(route_ids & mapped_ids):
            name = PLATFORM_SURFACE_NAMES[route_id]
            if not re.search(rf"\b{re.escape(name)}\b", platform_source, flags=re.I):
                issues.append(f"platform docs do not cover dashboard surface {name!r}")

    stale_phrases = {
        "Available in the next a2a-pack release.": "remove stale future-release notices",
        "a2a dev                                                   # local server": (
            "bare a2a dev is cloud development; local docs must use --local"
        ),
    }
    checked_sources = [
        *pages.values(),
        root / "sdk" / "a2a-pack" / "a2a_pack" / "cli" / "templates" / "README.md.tmpl",
        root / "sdk" / "a2a-pack" / "a2a_pack" / "cli" / "templates" / "AGENTS.md.tmpl",
    ]
    for path in checked_sources:
        source = path.read_text()
        for phrase, message in stale_phrases.items():
            if phrase in source:
                issues.append(f"{path}: {message}")

    quickstart = (content_root / "quickstart.md").read_text()
    scaffold_readme = checked_sources[-2].read_text()
    if "a2a dev --local" not in quickstart:
        issues.append("quickstart must document a2a dev --local")
    if "Bare `a2a dev`" not in quickstart:
        issues.append("quickstart must explain that bare a2a dev uses cloud development")
    if "a2a dev --local" not in scaffold_readme:
        issues.append("scaffold README must use a2a dev --local for local development")

    # The Go and Rust SDKs route only the built-in `sum` skill. A developer who
    # reads the Implementation Contract before the limitation has already
    # committed to the language, so the disclosure has to come first.
    for slug in ("languages/go", "languages/rust"):
        page = pages.get(slug)
        if page is None:
            issues.append(f"{slug}.md is missing")
            continue
        source = page.read_text()
        notice_at = source.find("exactly one skill")
        contract_at = source.find("## Implementation Contract")
        if notice_at < 0:
            issues.append(f"{page}: must disclose that the SDK supports exactly one skill")
        elif 0 <= contract_at < notice_at:
            issues.append(
                f"{page}: single-skill disclosure must come before the Implementation Contract"
            )

    llms_route = (
        root / "web" / "apps" / "docs" / "app" / "llms.txt" / "route.ts"
    ).read_text()
    raw_route = (
        root
        / "web"
        / "apps"
        / "docs"
        / "app"
        / "raw"
        / "[[...slug]]"
        / "route.ts"
    )
    if "/raw/" not in llms_route or ".md" not in llms_route:
        issues.append("llms.txt must link to raw Markdown endpoints")
    if not raw_route.exists():
        issues.append("raw Markdown route is missing")

    manifest_path = content_root / "reference" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        source = manifest["source"]
        if source["repo_path"] != "sdk/a2a-pack" or len(source["tree_sha256"]) != 64:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        issues.append("reference manifest lacks valid monorepo SDK provenance")
    return issues


def run_generated_checks(root: Path) -> list[str]:
    commands = [
        [sys.executable, "web/apps/docs/scripts/gen.py", "--check"],
        [sys.executable, "web/apps/docs/scripts/gen_control_plane_api.py", "--check"],
        # `a2a-yaml.md` is hand-written, so its gate is the only thing keeping
        # it honest. Prove the gate still rejects the mistakes it exists for.
        [sys.executable, "web/apps/docs/scripts/gen_selftest.py"],
    ]
    issues: list[str] = []
    for command in commands:
        result = subprocess.run(command, cwd=root, text=True)
        if result.returncode:
            issues.append(f"generated documentation check failed: {' '.join(command)}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=str(DEFAULT_ROOT))
    parser.add_argument(
        "--skip-generated",
        action="store_true",
        help="Skip the slower SDK/CLI and API snapshot comparisons.",
    )
    args = parser.parse_args()
    root = Path(args.repo_root).resolve()
    issues = check_content(root)
    if not args.skip_generated:
        issues.extend(run_generated_checks(root))
    if issues:
        print("\n".join(f"error: {issue}" for issue in issues), file=sys.stderr)
        return 1
    print("documentation content, links, platform coverage, and generated snapshots are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
