"""Generate Markdown reference docs from the a2a_pack SDK + CLI.

Walks every public symbol exported from ``a2a_pack``, reads its
docstring + signature via ``inspect``, and writes a Markdown file per
module under ``content/reference/<module>.md``. Also captures the CLI's
typer ``--help`` output for every command.

Pure stdlib + introspection — no Sphinx, no Markdown parser. Idempotent;
running again overwrites prior output. ``--check`` renders into a temporary
directory and fails when the checked-in snapshot differs from the SDK at the
same monorepo revision.

Run:

    python web/apps/docs/scripts/gen.py
    python web/apps/docs/scripts/gen.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
# Monorepo layout:
# <root>/web/apps/docs/scripts/gen.py
# <root>/sdk/a2a-pack
REPO_ROOT = _HERE.parents[3]
MONOREPO_ROOT = _HERE.parents[4]
DEFAULT_SDK_PATH = REPO_ROOT.parent / "sdk" / "a2a-pack"
DEFAULT_OUT = _HERE.parents[1] / "content" / "reference"
DEFAULT_LANG_OUT = _HERE.parents[1] / "content" / "languages"


@dataclass
class SymbolDoc:
    name: str
    kind: str  # "class" | "function" | "module"
    signature: str
    docstring: str
    members: list["SymbolDoc"] = field(default_factory=list)
    module: str = ""

    def to_markdown(self, depth: int = 2) -> str:
        h = "#" * depth
        sig = f"```python\n{self.signature}\n```" if self.signature else ""
        body = self.docstring or "*(no docstring)*"
        parts = [f"{h} `{self.name}`  *({self.kind})*", "", sig, "", body, ""]
        for m in self.members:
            parts.append(m.to_markdown(depth + 1))
        return "\n".join(p for p in parts if p is not None)


def _short_sig(obj: Any, name: str) -> str:
    if inspect.isclass(obj) and issubclass(obj, Enum):
        # EnumMeta's generated signature changed between Python 3.11 and 3.12.
        return f"{name}(*values)"
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return ""
    return _clean_signature(f"{name}{sig}")


def _clean_signature(signature: str) -> str:
    """Remove nondeterministic framework object reprs from generated docs."""

    signature = re.sub(
        r" = <typer\.models\.(?:ArgumentInfo|OptionInfo) object at 0x[0-9a-fA-F]+>",
        "",
        signature,
    )
    signature = signature.replace("typing.Annotated[", "Annotated[")
    return signature


def _own_doc(obj: Any) -> str:
    """Return only the object's declared docstring, never an inherited one."""

    doc = getattr(obj, "__doc__", None)
    return inspect.cleandoc(doc) if isinstance(doc, str) else ""


def _walk_module(mod: Any) -> list[SymbolDoc]:
    out: list[SymbolDoc] = []
    seen: set[str] = set()
    exported = getattr(mod, "__all__", None)
    attr_names = exported if isinstance(exported, (list, tuple)) else dir(mod)
    for attr_name in sorted(name for name in attr_names if isinstance(name, str)):
        if attr_name.startswith("_"):
            continue
        if not hasattr(mod, attr_name):
            raise RuntimeError(f"{mod.__name__} exports missing symbol {attr_name!r}")
        attr = getattr(mod, attr_name)
        # Re-exports from other modules: skip when we'll cover the
        # owning module separately.
        owner_mod = getattr(attr, "__module__", None)
        if owner_mod and owner_mod != mod.__name__ and not owner_mod.startswith(
            mod.__name__ + "."
        ):
            continue
        key = f"{mod.__name__}.{attr_name}"
        if key in seen:
            continue
        seen.add(key)
        if inspect.isclass(attr):
            doc = SymbolDoc(
                name=attr_name, kind="class",
                signature=_short_sig(attr, attr_name),
                docstring=_own_doc(attr),
                module=mod.__name__,
            )
            for m_name, m_val in inspect.getmembers(attr):
                if m_name.startswith("_") and m_name != "__init__":
                    continue
                if not (inspect.isfunction(m_val) or inspect.ismethod(m_val)):
                    continue
                # Only methods defined on this class, not inherited from object.
                if (
                    getattr(m_val, "__qualname__", "").split(".")[0]
                    != attr.__name__
                ):
                    continue
                doc.members.append(SymbolDoc(
                    name=m_name, kind="method",
                    signature=_short_sig(m_val, m_name),
                    docstring=_own_doc(m_val),
                ))
            out.append(doc)
        elif callable(attr) and not inspect.isclass(attr):
            out.append(SymbolDoc(
                name=attr_name, kind="function",
                signature=_short_sig(attr, attr_name),
                docstring=_own_doc(attr),
                module=mod.__name__,
            ))
    return out


