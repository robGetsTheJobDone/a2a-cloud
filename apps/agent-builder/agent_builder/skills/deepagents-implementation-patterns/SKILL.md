---
name: deepagents-implementation-patterns
description: Implement DeepAgents inside a2a-pack agents with real deterministic tools, project skills, custom subagents, workspace-backed files, and ctx.llm-backed model resolution. Use when editing create_a2a_deep_agent calls or deciding what belongs in tools, skills, or subagents.
---
# DeepAgents Implementation Patterns

DeepAgents supplies the inner agent loop. In this platform, the reliable shape
is: `ctx.llm`-backed `create_a2a_deep_agent`, A2A workspace backend, project
DeepAgents skills, and a small number of exact tools. Hosted generated agents should
declare `LLMProvisioning.PLATFORM`; explicit BYOK agents can declare
`CALLER_PROVIDED`, but both modes still read the same `ctx.llm` object.

## Implementation Shape

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool  # langchain's @tool, not a2a_pack's
from a2a_pack.deepagents import create_a2a_deep_agent

RUNTIME_SKILLS_DIR = "research-agent/.deepagents/skills/"

SYSTEM_PROMPT = """\
You are a careful research agent. Use project skills for workflow rules and
use workspace-backed tools for durable files. Prefer /workspace/outputs/ for
stable output paths only when that is one of the grant write prefixes.
"""


def build_graph(ctx: Any) -> Any:
    creds = ctx.llm
    if not creds.api_key:
        raise RuntimeError(
            "LLM credentials were not available; declare PLATFORM for hosted "
            "generated agents or configure caller-provided credentials."
        )
    @tool
    def validate_citations(items_json: str) -> str:
        """Validate that each citation has title, url, and claim fields."""
        items = json.loads(items_json)
        missing = [
            i for i, item in enumerate(items)
            if not {"title", "url", "claim"} <= set(item)
        ]
        return json.dumps({"ok": not missing, "missing_indexes": missing})

    backend = ctx.workspace_backend()
    skill_sources = seed_runtime_skills(backend, ctx)
    return create_a2a_deep_agent(
        ctx,
        creds=creds,
        backend=backend,
        skills=skill_sources or None,
        tools=[validate_citations],
        subagents=[
            {
                "name": "source-reviewer",
                "description": "Reviews source quality and citation coverage.",
                "system_prompt": (
                    "Check whether sources support the claims. Return gaps "
                    "and concrete fixes."
                ),
                "skills": skill_sources,
            }
        ],
        system_prompt=SYSTEM_PROMPT,
    )


def runtime_skills_root(ctx: Any) -> str:
    workspace = getattr(ctx, "_workspace", None)
    prefixes = tuple(getattr(workspace, "write_prefixes", ()) or ())
    if not prefixes:
        outputs_prefix = getattr(workspace, "outputs_prefix", None)
        prefixes = (outputs_prefix or "outputs/",)
    prefix = str(prefixes[0]).strip("/")
    return f"/{prefix}/{RUNTIME_SKILLS_DIR}" if prefix else f"/{RUNTIME_SKILLS_DIR}"


def seed_runtime_skills(backend: Any, ctx: Any) -> list[str]:
    root = Path(__file__).parent / "skills"
    if not root.exists():
        return []
    runtime_root = runtime_skills_root(ctx)
    uploads: list[tuple[str, bytes]] = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            uploads.append((runtime_root + rel, path.read_bytes()))
    if uploads:
        backend.upload_files(uploads)
        return [runtime_root]
    return []
```

## Choose The Right Primitive

Use a DeepAgents skill when the agent needs reusable judgement, process, domain
rules, examples, or a checklist. Put it under `skills/<skill-name>/SKILL.md`.

Use a subagent when a separate LLM pass can independently research, review,
transform, or critique work. Custom subagents should receive their own `skills`
field; do not assume they inherit every main-agent skill.

Use a deterministic tool only for exact work: JSON validation, schema checks,
filesystem transforms, API calls, calculations, sandbox commands, conversion
commands, or database queries.

Do not write tools whose body is prompt text, canned answers, or another hidden
LLM call. That logic belongs in a DeepAgents skill or a subagent.

## Skill Files

Each skill directory must contain `SKILL.md` with frontmatter:

```markdown
---
name: market-research
description: Run market research with source review, competitor mapping, and cited synthesis. Use for market questions, competitive analysis, or customer research.
---
# Market Research

Follow this workflow...
```

The `description` is the trigger text visible to the LLM before full skill
loading. Keep it concrete. Put long examples or data dictionaries into
`references/`, scripts into `scripts/`, and reusable assets into `assets/`.

## Backend Rules

DeepAgents' default state backend is not enough for hosted agents because file
outputs can disappear into graph state. Always pass `backend=ctx.workspace_backend()`
for generated agents that read or write files.

Use `skills=skill_sources or None`, not an empty list, so agents without a
`skills/` directory still build cleanly.

## Invocation Budget

Generated agents should use the same recursion budget as agent-builder:

```python
state = await graph.ainvoke(
    {"messages": [{"role": "user", "content": prompt}]},
    config={"recursion_limit": 500},
)
```

For streaming/event loops, pass the same config to `graph.astream_events(...)`.
This prevents normal multi-step skill/subagent workflows from failing at the
default LangGraph recursion cap.
