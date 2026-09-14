"""Inner deepagents graph for the reviewer.

Reads the target agent's source from Gitea (via :class:`GiteaBackend`),
consults the three skill bundles bundled with this agent, and emits a
typed :class:`ReviewReport` through the ``submit_review_report`` tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from a2a_pack import GiteaBackend
from a2a_pack.deepagents import create_a2a_deep_agent

from .config import Settings, load_settings
from .tools import ReviewReport, ToolContext, build_tools


REVIEWER_SKILL_SOURCE = "/.agent-reviewer/skills/"


@dataclass(frozen=True)
class ReviewerContext:
    agent_name: str
    ref: str
    gitea_token: str
    gitea_owner: str
    settings: Settings | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature_mode: str | None = None
    llm_temperature: float | None = None
    llm_extra_body: dict[str, Any] | None = None
    llm_metadata: dict[str, Any] | None = None


SYSTEM_PROMPT = """\
You are an A2A agent reviewer. You audit deployed agents before they ship.

Your job: read the source of one agent project in a Gitea repository,
identify security, correctness, ergonomics, and scope issues, then emit a
single structured review via ``submit_review_report``.

You have packaged DeepAgents skills loaded from ``/.agent-reviewer/skills/``:

  - ``security-anti-patterns`` — credential leakage, unsafe execution,
    sandbox bypass, egress concerns. Consult first.
  - ``a2apack-best-practices`` — class structure, @a2a.tool decorator
    contract (bare @tool / legacy @skill are the same object), type
    hints, timeouts, error handling.
  - ``grant-scope-evaluation`` — declared vs actual workspace scope,
    write-prefix coverage, scope expansion correctness.

Workflow:

1. List files at the repo root with ``list_files`` (or use ``glob`` for
   patterns). Read ``a2a.yaml``, ``agent.py``, and ``requirements.txt``
   first. Then any imported modules under the package directory.
2. Apply each skill bundle to the source. Tabulate findings with
   ``severity`` (critical|warning|info) and ``category`` (security|
   correctness|ergonomics|policy|scope). Be strict about CRITICAL,
   conservative about INFO — false positives erode trust.
3. Call ``sandbox_a2a_card`` to verify the project actually loads. An
   exit_code != 0 is itself a CRITICAL correctness finding; cite the
   stderr line that broke things.
4. Call ``sandbox_ruff_check`` for objective static issues. Map S-prefix
   security rules to security/warning, other rules to ergonomics/info.
5. Once you have your full list, call ``submit_review_report`` exactly
   once with the JSON-encoded ReviewReport. ``ok=True`` iff zero CRITICAL.

Discipline:

  - One review = one ``submit_review_report`` call. Multiple submissions
    only on schema rejection; fix and resubmit.
  - Every finding must cite ``file`` (and ``line`` when the static check
    or AST scan gave you one).
  - Don't invent findings to look thorough. An empty findings list with
    ``ok=True`` is the right answer for a clean agent.
  - Don't deploy, don't push commits, don't modify files. You are
    read-only against the target repo.
"""


def build_reviewer_graph(
    ctx: ReviewerContext,
    *,
    completion_box: dict[str, ReviewReport],
) -> Any:
    settings = ctx.settings or load_settings()
    backend = GiteaBackend(
        gitea_url=settings.gitea_internal,
        owner=ctx.gitea_owner,
        repo=ctx.agent_name,
        ref=ctx.ref,
        token=ctx.gitea_token,
    )

    async def _fetch_source() -> dict[str, bytes]:
        tree = await backend._load_tree()  # type: ignore[attr-defined]
        files: dict[str, bytes] = {}
        for entry in tree:
            if entry.is_dir:
                continue
            try:
                files[entry.path] = await backend._read_bytes(entry.path)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
        return files

    tools = build_tools(
        ToolContext(
            settings=settings,
            agent_name=ctx.agent_name,
            ref=ctx.ref,
            fetch_source=_fetch_source,
            completion_box=completion_box,
        )
    )

    missing_llm_fields = [
        name
        for name, value in (
            ("llm_model", ctx.llm_model),
            ("llm_base_url", ctx.llm_base_url),
            ("llm_api_key", ctx.llm_api_key),
        )
        if not value
    ]
    if missing_llm_fields:
        raise ValueError(
            "reviewer requires forwarded ctx.llm credentials; missing "
            + ", ".join(missing_llm_fields)
        )

    llm_creds = SimpleNamespace(
        model=ctx.llm_model,
        base_url=ctx.llm_base_url,
        api_key=ctx.llm_api_key,
        temperature_mode=ctx.llm_temperature_mode or "default",
        temperature=ctx.llm_temperature,
        extra_body=dict(ctx.llm_extra_body or {}),
        metadata=dict(ctx.llm_metadata or {}),
    )

    return create_a2a_deep_agent(
        ctx,
        creds=llm_creds,
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        skills=[REVIEWER_SKILL_SOURCE],
    )