def render_module(mod_name: str) -> str:
    mod = importlib.import_module(mod_name)
    title = mod_name.split(".")[-1]
    parts = [
        f"# `{mod_name}`",
        "",
        inspect.getdoc(mod) or "",
        "",
    ]
    syms = _walk_module(mod)
    if not syms:
        parts.append("*No public symbols.*")
    for s in syms:
        parts.append(s.to_markdown(depth=2))
    parts.append("")
    source = mod_name.replace("a2a_pack.", "").replace(".", "/") + ".py"
    parts.append(f"*Source: `sdk/a2a-pack/a2a_pack/{source}`*")
    return "\n".join(parts)


SDK_MODULES = [
    "a2a_pack.agent",
    "a2a_pack.context",
    "a2a_pack.grants",
    "a2a_pack.workspace",
    "a2a_pack.runtime",
    "a2a_pack.card",
    "a2a_pack.frontend",
    "a2a_pack.a2a_client",
    "a2a_pack.discovery",
    "a2a_pack.sandbox",
    "a2a_pack.auth",
    "a2a_pack.serve.asgi",
    "a2a_pack.cli.main",
    "a2a_pack.cli.local",
    "a2a_pack.cli.dev_server",
    "a2a_pack.deepagents",
    "a2a_pack.mcp.http",
    "a2a_pack.mcp.server",
]


LANGUAGE_DOCS = [
    {
        "slug": "python",
        "title": "Python",
        "init": "a2a init math-agent --language python",
        "contract": (
            "Subclass `a2a_pack.A2AAgent` and implement `@a2a.tool` methods. "
            "Tools are published in the agent card's `skills` array (A2A spec "
            "vocabulary); `@skill` remains a supported alias of `@a2a.tool`."
        ),
        "compile": "a2a compile",
        "worker": "a2a dev --local",
        "sdk": "`a2a_pack` Python package",
        "snippet_lang": "python",
        "snippet": '''
import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext

class MathAgent(A2AAgent[None, NoAuth]):
    name = "math-agent"
    description = "Math helper"
    auth_model = NoAuth

    @a2a.tool(description="Add two numbers")
    async def sum(self, ctx: RunContext[NoAuth], left: float, right: float) -> dict[str, float]:
        return {"value": left + right}
''',
    },
    {
        "slug": "typescript",
        "title": "TypeScript/JS",
        "init": "a2a init math-agent --language typescript",
        "contract": "Extend `A2AAgent`, declare static skill schemas, and implement matching handler methods taking `(ctx, input)`. This TypeScript/JS SDK page covers typed projects and plain JS.",
        "compile": "npm run compile",
        "worker": "npm run worker",
        "sdk": "`a2a-pack-ts`, vendored into the scaffold at `vendor/a2a-pack-ts` and wired up as `\"a2a-pack-ts\": \"file:vendor/a2a-pack-ts\"`",
        "snippet_lang": "typescript",
        "snippet": '''
import { A2AAgent, publicAuth, skill, type RunContext } from "a2a-pack-ts";

type SumInput = { left: number; right: number };

export class MathAgent extends A2AAgent {
  static agent = { name: "math-agent", description: "Math helper", version: "0.1.0" };
  static auth = publicAuth();
  static skills = [skill({
    name: "sum",
    handler: "sum",
    description: "Add two numbers",
    input_schema: {
      type: "object",
      properties: { left: { type: "number" }, right: { type: "number" } },
      required: ["left", "right"]
    },
    output_schema: {
      type: "object",
      properties: { value: { type: "number" } },
      required: ["value"]
    }
  })];

  async sum(ctx: RunContext, input: SumInput): Promise<{ value: number }> {
    return { value: input.left + input.right };
  }
}
''',
    },
    {
        "slug": "go",
        "title": "Go (single-skill demo)",
        "init": "a2a init math-agent --language go",
        "contract": "Implement the `a2apack.Agent` interface and call `a2apack.CompileAgent` / `a2apack.ServeAgent`.",
        "compile": "go run . compile",
        "worker": "go run . worker",
        "sdk": "`a2acloud.io/a2a-pack-go` vendored at `third_party/a2a-pack-go`",
        "snippet_lang": "go",
        "snippet": '''
type MathAgent struct{}

func (MathAgent) Definition() a2apack.AgentDefinition {
    return a2apack.AgentDefinition{Name: "math-agent", Description: "Math helper", Version: "0.1.0"}
}

func (MathAgent) Sum(ctx context.Context, request a2apack.SumRequest) (a2apack.SumResponse, error) {
    return a2apack.SumResponse{Value: request.Left + request.Right}, nil
}
''',
    },
    {
        "slug": "rust",
        "title": "Rust (single-skill demo)",
        "init": "a2a init math-agent --language rust",
        "contract": "Implement the `A2AAgent` trait and call `compile_agent` / `serve_agent`.",
        "compile": "cargo run -- compile",
        "worker": "cargo run -- worker",
        "sdk": "`a2a-pack-rs` vendored at `third_party/a2a-pack-rs`",
        "snippet_lang": "rust",
        "snippet": '''
pub struct MathAgent;

impl A2AAgent for MathAgent {
    fn definition(&self) -> AgentDefinition {
        AgentDefinition { name: "math-agent", description: "Math helper", version: "0.1.0" }
    }

    fn sum(&self, request: SumRequest) -> Result<SumResponse, String> {
        Ok(SumResponse { value: request.left + request.right })
    }
}
''',
    },
]


