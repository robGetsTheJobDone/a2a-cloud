"""Reusable meta-agent planning helpers.

This module is the SDK bridge between declarative composition manifests and
the raw-skill DAG executor. It deliberately plans in terms of existing
``agent`` + ``skill`` nodes; transport, grant delegation, and execution stay on
the normal ``RunContext`` and DAG paths.
"""
from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from .agent import A2AAgent, tool
from .context import AgentEvent, LLMCreds, RunContext
from .dag import DagLimits, DagNode, node_wire, parse_dag
from .meta_engine import MetaDagPlanner, run_meta_agent_goal
from .runtime import MetaAgentManifest

ConfigT = TypeVar("ConfigT", bound=BaseModel)
AuthT = TypeVar("AuthT", bound=BaseModel)

MetaPlannerLLM = Callable[
    [RunContext[Any], list[dict[str, str]], dict[str, Any]],
    Awaitable[str | dict[str, Any]] | str | dict[str, Any],
]


class MetaAgentPlannerError(RuntimeError):
    """Raised when a manifest-backed planner cannot produce a safe DAG."""


@dataclass(frozen=True)
class ResolvedSubAgent:
    """A manifest dependency resolved to concrete raw skills."""

    name: str
    url: str | None
    description: str
    version: str
    skills: tuple[dict[str, Any], ...]
    default_args: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    required: bool = True

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(str(item.get("name") or "") for item in self.skills)

    def planner_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skills": list(self.skills),
            "required": self.required,
        }
        if self.source:
            payload["source"] = self.source
        if self.default_args:
            payload["default_args"] = dict(self.default_args)
        return payload


class MetaAgent(A2AAgent[ConfigT, AuthT], Generic[ConfigT, AuthT]):
    """Base class for manifest-backed meta-agents.

    Subclasses normally only set ``name``, ``description``, and
    ``meta_agent_manifest``. They inherit the default ``pursue`` skill, which
    plans a raw-skill DAG from the manifest and executes it through
    :func:`run_meta_agent_goal`.
    """

    name = "meta-agent"
    description = "Composable meta-agent runtime"
    tools_used = ("a2a", "dag", "llm")

    @classmethod
    def manifest(cls) -> MetaAgentManifest:
        manifest = getattr(cls, "meta_agent_manifest", None)
        if isinstance(manifest, MetaAgentManifest):
            return manifest
        return MetaAgentManifest()

    async def planner_llm_call(
        self,
        ctx: RunContext[AuthT],
        messages: list[dict[str, str]],
        state: dict[str, Any],
    ) -> str | dict[str, Any]:
        return await call_meta_planner_llm(ctx, messages, state)

    async def build_planner(
        self,
        ctx: RunContext[AuthT],
        *,
        manifest: MetaAgentManifest | None = None,
    ) -> MetaDagPlanner:
        return build_manifest_dag_planner(
            ctx,
            manifest=manifest or type(self).manifest(),
            llm_call=self.planner_llm_call,
        )

    @tool(
        description=(
            "Plan and execute a bounded raw-skill DAG over the declared "
            "sub-agents."
        ),
        tags=("meta-agent", "dag", "composition"),
        stream=True,
        timeout_seconds=900,
        cost_class="expensive",
    )
    async def pursue(
        self,
        ctx: RunContext[AuthT],
        goal: str = "",
        success_criteria: list[str] | None = None,
        max_replans: int | None = None,
    ) -> dict[str, Any]:
        manifest = type(self).manifest()
        composition = manifest.composition
        declared_goal = manifest.goal
        objective = (goal or (declared_goal.objective if declared_goal else "")).strip()
        if not objective:
            raise MetaAgentPlannerError("meta-agent goal is required")

        criteria = (
            list(success_criteria)
            if success_criteria is not None
            else list(declared_goal.success_criteria if declared_goal else ())
        )
        limits = DagLimits(
            max_nodes=composition.max_nodes if composition else 8,
            max_parallel=composition.max_parallel if composition else 3,
        )
        replans = (
            max_replans
            if max_replans is not None
            else composition.max_replans
            if composition
            else 1
        )
        await ctx.emit_event(
            AgentEvent(
                kind="meta_goal",
                payload={
                    "agent": type(self).name,
                    "goal": objective,
                    "success_criteria": criteria,
                    "max_nodes": limits.max_nodes,
                    "max_parallel": limits.max_parallel,
                    "max_replans": replans,
                },
            )
        )
        planner = await self.build_planner(ctx, manifest=manifest)
        memory = (
            ctx.memory.for_manifest(manifest.memory, agent_name=type(self).name)
            if manifest.memory is not None
            else None
        )
        result = await run_meta_agent_goal(
            ctx,
            goal=objective,
            success_criteria=criteria,
            planner=_planner_with_memory(planner, memory) if memory is not None else planner,
            agent_name=type(self).name,
            max_replans=max(0, int(replans)),
            limits=limits,
        )
        if memory is not None:
            await _remember_meta_result(memory, goal=objective, result=result)
        return result


