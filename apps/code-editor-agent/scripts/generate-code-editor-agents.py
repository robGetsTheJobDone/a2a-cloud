#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_ROOT = REPO_ROOT / "apps" / "code-editor-agent" / "generated"
EXCLUDED_PARTS = {".git", ".codegraph", "node_modules", ".venv", "__pycache__"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate one OpenHarness code-editor A2A wrapper per local agent."
    )
    parser.add_argument("--list", action="store_true", help="Only list detected targets.")
    parser.add_argument("--clean", action="store_true", help="Remove generated wrappers first.")
    args = parser.parse_args()

    targets = discover_targets()
    if args.list:
        for target in targets:
            print(f"{target['editor_name']}\t{target['name']}\t{target['rel_path']}")
        return 0

    if args.clean and OUT_ROOT.exists():
        for child in OUT_ROOT.iterdir():
            if child.is_dir():
                for path in sorted(child.rglob("*"), reverse=True):
                    if path.is_file():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                child.rmdir()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    for target in targets:
        write_wrapper(target)
    print(f"generated {len(targets)} code editor agents under {OUT_ROOT.relative_to(REPO_ROOT)}")
    return 0


def discover_targets() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for manifest in sorted(REPO_ROOT.rglob("a2a.yaml")):
        if any(part in EXCLUDED_PARTS for part in manifest.parts):
            continue
        if "code-editor-agent" in manifest.parts:
            continue
        name = _top_level_yaml_value(manifest, "name")
        entrypoint = _top_level_yaml_value(manifest, "entrypoint")
        if not name or not entrypoint:
            continue
        rel_path = manifest.parent.relative_to(REPO_ROOT).as_posix()
        editor_name = _slug(f"{name}-code-editor")
        out.append({
            "name": name,
            "editor_name": editor_name,
            "rel_path": rel_path,
        })
    return out


def write_wrapper(target: dict[str, str]) -> None:
    root = OUT_ROOT / target["editor_name"]
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent.py").write_text(agent_py(target), encoding="utf-8")
    (root / "a2a.yaml").write_text(a2a_yaml(target), encoding="utf-8")
    (root / "requirements.txt").write_text(
        "-e ../..\na2a-pack>=0.1.57\nopenharness-ai>=0.1.7\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(readme(target), encoding="utf-8")


def agent_py(target: dict[str, str]) -> str:
    return f'''from __future__ import annotations

from pathlib import Path

from code_editor_agent import make_code_editor_agent_class


_REPO_ROOT = Path(__file__).resolve().parents[4]

CodeEditorAgent = make_code_editor_agent_class(
    name="{target["editor_name"]}",
    target_name="{target["name"]}",
    target_path=str(_REPO_ROOT / "{target["rel_path"]}"),
)
'''


def a2a_yaml(target: dict[str, str]) -> str:
    return f"""name: {target["editor_name"]}
version: 0.1.0
entrypoint: agent:CodeEditorAgent
description: OpenHarness + CodeGraph editor for {target["name"]}.
expose:
  public: false
"""


def readme(target: dict[str, str]) -> str:
    return f"""# {target["editor_name"]}

Generated wrapper around `apps/code-editor-agent` for `{target["name"]}`.

Target source: `{target["rel_path"]}`

Run locally:

```bash
cd apps/code-editor-agent/generated/{target["editor_name"]}
a2a run --entrypoint agent:CodeEditorAgent
```
"""


def _top_level_yaml_value(path: Path, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}:\s*(.+?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(" ") or line.startswith("\t") or not line.strip():
            continue
        match = pattern.match(line)
        if match:
            return match.group(1).strip().strip("'\"")
    return None


def _slug(value: str) -> str:
    out = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    out = re.sub(r"-+", "-", out)
    if not out or not out[0].isalpha():
        out = "agent-" + out
    return out[:63]


if __name__ == "__main__":
    raise SystemExit(main())