def _single_skill_notice(slug: str) -> list[str]:
    """Render the SDK's own single-skill disclosure, or nothing.

    The text and the affected language list are owned by
    ``a2a_pack.cli.main`` so this page, the ``a2a init`` output, and the
    scaffolded README cannot say three different things.
    """
    from a2a_pack.cli.main import SINGLE_SKILL_LANGUAGES, single_skill_notice_markdown

    if slug not in SINGLE_SKILL_LANGUAGES:
        return []
    return ["## Read this first", "", single_skill_notice_markdown(), ""]


def render_language_doc(item: dict[str, str]) -> str:
    snippet = textwrap.dedent(item["snippet"]).strip()
    return "\n".join(
        [
            f"# {item['title']}",
            "",
            "This page is generated from the A2A Pack language SDK metadata.",
            "",
            *_single_skill_notice(item["slug"]),
            "## Scaffold",
            "",
            "```bash",
            item["init"],
            "```",
            "",
            "## Implementation Contract",
            "",
            item["contract"],
            "",
            f"```{item['snippet_lang']}",
            snippet,
            "```",
            "",
            "## Local Commands",
            "",
            "```bash",
            item["compile"],
            item["worker"],
            "```",
            "",
            "## SDK Package",
            "",
            f"{item['sdk']}.",
            "",
            "The compiled `.a2a/agent.dsl.json` is the sidecar contract. The sidecar",
            "owns public A2A, MCP, frontend, and invoke endpoints; the language SDK",
            "owns native handler execution through the worker protocol.",
            "",
        ]
    )