def build_manifest_dag_planner(
    ctx: RunContext[Any],
    *,
    manifest: MetaAgentManifest,
    llm_call: MetaPlannerLLM | None = None,
    system_prompt: str | None = None,
    resolved_subagents: Sequence[ResolvedSubAgent] | None = None,
) -> MetaDagPlanner:
    """Build a planner that validates LLM output against a manifest."""

    call_llm = llm_call or call_meta_planner_llm
    cached = list(resolved_subagents) if resolved_subagents is not None else None

    async def planner(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal cached
        composition = manifest.composition
        limits = DagLimits(
            max_nodes=composition.max_nodes if composition else 8,
            max_parallel=composition.max_parallel if composition else 3,
        )
        if cached is None:
            cached = await resolve_manifest_subagents(ctx, manifest)
        if not cached:
            raise MetaAgentPlannerError(
                "composition manifest resolved no callable sub-agents"
            )
        messages = build_meta_planner_messages(
            manifest=manifest,
            state=state,
            subagents=cached,
            system_prompt=system_prompt,
        )
        await ctx.emit_event(
            AgentEvent(
                kind="meta_planner_context",
                payload={
                    "sub_agents": [
                        {
                            "name": agent.name,
                            "skills": list(agent.skill_names),
                            "source": agent.source,
                        }
                        for agent in cached
                    ],
                    "attempt": state.get("attempt"),
                },
            )
        )
        raw = call_llm(ctx, messages, state)
        planned = await raw if inspect.isawaitable(raw) else raw
        parsed = parse_meta_planner_response(planned)
        return sanitize_meta_dag(
            parsed,
            goal=str(state.get("goal") or ""),
            subagents=cached,
            limits=limits,
        )

    return planner


def _planner_with_memory(planner: MetaDagPlanner, memory: Any) -> MetaDagPlanner:
    async def wrapped(state: dict[str, Any]) -> dict[str, Any] | str:
        query = " ".join(
            str(item or "")
            for item in (
                state.get("goal"),
                state.get("previous_result", {}).get("summary")
                if isinstance(state.get("previous_result"), dict)
                else None,
            )
        ).strip()
        if query:
            try:
                hits = await memory.search(query, limit=5)
            except Exception:  # noqa: BLE001
                hits = []
            if hits:
                state = {
                    **state,
                    "memory": {
                        "relevant": [
                            hit.model_dump(mode="json")
                            for hit in hits
                        ]
                    },
                }
        planned = planner(state)
        return await planned if inspect.isawaitable(planned) else planned

    return wrapped


async def _remember_meta_result(memory: Any, *, goal: str, result: dict[str, Any]) -> None:
    try:
        await memory.remember(
            f"runs/{_memory_key(goal)}",
            {
                "goal": goal,
                "status": result.get("status"),
                "ok": bool(result.get("ok")),
                "summary": result.get("summary"),
                "attempts": result.get("attempts"),
            },
            metadata={"source": "meta-agent"},
        )
    except Exception:  # noqa: BLE001
        return


async def resolve_manifest_subagents(
    ctx: RunContext[Any],
    manifest: MetaAgentManifest,
    *,
    per_tag_limit: int = 10,
) -> list[ResolvedSubAgent]:
    """Resolve manifest dependencies to concrete agents and allowed skills."""

    composition = manifest.composition
    if composition is None:
        return []

    by_name: dict[str, ResolvedSubAgent] = {}
    for spec in composition.sub_agents:
        matches = []
        if spec.name:
            try:
                matches = [await ctx.discover.get_agent(spec.name)]
            except Exception as exc:  # noqa: BLE001
                if spec.required:
                    raise MetaAgentPlannerError(
                        f"required sub-agent {spec.name!r} is unavailable: {exc}"
                    ) from exc
                continue
        elif spec.tag:
            try:
                matches = await ctx.discover.find_agents(
                    tags=(spec.tag,),
                    limit=per_tag_limit,
                )
            except Exception as exc:  # noqa: BLE001
                if spec.required:
                    raise MetaAgentPlannerError(
                        f"required sub-agent tag {spec.tag!r} is unavailable: {exc}"
                    ) from exc
                continue

        resolved_for_spec: list[ResolvedSubAgent] = []
        for hit in matches:
            skills = _select_skills(hit.card.skills, spec.skills)
            if not skills:
                continue
            resolved_for_spec.append(
                ResolvedSubAgent(
                    name=hit.name,
                    url=hit.url,
                    description=hit.card.description,
                    version=hit.card.version,
                    skills=skills,
                    default_args=dict(spec.default_args),
                    source=spec.name or f"tag:{spec.tag}",
                    required=spec.required,
                )
            )

        if spec.required and not resolved_for_spec:
            wanted = ", ".join(spec.skills) if spec.skills else "any skill"
            source = spec.name or f"tag:{spec.tag}"
            raise MetaAgentPlannerError(
                f"required sub-agent {source!r} has no allowed skills ({wanted})"
            )
        for resolved in resolved_for_spec:
            by_name[resolved.name] = _merge_resolved(by_name.get(resolved.name), resolved)

    return list(by_name.values())


def build_meta_planner_messages(
    *,
    manifest: MetaAgentManifest,
    state: Mapping[str, Any],
    subagents: Sequence[ResolvedSubAgent],
    system_prompt: str | None = None,
) -> list[dict[str, str]]:
    prompt = system_prompt or (
        "You are a meta-agent planner. Return only a JSON object with "
        "`goal` and `nodes`. Each node must use one listed sub-agent name and "
        "one listed raw skill name. Use `deps` for dependencies and "
        "{{nodes.<id>.result.<field>}} templates to pass prior outputs."
    )
    payload = {
        "manifest": manifest.public_payload(),
        "planner_state": _json_safe(state),
        "available_subagents": [agent.planner_payload() for agent in subagents],
        "dag_contract": {
            "shape": {
                "goal": "string",
                "nodes": [
                    {
                        "id": "short_unique_id",
                        "agent": "one available_subagents[].name",
                        "skill": "one available_subagents[].skills[].name",
                        "args": {},
                        "deps": ["prior_node_id"],
                        "expected_outputs": ["optional field names"],
                    }
                ],
            },
            "limits": (
                manifest.composition.public_payload()
                if manifest.composition is not None
                else {}
            ),
        },
    }
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, sort_keys=True)},
    ]


