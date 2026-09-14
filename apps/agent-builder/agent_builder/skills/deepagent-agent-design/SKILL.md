---
name: deepagent-agent-design
description: Design generated A2A agents around DeepAgents skills, subagents, durable workspace backends, and ctx.llm-backed reasoning. Use whenever building or modifying an agent, deciding whether to add tools, creating skill bundles, wiring create_a2a_deep_agent, or avoiding shallow fake tools.
---
# DeepAgent Agent Design

Build generated agents as small A2A surfaces around a capable inner DeepAgent.
The A2A `@a2a.tool` (via `import a2a_pack as a2a`; bare `@tool` and legacy
`@skill` are the same object) is the user-facing API entrypoint.
The inner DeepAgent does planning, file work, skill selection, and subagent
delegation.

## Default Architecture

Use this shape for non-trivial generated agents:

1. Keep one or two public A2A `@a2a.tool` methods with clear typed parameters.
2. Inside each method, read `ctx.llm`, verify credentials are available, and
   build a DeepAgent with `create_a2a_deep_agent`.
3. Pass `backend=ctx.workspace_backend()` so DeepAgents file tools write to the
   caller's durable workspace instead of LangGraph state.
4. Seed project skills into the current grant's write prefix and pass
   `skills=skill_sources or None` when the generated project includes
   `skills/<skill-name>/SKILL.md`.
5. For custom subagents, include a `skills` field on each subagent definition.
   The general-purpose subagent inherits main skills, but custom subagents do
   not.

DeepAgents skills are progressive-disclosure folders:

```text
skills/
  skill-name/
    SKILL.md
    references/...
    scripts/...
    assets/...
```

`SKILL.md` must start with YAML frontmatter containing `name` and
`description`. The description is the trigger; include exactly when the skill
should be used. Keep detailed reference material in separate files that are
linked from `SKILL.md`.

## Runtime Skill Seeding

Project `skills/` files are source files in the agent image. DeepAgents loads
skills from its backend, so seed those files into the invocation workspace
before `create_a2a_deep_agent`:

```python
from pathlib import Path
from typing import Any

RUNTIME_SKILLS_DIR = "{{ agent_name }}/.deepagents/skills/"


def _runtime_skills_root(ctx: Any) -> str:
    workspace = getattr(ctx, "_workspace", None)
    prefixes = tuple(getattr(workspace, "write_prefixes", ()) or ())
    if not prefixes:
        outputs_prefix = getattr(workspace, "outputs_prefix", None)
        prefixes = (outputs_prefix or "outputs/",)
    prefix = str(prefixes[0]).strip("/")
    return f"/{prefix}/{RUNTIME_SKILLS_DIR}" if prefix else f"/{RUNTIME_SKILLS_DIR}"


def _seed_runtime_skills(backend: Any, ctx: Any) -> list[str]:
    root = Path(__file__).parent / "skills"
    if not root.exists():
        return []
    runtime_skills_root = _runtime_skills_root(ctx)
    uploads: list[tuple[str, bytes]] = []
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            uploads.append((runtime_skills_root + rel, path.read_bytes()))
    if uploads:
        backend.upload_files(uploads)
        return [runtime_skills_root]
    return []
```

Then:

```python
backend = ctx.workspace_backend()
skill_sources = _seed_runtime_skills(backend, ctx)
return create_a2a_deep_agent(
    ctx,
    creds=creds,
    backend=backend,
    skills=skill_sources or None,
    tools=[small_deterministic_tool],
    subagents=[...],
    system_prompt=SYSTEM_PROMPT,
)
```

Invoke the graph with the same recursion budget agent-builder uses:

```python
state = await graph.ainvoke(
    {"messages": [{"role": "user", "content": prompt}]},
    config={"recursion_limit": 500},
)
```

Because this uses `/outputs/...`, the write fits the normal A2A workspace
grant. If the agent uses `ctx.workspace_backend()`, also declare
`workspace_access = WorkspaceAccess.dynamic(...)` on the A2A agent class.

## Tool Design Rules

Do not make a long list of fake tools that just return canned text or call the
LLM again. Prefer:

- DeepAgents skills for procedural/domain knowledge the LLM should apply.
- Subagents for independent research, analysis, review, or transformation
  lanes that benefit from another LLM pass.
- Small deterministic tools only for exact operations: parsing, validation,
  API calls, math, file conversion, sandbox commands, database queries.

If a tool's body is mostly prompt text, it should usually be a DeepAgents
skill instead. If a tool needs judgement, route that judgement through the
inner DeepAgent or a subagent, not a hard-coded placeholder.

## Skill Authoring Workflow

When the generated agent needs reusable workflow knowledge, first call
`write_agent_skill` to create `skills/<skill-name>/SKILL.md` and any support
files. Then wire `agent.py` to seed and pass those skills.

Use concise skill descriptions with concrete trigger phrases. Example:

```yaml
---
name: market-research
description: Plan and run multi-source market research, delegate subtopics to subagents, save findings files, and synthesize cited reports. Use for competitive analysis, market maps, customer research, or current market questions.
---
```

Keep A2A public schemas stable and simple. Put rich autonomous behavior behind
the inner DeepAgent, not in a pile of public endpoints.