def write_language_docs(out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for item in LANGUAGE_DOCS:
        path = out_dir / f"{item['slug']}.md"
        path.write_text(render_language_doc(item))
        written.append(item["slug"])
        print(f"  + {_rel(path)}")
    return written


def _cli_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _cli_cell(value: Any) -> str:
    return _cli_text(value).replace("|", "\\|")


def _is_cli_option(param: Any) -> bool:
    return getattr(param, "param_type_name", "") == "option"


def _cli_type(param: Any) -> str:
    param_type = getattr(param, "type", None)
    choices = getattr(param_type, "choices", None)
    if choices:
        rendered = " | ".join(str(choice) for choice in choices)
    else:
        raw_name = str(getattr(param_type, "name", "") or "value").lower()
        rendered = {
            "bool": "BOOLEAN",
            "boolean": "BOOLEAN",
            "float": "FLOAT",
            "int": "INTEGER",
            "integer": "INTEGER",
            "path": "PATH",
            "str": "TEXT",
            "string": "TEXT",
            "text": "TEXT",
        }.get(raw_name, raw_name.upper())
    if getattr(param, "nargs", 1) == -1:
        rendered += "..."
    return rendered


def _cli_default(param: Any) -> str:
    default = getattr(param, "default", None)
    if default is None:
        return "—"
    if callable(default):
        return "<dynamic>"
    if isinstance(default, (list, tuple)):
        return ", ".join(str(value) for value in default) or "—"
    return str(default)


def _cli_flags(param: Any) -> str:
    flags = [*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())]
    if _is_cli_option(param):
        return ", ".join(f"`{flag}`" for flag in flags)
    return f"`{str(getattr(param, 'human_readable_name', param.name)).upper()}`"


def _cli_usage(command_name: str, command: Any) -> str:
    has_options = any(_is_cli_option(param) for param in command.params)
    tokens = [f"a2a {command_name}"]
    if has_options:
        tokens.append("[OPTIONS]")
    for param in command.params:
        if _is_cli_option(param):
            continue
        name = str(getattr(param, "human_readable_name", param.name)).upper()
        if getattr(param, "nargs", 1) == -1:
            name += "..."
        tokens.append(name if param.required else f"[{name}]")
    return " ".join(tokens)


def _cli_commands(group: Any, prefix: str = "") -> list[tuple[str, Any]]:
    commands: list[tuple[str, Any]] = []
    for name, command in sorted(getattr(group, "commands", {}).items()):
        command_name = f"{prefix} {name}".strip()
        commands.append((command_name, command))
        commands.extend(_cli_commands(command, command_name))
    return commands