async def call_meta_planner_llm(
    ctx: RunContext[Any],
    messages: list[dict[str, str]],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Call the invocation LLM as an OpenAI-compatible JSON planner."""

    del state
    import httpx

    creds = ctx.llm
    if not creds.api_key:
        raise MetaAgentPlannerError("LLM credentials are unavailable for planning")
    payload = _chat_payload(creds, messages)
    url = f"{creds.base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            url,
            headers={
                "authorization": f"Bearer {creds.api_key}",
                "content-type": "application/json",
                "accept": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        raise MetaAgentPlannerError(
            f"LLM planner request failed with HTTP {response.status_code}: "
            f"{response.text[:240]}"
        )
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        raise MetaAgentPlannerError("LLM planner response missing message content") from exc
    return parse_meta_planner_response(_content_to_text(content))


def parse_meta_planner_response(raw: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        raise MetaAgentPlannerError("planner returned an empty response")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _parse_embedded_json(text)
    if not isinstance(parsed, dict):
        raise MetaAgentPlannerError("planner response must be a JSON object")
    return parsed


def sanitize_meta_dag(
    dag: Mapping[str, Any],
    *,
    goal: str,
    subagents: Sequence[ResolvedSubAgent],
    limits: DagLimits,
) -> dict[str, Any]:
    raw_json = json.dumps(dict(dag), sort_keys=True)
    parsed_goal, nodes = parse_dag(raw_json, max_nodes=limits.max_nodes)
    allowed: dict[str, set[str]] = {}
    defaults: dict[str, dict[str, Any]] = {}
    for agent in subagents:
        allowed.setdefault(agent.name, set()).update(agent.skill_names)
        defaults.setdefault(agent.name, {}).update(agent.default_args)

    sanitized: list[DagNode] = []
    errors: list[str] = []
    for node in nodes:
        if node.agent not in allowed:
            errors.append(f"node {node.id!r} uses undeclared agent {node.agent!r}")
            continue
        if node.skill not in allowed[node.agent]:
            errors.append(
                f"node {node.id!r} uses undeclared skill "
                f"{node.agent}.{node.skill}"
            )
            continue
        args = dict(defaults.get(node.agent) or {})
        args.update(node.args)
        sanitized.append(
            DagNode(
                id=node.id,
                agent=node.agent,
                skill=node.skill,
                args=args,
                deps=node.deps,
                expected_outputs=node.expected_outputs,
            )
        )

    if errors:
        raise MetaAgentPlannerError("; ".join(errors))
    return {
        "goal": goal or parsed_goal,
        "nodes": [node_wire(node) for node in sanitized],
    }


def _select_skills(
    skills: Sequence[Any],
    allowed: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    wanted = {item for item in allowed if item}
    out: list[dict[str, Any]] = []
    for skill_card in skills:
        name = str(getattr(skill_card, "name", "") or "")
        if wanted and name not in wanted:
            continue
        out.append(
            {
                "name": name,
                "description": str(getattr(skill_card, "description", "") or ""),
                "tags": list(getattr(skill_card, "tags", ()) or ()),
                "input_schema": dict(getattr(skill_card, "input_schema", {}) or {}),
            }
        )
    return tuple(out)


def _merge_resolved(
    existing: ResolvedSubAgent | None,
    new: ResolvedSubAgent,
) -> ResolvedSubAgent:
    if existing is None:
        return new
    skills_by_name = {item["name"]: item for item in existing.skills}
    for item in new.skills:
        skills_by_name[item["name"]] = item
    default_args = dict(existing.default_args)
    default_args.update(new.default_args)
    return ResolvedSubAgent(
        name=existing.name,
        url=existing.url or new.url,
        description=existing.description or new.description,
        version=existing.version or new.version,
        skills=tuple(skills_by_name.values()),
        default_args=default_args,
        source=", ".join(item for item in (existing.source, new.source) if item),
        required=existing.required or new.required,
    )


def _chat_payload(creds: LLMCreds, messages: list[dict[str, str]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": creds.model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    if creds.temperature_mode != "omit":
        payload["temperature"] = (
            creds.temperature if creds.temperature is not None else 0.0
        )
    if creds.extra_body:
        payload.update(dict(creds.extra_body))
    return payload


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def _parse_embedded_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise MetaAgentPlannerError("planner response did not contain a JSON object")
    return json.loads(stripped[start : end + 1])


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, Mapping):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)


def _memory_key(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.casefold()).strip("-")
    return (clean or "goal")[:96]


__all__ = [
    "MetaAgent",
    "MetaAgentPlannerError",
    "MetaPlannerLLM",
    "ResolvedSubAgent",
    "build_manifest_dag_planner",
    "build_meta_planner_messages",
    "call_meta_planner_llm",
    "parse_meta_planner_response",
    "resolve_manifest_subagents",
    "sanitize_meta_dag",
]