def render_cli() -> str:
    """Render the Typer command model without version-specific rich help."""
    from typer.main import get_command

    from a2a_pack.cli.main import app

    root = get_command(app)
    commands = _cli_commands(root)
    parts = [
        "# `a2a` CLI",
        "",
        "Build, package, and deploy A2A agents.",
        "",
        "This page is generated from the CLI command model. The structured",
        "format is stable across terminal widths and Rich/Typer renderers.",
        "",
        "## Commands",
        "",
        "| Command | Purpose |",
        "| --- | --- |",
    ]
    for name, command in commands:
        purpose = _cli_cell(command.help or command.short_help)
        anchor = f"a2a-{name.replace(' ', '-')}"
        parts.append(f"| [`a2a {name}`](#{anchor}) | {purpose} |")
    parts.append("")

    for name, command in commands:
        parts.extend(
            [
                f"## `a2a {name}`",
                "",
                _cli_text(command.help or command.short_help) or "*(no description)*",
                "",
                "```text",
                _cli_usage(name, command),
                "```",
                "",
            ]
        )
        if command.params:
            parts.extend(
                [
                    "| Parameter | Type | Required | Default | Description |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for param in command.params:
                description = _cli_cell(getattr(param, "help", ""))
                parts.append(
                    f"| {_cli_flags(param)} | {_cli_type(param)} "
                    f"| {'yes' if param.required else 'no'} "
                    f"| {_cli_cell(_cli_default(param))} | {description} |"
                )
            parts.append("")
    return "\n".join(parts)


def _sdk_version(sdk_path: Path) -> str:
    init_source = (sdk_path / "a2a_pack" / "__init__.py").read_text()
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_source, flags=re.M)
    if not match:
        raise RuntimeError("could not read a2a_pack.__version__")
    return match.group(1)


def _sdk_tree_sha256(sdk_path: Path) -> str:
    """Stable source fingerprint for the SDK snapshot used by these docs."""

    ignored_parts = {
        ".git",
        ".gitea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "docs",
        "docker",
        "dist",
        "examples",
        "node_modules",
        "output",
        "target",
        "tests",
    }
    included_suffixes = {
        ".go",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".py",
        ".rs",
        ".tmpl",
        ".toml",
        ".ts",
        ".yaml",
        ".yml",
    }
    try:
        repo_root = Path(
            subprocess.run(
                ["git", "-C", str(sdk_path), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        sdk_rel = sdk_path.relative_to(repo_root)
        tracked = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--", sdk_rel.as_posix()],
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        candidates = [repo_root / raw.decode() for raw in tracked if raw]
    except (OSError, subprocess.SubprocessError, ValueError):
        # Keep generation usable from an unpacked source archive. The normal
        # monorepo path uses Git's tracked-file set so build artifacts cannot
        # influence provenance.
        candidates = [path for path in sdk_path.rglob("*") if path.is_file()]

    digest = hashlib.sha256()
    for path in sorted(candidates):
        rel = path.relative_to(sdk_path)
        if any(
            part in ignored_parts or part.endswith(".egg-info")
            for part in rel.parts
        ):
            continue
        if path.suffix not in included_suffixes:
            continue
        digest.update(rel.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_manifest(out_dir: Path, generated: list[str], sdk_path: Path) -> None:
    """Record the exact SDK source snapshot used by generated references."""

    items = [
        {
            "slug": slug,
            "title": _humanize(slug),
            "path": str((out_dir / f"{slug}.md").relative_to(out_dir.parent)),
        }
        for slug in sorted(generated)
    ]
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source": {
                    "package": "a2a-pack",
                    "version": _sdk_version(sdk_path),
                    "repo_path": "sdk/a2a-pack",
                    "tree_sha256": _sdk_tree_sha256(sdk_path),
                },
                "generated": generated,
                "items": items,
            },
            indent=2,
        )
        + "\n"
    )


def _humanize(slug: str) -> str:
    return slug.replace("_", " ").replace("-", " ").title()


def _rel(p: Path) -> str:
    """Pretty-print a path relative to REPO_ROOT, or absolute if unrelated."""
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def generate(sdk_path: Path, out_dir: Path, language_out: Path) -> tuple[int, int]:
    sdk_path = sdk_path.resolve()
    sys.path.insert(0, str(sdk_path))
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for mod in SDK_MODULES:
        try:
            md = render_module(mod)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"could not render required module {mod}: {exc}") from exc
        slug = mod.replace("a2a_pack.", "").replace(".", "-")
        path = out_dir / f"{slug}.md"
        path.write_text(textwrap.dedent(md).lstrip("\n"))
        written.append(slug)
        print(f"  + {_rel(path)}")

    # Render the command model directly rather than terminal-formatted Rich
    # help, which varies with Typer/Rich versions and terminal width.
    try:
        cli_md = render_cli()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not render required CLI reference: {exc}") from exc
    (out_dir / "cli.md").write_text(cli_md)
    written.append("cli")
    print(f"  + {_rel(out_dir / 'cli.md')}")

    write_manifest(out_dir, written, sdk_path)
    language_written = write_language_docs(language_out)
    print(f"\n{len(written)} reference files generated in {out_dir}")
    print(f"{len(language_written)} language files generated in {language_out}")
    return len(written), len(language_written)


def _compare_tree(expected: Path, actual: Path, *, allowed_extra: set[str]) -> list[str]:
    issues: list[str] = []
    expected_files = {
        p.relative_to(expected).as_posix(): p
        for p in expected.rglob("*")
        if p.is_file()
    }
    actual_files = {
        p.relative_to(actual).as_posix(): p
        for p in actual.rglob("*")
        if p.is_file()
    }
    for rel, expected_path in sorted(expected_files.items()):
        actual_path = actual_files.get(rel)
        if actual_path is None:
            issues.append(f"missing generated file: {actual / rel}")
        elif expected_path.read_bytes() != actual_path.read_bytes():
            issues.append(f"stale generated file: {actual / rel}")
    for rel in sorted(actual_files.keys() - expected_files.keys() - allowed_extra):
        issues.append(f"orphaned generated file: {actual / rel}")
    return issues


# Hand-written pages that live beside the generated ones. `a2a-yaml.md` is not
# rendered from the models, so `check_manifest_reference` verifies its key
# tables against them instead.
HAND_WRITTEN_REFERENCE = {"a2a-yaml.md", "control-plane-api.md", "index.md"}

# Section heading in `a2a-yaml.md` -> (`a2a_pack` model, {documented key: model field}).
# Every model field must appear as a key in that section's tables, and every
# documented key must be a model field or a declared alias.
MANIFEST_SECTIONS: dict[str, tuple[str, dict[str, str]]] = {
    "`frontend`": ("FrontendConfig", {"type": "kind"}),
    "`runtime.resources`": ("Resources", {}),
    "`runtime.egress`": ("EgressPolicy", {}),
    "`runtime.account_access`": ("AccountAccess", {}),
    "`runtime.endpoints[]`": ("AgentEndpoint", {}),
    "`resources.memory`": ("AgentMemory", {}),
    "`resources.databases[]`": ("AgentDatabase", {}),
    "`resources.databases[].env`": ("AgentDatabaseEnv", {}),
    "`resources.databases[].migrations`": ("AgentDatabaseMigrations", {}),
    "`self_healing`": ("SelfHealingPolicy", {}),
    "`composition`": ("AgentComposition", {}),
    "`composition.sub_agents[]`": ("CompositionSubAgent", {}),
    "`goal`": ("AgentGoal", {}),
    "`template_lineage`": ("TemplateLineage", {}),
}

_SECTION_RE = re.compile(r"^#{2,4}\s+(.+?)\s*$", flags=re.M)
_KEY_ROW_RE = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|.*$", flags=re.M)


@dataclass(frozen=True)
class DefaultPin:
    """Pin one `Default` cell in `a2a-yaml.md` to the code that produces it.

    Defaults are the easiest thing on that page to get wrong, because the SDK
    model default is frequently *not* the value a hosted deploy ends up using
    — the control plane substitutes its own when the key is omitted. Field
    names are covered by `check_manifest_reference`; this covers the values.

    Each pin names the single expression that decides the real default. If
    that expression moves or changes, the gate fails and the author has to
    re-read the code before touching the docs, instead of the reference
    quietly going stale.
    """

    section: str
    key: str
    source: str
    pattern: str
    decides: str
    # Group 1 of `pattern`, when set, is the literal default; the `Default`
    # cell must quote it verbatim.
    pinned_group: bool = False
    # Substrings required in the `Default` cell, in the whole row, and banned
    # from the whole row. Value claims belong in `default_contains` so that a
    # description is still free to name the value it is correcting.
    default_contains: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    forbids: tuple[str, ...] = ()


DEFAULT_PINS: tuple[DefaultPin, ...] = (
    DefaultPin(
        section="`expose`",
        key="public",
        source="sdk/a2a-pack/a2a_pack/cli/main.py",
        # Pins the terminal arm of `_resolve_listing`: flag > manifest >
        # current listing > this. It is reached only for a name the registry
        # has never seen, because an absent `expose` block means "unspecified"
        # and preserves whatever listing the agent already has — a redeploy
        # must never silently unlist a published agent.
        pattern=r'if registered is not None:\n        return registered, "unchanged"\n    return (True|False), "new"',
        decides="whether a brand-new agent with no `expose` block is listed",
        pinned_group=True,
        # `public` gates the registry and the public discovery endpoints only.
        # Reading it as a security boundary is the dangerous mistake here.
        requires=("not access control",),
    ),
    DefaultPin(
        section="`runtime`",
        key="concurrency",
        source="control-plane/control_plane/scaffold.py",
        pattern=r'_sanitize_positive_int\(\s*rt\.get\("concurrency"\),\s*default=(\d+)',
        decides="the hosted Knative containerConcurrency used when the key is omitted",
        pinned_group=True,
        default_contains=("hosted",),
    ),
    DefaultPin(
        section="`resources`",
        key="databases",
        source="control-plane/control_plane/database_resources.py",
        pattern=r'raise ValueError\("resources\.databases must be a list"\)',
        decides="the control plane rejecting a bare mapping at deploy time",
        requires=("must be a list",),
        # The SDK model folds a mapping into a one-item list, so this shorthand
        # parses locally and then 400s on deploy. It must not be documented.
        forbids=("one-item list",),
    ),
    DefaultPin(
        section="`resources.memory`",
        key="tiers",
        source="sdk/a2a-pack/a2a_pack/memory.py",
        pattern=r'return \(clean or \("([a-z_]+)",\)',
        decides="the tier used when none is declared",
        pinned_group=True,
    ),
    DefaultPin(
        section="`resources.memory`",
        key="namespace",
        source="sdk/a2a-pack/a2a_pack/memory.py",
        pattern=r'default_namespace=namespace or "([a-z_]+)"',
        decides="the namespace used when the key is omitted",
        # Notably not the agent name — the scaffold ships that as a commented
        # out example value, which is where the wrong default came from.
        pinned_group=True,
    ),
    DefaultPin(
        section="`resources.mailbox`",
        key="allowed_senders",
        source="control-plane/control_plane/mail_ingress.py",
        # Membership in the declared allowlist is the whole answer for a
        # non-owner sender: no branch turns an empty list into "allow all".
        pattern=(
            r"allowed = \{[^{}]*mailbox\.allowed_senders_json or \[\][^{}]*\}"
            r"\s*\n\s*return sender in allowed\b"
        ),
        decides="an empty allowlist meaning owner-only rather than open",
        default_contains=("owner only",),
        requires=("default-deny",),
    ),
)


def check_effective_defaults(path: Path, root: Path = MONOREPO_ROOT) -> list[str]:
    """Verify documented `a2a.yaml` defaults against the code that sets them."""

    if not path.is_file():
        return [f"missing hand-written manifest reference: {path}"]
    sections = _markdown_sections(path.read_text())
    issues: list[str] = []
    for pin in DEFAULT_PINS:
        source_path = root / pin.source
        if not source_path.is_file():
            issues.append(f"{pin.source}: pinned source for '{pin.key}' is missing")
            continue
        matches = re.findall(pin.pattern, source_path.read_text())
        if len(matches) != 1:
            issues.append(
                f"{pin.source}: the expression deciding {pin.decides} matched "
                f"{len(matches)} times (expected 1) — re-read it and update the "
                f"'{pin.key}' row in {path.name} and its pin in gen.py"
            )
            continue
        row = _key_rows(sections.get(pin.section, "")).get(pin.key)
        if row is None:
            issues.append(f"{path}: '{pin.section}' does not document a '{pin.key}' row")
            continue
        default_cell = _default_cell(row)
        if default_cell is None:
            issues.append(
                f"{path}: the '{pin.key}' row has no Default column to check"
            )
            continue
        in_default = [*pin.default_contains]
        if pin.pinned_group:
            in_default.append(str(matches[0]))
        for needle in in_default:
            if needle.lower() not in default_cell.lower():
                issues.append(
                    f"{path}: the Default cell for '{pin.key}' is "
                    f"{default_cell.strip()!r} but must say {needle!r} — "
                    f"{pin.source} decides {pin.decides}"
                )
        for needle in pin.requires:
            if needle.lower() not in row.lower():
                issues.append(
                    f"{path}: the '{pin.key}' row must say {needle!r} — "
                    f"{pin.source} decides {pin.decides}"
                )
        for needle in pin.forbids:
            if needle.lower() in row.lower():
                issues.append(
                    f"{path}: the '{pin.key}' row says {needle!r}, which "
                    f"{pin.source} contradicts ({pin.decides})"
                )
    return issues


def _model_field_names(model: Any) -> set[str]:
    fields = getattr(model, "model_fields", None)
    if fields is not None:
        return set(fields)
    import dataclasses

    if dataclasses.is_dataclass(model):
        return {f.name for f in dataclasses.fields(model)}
    raise RuntimeError(f"{model!r} is neither a pydantic model nor a dataclass")


def _markdown_sections(source: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(source))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        sections[match.group(1)] = source[match.end() : end]
    return sections


def _key_rows(body: str) -> dict[str, str]:
    """Map each documented key in a section's tables to its whole table row."""

    return {match.group(1): match.group(0) for match in _KEY_ROW_RE.finditer(body)}


def _default_cell(row: str) -> str | None:
    """Return the `Default` cell of a `| key | type | default | ... |` row."""

    # `\|` inside a cell is an escaped pipe (`string \| list`), not a divider.
    cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
    return cells[2] if len(cells) > 3 else None


def check_manifest_reference(path: Path) -> list[str]:
    """Verify `a2a.yaml` reference keys against the models that parse them."""

    import a2a_pack

    if not path.is_file():
        return [f"missing hand-written manifest reference: {path}"]
    sections = _markdown_sections(path.read_text())
    issues: list[str] = []
    for heading, (model_name, aliases) in MANIFEST_SECTIONS.items():
        body = sections.get(heading)
        if body is None:
            issues.append(f"{path}: missing section '{heading}'")
            continue
        expected = _model_field_names(getattr(a2a_pack, model_name))
        documented = set(_key_rows(body))
        resolved = {aliases.get(key, key) for key in documented}
        for field_name in sorted(expected - resolved):
            issues.append(
                f"{path}: '{heading}' does not document "
                f"{model_name}.{field_name}"
            )
        for key in sorted(documented):
            if aliases.get(key, key) not in expected:
                issues.append(
                    f"{path}: '{heading}' documents {key!r}, which is not a "
                    f"{model_name} field"
                )
    return issues


REGENERATE_HINT = "run `python web/apps/docs/scripts/gen.py` and commit the result"
HAND_EDIT_HINT = (
    "edit web/apps/docs/content/reference/a2a-yaml.md by hand to match the "
    "source — gen.py does not write that page, so regenerating will not clear "
    "these"
)


def _remediation_hints(
    generated_issues: list[str], hand_written_issues: list[str]
) -> list[str]:
    """Print only the hint that can actually fix the failures we have.

    The two classes have opposite fixes, and telling someone to regenerate a
    page `gen.py` refuses to write sends them in a circle.
    """

    hints: list[str] = []
    if generated_issues:
        hints.append(REGENERATE_HINT)
    if hand_written_issues:
        hints.append(HAND_EDIT_HINT)
    return hints


def check_generated(sdk_path: Path, out_dir: Path, language_out: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="a2a-docs-check-") as tmp:
        tmp_root = Path(tmp)
        expected_reference = tmp_root / "reference"
        expected_languages = tmp_root / "languages"
        generate(sdk_path, expected_reference, expected_languages)
        manifest_reference = out_dir / "a2a-yaml.md"
        # Two failure modes with two different fixes: regenerate, or hand-edit
        # the one reference page `gen.py` deliberately does not write.
        generated_issues = [
            *_compare_tree(
                expected_reference,
                out_dir,
                allowed_extra=HAND_WRITTEN_REFERENCE,
            ),
            *_compare_tree(expected_languages, language_out, allowed_extra=set()),
        ]
        hand_written_issues = [
            *check_manifest_reference(manifest_reference),
            *check_effective_defaults(manifest_reference),
        ]
    if generated_issues or hand_written_issues:
        issues = [*generated_issues, *hand_written_issues]
        print("\n".join(f"error: {issue}" for issue in issues), file=sys.stderr)
        for hint in _remediation_hints(generated_issues, hand_written_issues):
            print(hint, file=sys.stderr)
        return 1
    print("generated SDK, CLI, and language references match the monorepo SDK")
    print("a2a.yaml reference keys match the a2a_pack manifest models")
    print("a2a.yaml documented defaults match the code that produces them")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk-path", default=str(DEFAULT_SDK_PATH))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--language-out", default=str(DEFAULT_LANG_OUT))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    sdk_path = Path(args.sdk_path).resolve()
    out_dir = Path(args.out)
    language_out = Path(args.language_out)
    with tempfile.TemporaryDirectory(prefix="a2a-docs-agent-stubs-") as stub_dir:
        previous_agents_dir = os.environ.get("A2A_AGENTS_DIR")
        os.environ["A2A_AGENTS_DIR"] = stub_dir
        try:
            if args.check:
                return check_generated(sdk_path, out_dir, language_out)
            generate(sdk_path, out_dir, language_out)
        finally:
            if previous_agents_dir is None:
                os.environ.pop("A2A_AGENTS_DIR", None)
            else:
                os.environ["A2A_AGENTS_DIR"] = previous_agents_dir
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
